# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The scratch side: working databases, anonymization, images and restores.

``load`` builds a group's working database on a fresh server: the default
database, with ``before_load`` hooks run in it, the schema (tables, views and
routines), the rows with ``LOAD DATA`` and foreign-key checks off, fallbacks
for missing parents, then the triggers, so loading does not fire them. Tables
of other groups are dropped, with the foreign keys and views that would point
at nothing. ``transform`` copies the stopped server's files and anonymizes the
copy; MySQL cannot switch triggers off for a session, so they are set aside
while the rules and hooks run in one transaction, and put back. ``package``
writes the data directory (physical) or a ``mysqldump`` script (logical);
``restore`` brings either back up. A working database holds every database the
source read, under its own name. A standalone group's holds exactly its
closure: the tables it pulls in, with the rows of the group's own extract and
no foreign keys into those that come without rows, and none of the routines,
triggers, events and views the closure lacks.
"""

import re
import secrets
import shutil
from collections.abc import Generator, Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import pymysql
from pydantic import BaseModel, ConfigDict
from pymysql.connections import Connection

from datadouble.catalog import Column, ColumnKind, ObjectKind, Requirement, RequirementKind
from datadouble.closure import ordered
from datadouble.engine import (
    Account,
    EngineError,
    RestoredImage,
    TableCount,
    Transformation,
    group_extract,
)
from datadouble.files import copy_tree
from datadouble.names import ColumnRef, TableName
from datadouble.planner import Fixup, Plan, PlannedGroup, PlannedRule
from datadouble.spec import AccountPrivileges, ExpressionRule, Hook, ImageFormat, NullRule
from datadouble.subset import Edge, EdgeMode
from datadouble_mysql.errors import database_error
from datadouble_mysql.render import columns, compose, qualified, quote
from datadouble_mysql.server import MysqlServer, Tools
from datadouble_mysql.source import ExtractedTable, ExtractIndex

_DATADIR = "mysql"
_DUMP = "database.sql"
_INFO = "database.json"
_DEFINER = re.compile(r"DEFINER=`[^`]*`@`[^`]*`\s*")
_BATCH = 1000
_PLACEHOLDERS: dict[ColumnKind, str] = {
    ColumnKind.TEXT: "''",
    ColumnKind.INTEGER: "0",
    ColumnKind.DECIMAL: "0",
    ColumnKind.FLOAT: "0",
    ColumnKind.DATE: "'1970-01-01'",
    ColumnKind.TIMESTAMP: "'1970-01-02 00:00:00'",
    ColumnKind.TIME: "'00:00:00'",
    ColumnKind.JSON: "'{}'",
    ColumnKind.BINARY: "''",
}


class DatabaseInfo(BaseModel):
    """Stored beside a working database and in every image."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    databases: tuple[str, ...]
    default: str
    """The database spec SQL without a table runs in, and copies announce."""
    server_version: str


def placeholder(table: TableName, column: Column) -> str:
    """A stand-in value for a required column of a stub row."""
    value = _PLACEHOLDERS.get(column.kind)
    if value is None:
        message = (
            f"cannot stub {table}: no placeholder for {column.name} ({column.type}); "
            "make the column nullable, give it a default, or use another fallback"
        )
        raise EngineError(message)
    return value


def without_definers(script: str) -> str:
    """A dump with its ``DEFINER`` clauses removed, so objects belong to whoever loads them."""
    return _DEFINER.sub("", script)


def create_database(script: str, database: str) -> str:
    """The dump's ``CREATE DATABASE`` for ``database``, keeping its character set.

    ``mysqldump --databases`` writes one per database, ``IF NOT EXISTS``.
    """
    name = quote(database)
    for line in script.splitlines():
        if line.startswith("CREATE DATABASE") and f" {name} " in f"{line} ":
            return line.rstrip().removesuffix(";")
    return compose("CREATE DATABASE IF NOT EXISTS {}", name)


def _scripts(extracts: tuple[Path, ...], directory: Path) -> list[tuple[str, str]]:
    """Each source's schema and trigger scripts, without definers, written to ``directory``."""
    scripts: list[tuple[str, str]] = []
    for position, extract in enumerate(extracts):
        names = (f"schema-{position:03d}.sql", f"triggers-{position:03d}.sql")
        for source, name in zip(("schema.sql", "triggers.sql"), names, strict=True):
            script = without_definers((extract / source).read_text("utf-8"))
            (directory / name).write_text(script, "utf-8")
        scripts.append(names)
    return scripts


def merged(indexes: tuple[ExtractIndex, ...]) -> ExtractIndex:
    """Several sources' extract indexes as one; the first source's default database leads."""
    return ExtractIndex(
        databases=tuple(d for index in indexes for d in index.databases),
        default=indexes[0].default,
        tables=tuple(t for index in indexes for t in index.tables),
        omitted_views=tuple(v for index in indexes for v in index.omitted_views),
        plugins=tuple(dict.fromkeys(p for index in indexes for p in index.plugins)),
    )


def load_statement(table: ExtractedTable) -> str:
    """``LOAD DATA`` for one extracted table; hexadecimal columns pass through ``UNHEX``."""
    targets = ", ".join(
        compose("@{}", quote(f"dd_{i}")) if name in table.hex else quote(name)
        for i, name in enumerate(table.columns)
    )
    assignments = [
        compose("{} = UNHEX(@{})", quote(name), quote(f"dd_{i}"))
        for i, name in enumerate(table.columns)
        if name in table.hex
    ]
    tail = compose(" SET {}", ", ".join(assignments)) if assignments else ""
    return compose(
        "LOAD DATA INFILE %s INTO TABLE {} CHARACTER SET utf8mb4 ({}){}",
        qualified(table.table),
        targets,
        tail,
    )


def _sources(
    group: PlannedGroup, index: ExtractIndex, extract: Path
) -> Iterator[tuple[Path, ExtractedTable]]:
    """Each table whose rows the group's database holds, and the folder its file is in.

    A standalone group's own extract has the rows it pulls in, and its own
    tables' when they gained some; other own tables come from the shared one.
    """
    own = group_extract(extract, group.name)
    taken: set[TableName] = set()
    if own is not None:
        pulled = ExtractIndex.model_validate_json((own / "index.json").read_bytes())
        for table in pulled.tables:
            taken.add(table.table)
            yield own, table
    members = frozenset(group.tables)
    for table in index.tables:
        if table.table in members and table.table not in taken:
            yield extract, table


def _rowless_references(connection: Connection, group: PlannedGroup) -> None:
    """Drop foreign keys into pulled-in tables that come without rows."""
    rowless = group.rowless()
    for table in sorted(rowless, key=str):
        found = _rows(
            connection,
            f"finding references to {table}",
            "SELECT r.CONSTRAINT_SCHEMA, r.TABLE_NAME, r.CONSTRAINT_NAME"
            " FROM information_schema.REFERENTIAL_CONSTRAINTS r"
            " WHERE r.UNIQUE_CONSTRAINT_SCHEMA = %s AND r.REFERENCED_TABLE_NAME = %s",
            [table.namespace[-1], table.name],
        )
        for database, child, constraint in found:
            if TableName.of(str(database), str(child)) in rowless:
                continue
            _execute(
                connection,
                f"dropping {constraint}",
                compose(
                    "ALTER TABLE {} DROP FOREIGN KEY {}",
                    qualified(TableName.of(str(database), str(child))),
                    quote(str(constraint)),
                ),
            )


_DROPPED = {
    ObjectKind.FUNCTION: "FUNCTION",
    ObjectKind.PROCEDURE: "PROCEDURE",
    ObjectKind.TRIGGER: "TRIGGER",
    ObjectKind.EVENT: "EVENT",
    ObjectKind.VIEW: "VIEW",
}
"""How to drop each kind of object the catalog lists (``introspect``)."""


def _drop_outside(connection: Connection, plan: Plan, group: PlannedGroup) -> None:
    """Drop the routines, triggers, events and views the group's closure lacks."""
    wanted = frozenset(group.objects)
    found = [item.ref for item in plan.catalog.objects if item.ref not in wanted]
    for ref in reversed(ordered(found, plan.catalog.objects)):
        _execute(
            connection,
            f"dropping {ref}",
            compose(
                "DROP {} IF EXISTS {}",
                _DROPPED[ref.kind],
                qualified(TableName.of(*ref.namespace, ref.name)),
            ),
        )


def _listed(values: tuple[str, ...]) -> str:
    """Placeholders for ``values``, for an ``IN`` list."""
    return ", ".join(["%s"] * len(values))


def _execute(
    connection: Connection, action: str, statement: str, params: list[object] | None = None
) -> None:
    try:
        with connection.cursor() as cursor:
            cursor.execute(statement, params)
    except pymysql.MySQLError as error:
        raise database_error(error, action) from None


def _rows(
    connection: Connection, action: str, statement: str, params: list[object]
) -> list[tuple[object, ...]]:
    try:
        with connection.cursor() as cursor:
            cursor.execute(statement, params)
            return [tuple(row) for row in cursor.fetchall()]
    except pymysql.MySQLError as error:
        raise database_error(error, action) from None


def _kept(connection: Connection, table: TableName, alias: str) -> list[str]:
    """Assignments that keep ``ON UPDATE`` columns as they are.

    MySQL changes a ``TIMESTAMP ... ON UPDATE CURRENT_TIMESTAMP`` column on
    any update unless the statement assigns it; anonymization must change only
    the columns its rules name.
    """
    found = _rows(
        connection,
        f"reading {table}",
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS"
        " WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND EXTRA LIKE %s",
        [table.namespace[0], table.name, "%on update%"],
    )
    return [compose("{}.{} = {}.{}", alias, quote(str(n)), alias, quote(str(n))) for (n,) in found]


def _matches(edge: Edge) -> str:
    return " AND ".join(
        compose("p.{} = c.{}", quote(p), quote(c))
        for p, c in zip(edge.parent_columns, edge.columns, strict=True)
    )


class MysqlRestored:
    """A database open for checks: a restored image, or a working database."""

    def __init__(self, connection: Connection) -> None:
        """Wrap a connection to the database."""
        self.connection = connection

    def _scalar(self, action: str, statement: str) -> int:
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(statement)
                row = cursor.fetchone()
        except pymysql.MySQLError as error:
            raise database_error(error, action) from None
        return int(str(row[0])) if row else 0

    def counts(self, tables: Iterable[TableName]) -> dict[TableName, int]:
        """Rows per table."""
        return {
            table: self._scalar(
                f"counting {table}", compose("SELECT COUNT(*) FROM {}", qualified(table))
            )
            for table in tables
        }

    def orphans(self, edge: Edge) -> int:
        """Child rows with a non-null reference and no parent."""
        present = " AND ".join(compose("c.{} IS NOT NULL", quote(c)) for c in edge.columns)
        when = f" AND ({edge.when})" if edge.when else ""
        return self._scalar(
            f"checking {edge.name}",
            compose(
                "SELECT COUNT(*) FROM {} c WHERE {}{} AND NOT EXISTS (SELECT 1 FROM {} p WHERE {})",
                qualified(edge.child),
                present,
                when,
                qualified(edge.parent),
                _matches(edge),
            ),
        )

    def text_values(self, column: ColumnRef) -> Iterator[str]:
        """Distinct non-null values of a column, as text, streamed."""
        name = quote(column.column)
        statement = compose(
            "SELECT DISTINCT CAST({} AS CHAR) COLLATE utf8mb4_bin FROM {} WHERE {} IS NOT NULL",
            name,
            qualified(column.table),
            name,
        )
        try:
            with self.connection.cursor(pymysql.cursors.SSCursor) as cursor:
                cursor.execute(statement)
                for (value,) in cursor:
                    yield str(value)
        except pymysql.MySQLError as error:
            raise database_error(error, f"scanning {column}") from None

    def failing_rows(self, query: str) -> int:
        """Rows a spec assertion returns."""
        return self._scalar(
            "running an assertion", compose("SELECT COUNT(*) FROM ({}) AS assertion", query)
        )


def _copy_tree(source: Path, destination: Path) -> None:
    """Copy a directory, sharing blocks where the filesystem allows."""
    problem = copy_tree(source, destination)
    if problem:
        message = f"cannot copy {source.name}: {problem}"
        raise EngineError(message)


class MysqlWorking:
    """Working databases and images under one scratch directory."""

    def __init__(self, tools: Tools, scratch: Path) -> None:
        """Use ``tools`` for every server under ``scratch``."""
        self.tools = tools
        self.scratch = scratch
        self._version: str | None = None

    def server_version(self) -> str:
        """The version of the image's server, which images are built for."""
        if self._version is None:
            self._version = self.tools.version(self.scratch)
        return self._version

    def close(self) -> None:
        """Nothing to stop: each call starts and stops its own servers."""

    def available(self) -> tuple[Requirement, ...]:
        """The plugin libraries in the image's plugin directory, at any version."""
        return tuple(
            Requirement(kind=RequirementKind.PLUGIN, name=library)
            for library in self.tools.plugin_libraries(self.scratch)
        )

    # load

    def load(
        self,
        plan: Plan,
        group: PlannedGroup,
        extracts: tuple[Path, ...],
        directory: Path,
        hooks: tuple[Hook, ...],
    ) -> tuple[TableCount, ...]:
        """Build the group's working database from every source's extract.

        Each source's schema runs in turn, then every source's rows, the
        fallbacks, and every source's triggers; hooks run first, in the first
        source's default database. A standalone group's rows come from each
        source's group extract where it has them.
        """
        read = [
            (extract, ExtractIndex.model_validate_json((extract / "index.json").read_bytes()))
            for extract in extracts
        ]
        index = merged(tuple(found for _, found in read))
        loaded = [
            (folder, table)
            for extract, found in read
            for folder, table in _sources(group, found, extract)
        ]
        owns = [own for extract in extracts if (own := group_extract(extract, group.name))]
        server = MysqlServer(self.tools, directory / _DATADIR, directory, [*extracts, *owns])
        server.initialize()
        scripts = _scripts(extracts, directory)
        omitted = frozenset(r.name for r in plan.omitted)
        with server.running():
            with server.connect() as connection:
                # Installed plugins are kept in the data directory, so later
                # servers and copies load them too.
                for plugin in index.plugins:
                    if plugin.library not in omitted:
                        _execute(
                            connection,
                            f"installing plugin {plugin.name}",
                            f"INSTALL PLUGIN {plugin.name} SONAME '{plugin.library}'",
                        )
                schema = (directory / scripts[0][0]).read_text("utf-8")
                _execute(
                    connection,
                    "creating the default database",
                    create_database(schema, index.default),
                )
            # The dump's own CREATE DATABASE for it is IF NOT EXISTS.
            with server.connect(index.default) as connection:
                for hook in hooks:
                    _execute(connection, "a before_load hook", hook.sql)
            for schema_script, _ in scripts:
                server.tool("mysql", [], directory / schema_script)
            with server.connect(index.default) as connection:
                _execute(
                    connection, "preparing to load", "SET foreign_key_checks = 0, unique_checks = 0"
                )
                for folder, table in loaded:
                    _execute(
                        connection,
                        f"loading {table.table}",
                        load_statement(table),
                        [str(folder / table.file)],
                    )
                for fixup in group.fixups:
                    self._fixup(connection, plan, index, fixup)
            for _, triggers_script in scripts:
                server.tool("mysql", [], directory / triggers_script)
            with server.connect(index.default) as connection:
                self._trim(connection, plan, group, index)
                counts = MysqlRestored(connection).counts(group.packaged())
        info = DatabaseInfo(
            databases=index.databases, default=index.default, server_version=self.server_version()
        )
        (directory / _INFO).write_text(info.model_dump_json(indent=2), "utf-8")
        return tuple(TableCount(table=t, rows=n) for t, n in counts.items())

    def _trim(
        self, connection: Connection, plan: Plan, group: PlannedGroup, index: ExtractIndex
    ) -> None:
        """Drop the tables the group's image lacks, and what then points nowhere.

        A standalone group's image also loses references into pulled-in
        tables without rows, and every object its closure lacks.
        """
        members = frozenset(group.packaged())
        _execute(connection, "preparing to prune", "SET foreign_key_checks = 0")
        for table in index.tables:
            if table.table not in members:
                _execute(
                    connection,
                    f"dropping {table.table}",
                    compose("DROP TABLE IF EXISTS {}", qualified(table.table)),
                )
        self._prune(connection, index.databases)
        if group.standalone:
            _rowless_references(connection, group)
            _drop_outside(connection, plan, group)

    @staticmethod
    def _prune(connection: Connection, databases: tuple[str, ...]) -> None:
        """Drop foreign keys and views that point at tables the working database lacks."""
        dangling = _rows(
            connection,
            "finding dangling references",
            compose(
                """
                SELECT r.CONSTRAINT_SCHEMA, r.TABLE_NAME, r.CONSTRAINT_NAME
                FROM information_schema.REFERENTIAL_CONSTRAINTS r
                WHERE r.CONSTRAINT_SCHEMA IN ({}) AND NOT EXISTS (
                    SELECT 1 FROM information_schema.TABLES t
                    WHERE t.TABLE_SCHEMA = r.UNIQUE_CONSTRAINT_SCHEMA
                      AND t.TABLE_NAME = r.REFERENCED_TABLE_NAME)
                ORDER BY 1, 2, 3
                """,
                _listed(databases),
            ),
            list(databases),
        )
        for database, table, constraint in dangling:
            _execute(
                connection,
                f"dropping {constraint}",
                compose(
                    "ALTER TABLE {} DROP FOREIGN KEY {}",
                    qualified(TableName.of(str(database), str(table))),
                    quote(str(constraint)),
                ),
            )
        while broken := _rows(
            connection,
            "finding broken views",
            compose(
                """
                SELECT DISTINCT u.VIEW_SCHEMA, u.VIEW_NAME
                FROM information_schema.VIEW_TABLE_USAGE u
                WHERE u.VIEW_SCHEMA IN ({}) AND NOT EXISTS (
                    SELECT 1 FROM information_schema.TABLES t
                    WHERE t.TABLE_SCHEMA = u.TABLE_SCHEMA AND t.TABLE_NAME = u.TABLE_NAME)
                ORDER BY 1, 2
                """,
                _listed(databases),
            ),
            list(databases),
        ):
            for database, view in broken:
                _execute(
                    connection,
                    f"dropping {view}",
                    compose("DROP VIEW {}", qualified(TableName.of(str(database), str(view)))),
                )

    @staticmethod
    def _fixup(connection: Connection, plan: Plan, index: ExtractIndex, fixup: Fixup) -> None:
        """Null or stub references whose parent rows are missing."""
        edge = fixup.edge
        conditions = [compose("c.{} IS NOT NULL", quote(c)) for c in edge.columns]
        if edge.when:
            conditions.append(f"({edge.when})")
        if fixup.parent_present:
            conditions.append(
                compose(
                    "NOT EXISTS (SELECT 1 FROM {} p WHERE {})",
                    qualified(edge.parent),
                    _matches(edge),
                )
            )
        where = " AND ".join(conditions)
        if fixup.mode is EdgeMode.NULL:
            # MySQL will not read the table an UPDATE changes in a subquery,
            # which a self-reference needs, so the missing parents are found
            # with an anti-join instead.
            assignments = ", ".join(
                [
                    *(compose("c.{} = NULL", quote(c)) for c in edge.columns),
                    *_kept(connection, edge.child, "c"),
                ]
            )
            base = [condition for condition in conditions if not condition.startswith("NOT EXISTS")]
            joined = ""
            if fixup.parent_present:
                joined = compose(" LEFT JOIN {} p ON {}", qualified(edge.parent), _matches(edge))
                base.append(compose("p.{} IS NULL", quote(edge.parent_columns[0])))
            statement = compose(
                "UPDATE {} c{} SET {} WHERE {}",
                qualified(edge.child),
                joined,
                assignments,
                " AND ".join(base),
            )
        else:
            parent = plan.catalog.table(edge.parent)
            extracted = index.table(edge.parent)
            loaded = extracted.columns if extracted else ()
            fillers = [
                (column.name, placeholder(edge.parent, column))
                for column in (parent.columns if parent else ())
                if column.name not in edge.parent_columns
                and not column.nullable
                and column.name in loaded
            ]
            targets = columns([*edge.parent_columns, *(name for name, _ in fillers)])
            values = ", ".join(
                [*(compose("c.{}", quote(c)) for c in edge.columns), *(v for _, v in fillers)]
            )
            statement = compose(
                "INSERT IGNORE INTO {} ({}) SELECT DISTINCT {} FROM {} c WHERE {}",
                qualified(edge.parent),
                targets,
                values,
                qualified(edge.child),
                where,
            )
        _execute(connection, f"applying the fallback for {edge.name}", statement)

    # transform

    def transform(
        self,
        group: PlannedGroup,
        source: Path,
        directory: Path,
        transformation: Transformation,
    ) -> tuple[TableCount, ...]:
        """Copy the working database and anonymize the copy."""
        directory.parent.mkdir(parents=True, exist_ok=True)
        _copy_tree(source, directory)
        info = DatabaseInfo.model_validate_json((directory / _INFO).read_bytes())
        server = MysqlServer(self.tools, directory / _DATADIR, directory)
        with server.running(), server.connect(info.default) as connection:
            triggers = self._set_aside_triggers(connection, info.databases)
            _execute(connection, "starting the transformation", "START TRANSACTION")
            by_table: dict[TableName, list[PlannedRule]] = {}
            for rule in transformation.rules:
                by_table.setdefault(rule.column.table, []).append(rule)
            for position, (table, rules) in enumerate(by_table.items()):
                self._anonymize(connection, position, table, rules, transformation)
            for hook in transformation.hooks:
                _execute(connection, "an after_transform hook", hook.sql)
            _execute(connection, "committing the transformation", "COMMIT")
            for database, statement in triggers:
                # A trigger's statement does not name its database.
                _execute(connection, "restoring a trigger", compose("USE {}", quote(database)))
                _execute(connection, "restoring a trigger", statement)
            _execute(connection, "restoring a trigger", compose("USE {}", quote(info.default)))
            counts = MysqlRestored(connection).counts(group.packaged())
        return tuple(TableCount(table=t, rows=n) for t, n in counts.items())

    @staticmethod
    def _set_aside_triggers(
        connection: Connection, databases: tuple[str, ...]
    ) -> list[tuple[str, str]]:
        """Drop every trigger and return each one's database and the statement recreating it."""
        names = _rows(
            connection,
            "listing triggers",
            compose(
                "SELECT TRIGGER_SCHEMA, TRIGGER_NAME FROM information_schema.TRIGGERS"
                " WHERE TRIGGER_SCHEMA IN ({}) ORDER BY 1, 2",
                _listed(databases),
            ),
            list(databases),
        )
        statements: list[tuple[str, str]] = []
        for database, name in names:
            trigger = qualified(TableName.of(str(database), str(name)))
            rows = _rows(
                connection, f"reading {name}", compose("SHOW CREATE TRIGGER {}", trigger), []
            )
            statements.append((str(database), str(rows[0][2])))
            _execute(connection, f"setting aside {name}", compose("DROP TRIGGER {}", trigger))
        return statements

    @staticmethod
    def _anonymize(
        connection: Connection,
        position: int,
        table: TableName,
        rules: list[PlannedRule],
        transformation: Transformation,
    ) -> None:
        """Apply every rule for one table in a single UPDATE."""
        joins: list[str] = []
        assignments: list[str] = []
        for number, rule in enumerate(rules):
            column = compose("t.{}", quote(rule.column.column))
            match rule.rule:
                case NullRule():
                    assignments.append(compose("{} = NULL", column))
                case ExpressionRule(expression=expression, where=where):
                    value = f"({expression})"
                    if where:
                        value = compose("CASE WHEN ({}) THEN {} ELSE {} END", where, value, column)
                    assignments.append(compose("{} = {}", column, value))
                case _:
                    alias = quote(f"dd_map_{position}_{number}")
                    MysqlWorking._replacements(connection, alias, rule, transformation)
                    joins.append(
                        compose(
                            "LEFT JOIN {} ON {}.old = CAST({} AS CHAR) COLLATE utf8mb4_bin",
                            alias,
                            alias,
                            column,
                        )
                    )
                    assignments.append(
                        compose(
                            "{} = CASE WHEN {} IS NULL THEN NULL ELSE {}.new END",
                            column,
                            column,
                            alias,
                        )
                    )
        named = {rule.column.column for rule in rules}
        kept = [
            assignment
            for assignment in _kept(connection, table, "t")
            if assignment.split(" = ", 1)[0] not in {compose("t.{}", quote(n)) for n in named}
        ]
        update = compose(
            "UPDATE {} AS t {} SET {}",
            qualified(table),
            " ".join(joins),
            ", ".join([*assignments, *kept]),
        )
        _execute(connection, f"anonymizing {table}", update)

    @staticmethod
    def _replacements(
        connection: Connection, alias: str, rule: PlannedRule, transformation: Transformation
    ) -> None:
        """Fill a temporary table mapping each original value to its replacement."""
        name = quote(rule.column.column)
        distinct = compose(
            "SELECT DISTINCT CAST({} AS CHAR) COLLATE utf8mb4_bin FROM {} WHERE {} IS NOT NULL",
            name,
            qualified(rule.column.table),
            name,
        )
        values = [str(row[0]) for row in _rows(connection, f"reading {rule.column}", distinct, [])]
        mapping = transformation.mapper(rule, values)
        _execute(
            connection,
            "preparing replacements",
            compose(
                "CREATE TEMPORARY TABLE {} (old LONGTEXT COLLATE utf8mb4_bin, new LONGTEXT,"
                " KEY (old(255))) CHARACTER SET utf8mb4",
                alias,
            ),
        )
        pairs = list(mapping.items())
        insert = compose("INSERT INTO {} (old, new) VALUES (%s, %s)", alias)
        try:
            with connection.cursor() as cursor:
                for start in range(0, len(pairs), _BATCH):
                    cursor.executemany(insert, pairs[start : start + _BATCH])
        except pymysql.MySQLError as error:
            raise database_error(error, "preparing replacements") from None

    # package and restore

    def package(self, source: Path, image_format: ImageFormat, directory: Path) -> None:
        """Write one format of the image into ``directory``."""
        info = DatabaseInfo.model_validate_json((source / _INFO).read_bytes())
        shutil.copyfile(source / _INFO, directory / _INFO)
        if image_format is ImageFormat.PHYSICAL:
            _copy_tree(source / _DATADIR, directory / _DATADIR)
            return
        server = MysqlServer(self.tools, source / _DATADIR, directory, [directory])
        with server.running():
            server.tool(
                "mysqldump",
                [
                    "--single-transaction",
                    "--routines",
                    "--triggers",
                    "--events",
                    "--set-gtid-purged=OFF",
                    "--skip-comments",
                    "--skip-dump-date",
                    f"--result-file={directory / _DUMP}",
                    "--databases",
                    *info.databases,
                ],
            )

    def add_accounts(self, image: Path, accounts: Sequence[Account]) -> None:
        """Give the physical image each account as ``'user'@'%'``, with its privileges.

        A server starts on the image's own data directory, so the accounts are
        written into it; the build user is dropped again before it stops. Each
        account's privileges are revoked and granted again, so an existing user
        ends up with exactly the declared ones. MySQL logs these statements with
        their passwords rewritten.
        """
        server = MysqlServer(self.tools, image / _DATADIR, image)
        with server.running():
            with server.connect() as connection:
                for account in accounts:
                    _create_account(connection, account)
            for account in accounts:
                _check_account(server, account)

    @contextmanager
    def restore(
        self, image: Path, image_format: ImageFormat, directory: Path
    ) -> Generator[RestoredImage]:
        """Bring an image up in a scratch server for checks."""
        info = DatabaseInfo.model_validate_json((image / _INFO).read_bytes())
        directory.mkdir(parents=True, exist_ok=True)
        server = MysqlServer(self.tools, directory / _DATADIR, directory, [image])
        if image_format is ImageFormat.PHYSICAL:
            _copy_tree(image / _DATADIR, directory / _DATADIR)
        else:
            server.initialize()
        with server.running():
            if image_format is ImageFormat.LOGICAL:
                server.tool("mysql", [], image / _DUMP)
            with server.connect(info.default) as connection:
                yield MysqlRestored(connection)


def _create_account(connection: Connection, account: Account) -> None:
    """Create the account or reset its password, and grant exactly its privileges.

    A ``read`` account gets ``SELECT`` and ``SHOW VIEW`` on each of the image's
    own databases, never on ``*.*``, which would let it read the ``mysql``
    schema's password hashes.
    """
    login = (account.user, account.password)
    action = f"giving the image the account {account.user!r}"
    try:
        with connection.cursor() as cursor:
            cursor.execute(compose("CREATE USER IF NOT EXISTS %s@'%%' IDENTIFIED BY %s"), login)
            cursor.execute(compose("ALTER USER %s@'%%' IDENTIFIED BY %s"), login)
            cursor.execute(
                compose("REVOKE ALL PRIVILEGES, GRANT OPTION FROM %s@'%%'"), (account.user,)
            )
            if account.privileges is AccountPrivileges.ALL:
                cursor.execute(
                    compose("GRANT ALL ON *.* TO %s@'%%' WITH GRANT OPTION"), (account.user,)
                )
                return
            cursor.execute(
                compose(
                    "SELECT schema_name FROM information_schema.schemata"
                    " WHERE schema_name NOT IN ({})",
                    ", ".join(["%s"] * len(_SYSTEM_SCHEMAS)),
                ),
                _SYSTEM_SCHEMAS,
            )
            for (database,) in cursor.fetchall():
                cursor.execute(
                    compose(
                        "GRANT SELECT, SHOW VIEW ON {}.* TO %s@'%%'", _grant_level(str(database))
                    ),
                    (account.user,),
                )
    except pymysql.MySQLError as error:
        raise database_error(error, action) from None


_SYSTEM_SCHEMAS = ("mysql", "information_schema", "performance_schema", "sys")


def _grant_level(database: str) -> str:
    """A database name for a database-level grant, where ``_`` and ``%`` are wildcards."""
    escaped = database.replace("_", "\\_").replace("%", "\\%")
    # The statement is formatted with bound values, so a literal % is doubled.
    return quote(escaped).replace("%", "%%")


def _check_account(server: MysqlServer, account: Account) -> None:
    """Log in as the account over the network, and fail if a wrong password works too.

    The second login proves the server checks the password: an anonymous
    account or a password-less one of the same name would accept any.
    """
    endpoint = server.server.endpoint
    action = f"logging in with the image's account {account.user!r}"

    def login(password: str) -> None:
        pymysql.connect(
            host=endpoint.host,
            port=endpoint.port,
            user=account.user,
            password=password,
            connect_timeout=30,
        ).close()

    try:
        login(account.password)
    except pymysql.MySQLError as error:
        raise database_error(error, action) from None
    try:
        login(secrets.token_hex(16))
    except pymysql.MySQLError:
        return
    message = f"{action} failed: the server accepted a wrong password"
    raise EngineError(message)
