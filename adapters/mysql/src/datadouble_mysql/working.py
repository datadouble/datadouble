# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The scratch side: working databases, anonymization, images and restores.

``load`` builds a group's working database on a fresh server: ``before_load``
hooks, the schema (tables, views and routines), the rows with ``LOAD DATA``
and foreign-key checks off, fallbacks for missing parents, then the triggers,
so loading does not fire them. Tables of other groups are dropped, with the
foreign keys and views that would point at nothing. ``transform`` copies the
stopped server's files and anonymizes the copy; MySQL cannot switch triggers
off for a session, so they are set aside while the rules and hooks run in one
transaction, and put back. ``package`` writes the data directory (physical)
or a ``mysqldump`` script (logical); ``restore`` brings either back up.
"""

import re
import shutil
from collections.abc import Generator, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pymysql
from pydantic import BaseModel, ConfigDict
from pymysql.connections import Connection

from datadouble.catalog import Column, ColumnKind
from datadouble.engine import EngineError, RestoredImage, TableCount, Transformation
from datadouble.files import copy_tree
from datadouble.names import ColumnRef, TableName
from datadouble.planner import Fixup, Plan, PlannedGroup, PlannedRule
from datadouble.spec import ExpressionRule, Hook, ImageFormat, NullRule
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

    database: str
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
        [table.namespace, table.name, "%on update%"],
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

    # load

    def load(
        self,
        plan: Plan,
        group: PlannedGroup,
        extract: Path,
        directory: Path,
        hooks: tuple[Hook, ...],
    ) -> tuple[TableCount, ...]:
        """Build the group's working database."""
        index = ExtractIndex.model_validate_json((extract / "index.json").read_bytes())
        members = frozenset(group.tables)
        server = MysqlServer(self.tools, directory / _DATADIR, directory, [extract])
        server.initialize()
        for name in ("schema.sql", "triggers.sql"):
            script = without_definers((extract / name).read_text("utf-8"))
            (directory / name).write_text(script, "utf-8")
        with server.running():
            with server.connect() as connection:
                for hook in hooks:
                    _execute(connection, "a before_load hook", hook.sql)
            server.tool("mysql", [], directory / "schema.sql")
            with server.connect(index.database) as connection:
                _execute(
                    connection, "preparing to load", "SET foreign_key_checks = 0, unique_checks = 0"
                )
                for table in index.tables:
                    if table.table in members:
                        _execute(
                            connection,
                            f"loading {table.table}",
                            load_statement(table),
                            [str(extract / table.file)],
                        )
                for fixup in group.fixups:
                    self._fixup(connection, plan, index, fixup)
            server.tool("mysql", [], directory / "triggers.sql")
            with server.connect(index.database) as connection:
                _execute(connection, "preparing to prune", "SET foreign_key_checks = 0")
                for table in index.tables:
                    if table.table not in members:
                        _execute(
                            connection,
                            f"dropping {table.table}",
                            compose("DROP TABLE IF EXISTS {}", qualified(table.table)),
                        )
                self._prune(connection, index.database)
                counts = MysqlRestored(connection).counts(group.tables)
        info = DatabaseInfo(database=index.database, server_version=self.server_version())
        (directory / _INFO).write_text(info.model_dump_json(indent=2), "utf-8")
        return tuple(TableCount(table=t, rows=n) for t, n in counts.items())

    @staticmethod
    def _prune(connection: Connection, database: str) -> None:
        """Drop foreign keys and views that point at tables this database no longer has."""
        dangling = _rows(
            connection,
            "finding dangling references",
            """
            SELECT r.TABLE_NAME, r.CONSTRAINT_NAME FROM information_schema.REFERENTIAL_CONSTRAINTS r
            WHERE r.CONSTRAINT_SCHEMA = %s AND (
                r.UNIQUE_CONSTRAINT_SCHEMA <> r.CONSTRAINT_SCHEMA OR NOT EXISTS (
                    SELECT 1 FROM information_schema.TABLES t
                    WHERE t.TABLE_SCHEMA = r.CONSTRAINT_SCHEMA
                      AND t.TABLE_NAME = r.REFERENCED_TABLE_NAME))
            ORDER BY 1, 2
            """,
            [database],
        )
        for table, constraint in dangling:
            _execute(
                connection,
                f"dropping {constraint}",
                compose(
                    "ALTER TABLE {} DROP FOREIGN KEY {}",
                    qualified(TableName.of(database, str(table))),
                    quote(str(constraint)),
                ),
            )
        while broken := _rows(
            connection,
            "finding broken views",
            """
            SELECT DISTINCT u.VIEW_NAME FROM information_schema.VIEW_TABLE_USAGE u
            WHERE u.VIEW_SCHEMA = %s AND NOT EXISTS (
                SELECT 1 FROM information_schema.TABLES t
                WHERE t.TABLE_SCHEMA = u.TABLE_SCHEMA AND t.TABLE_NAME = u.TABLE_NAME)
            ORDER BY 1
            """,
            [database],
        ):
            for (view,) in broken:
                _execute(
                    connection,
                    f"dropping {view}",
                    compose("DROP VIEW {}", qualified(TableName.of(database, str(view)))),
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
        with server.running(), server.connect(info.database) as connection:
            triggers = self._set_aside_triggers(connection, info.database)
            _execute(connection, "starting the transformation", "START TRANSACTION")
            by_table: dict[TableName, list[PlannedRule]] = {}
            for rule in transformation.rules:
                by_table.setdefault(rule.column.table, []).append(rule)
            for position, (table, rules) in enumerate(by_table.items()):
                self._anonymize(connection, position, table, rules, transformation)
            for hook in transformation.hooks:
                _execute(connection, "an after_transform hook", hook.sql)
            _execute(connection, "committing the transformation", "COMMIT")
            for statement in triggers:
                _execute(connection, "restoring a trigger", statement)
            counts = MysqlRestored(connection).counts(group.tables)
        return tuple(TableCount(table=t, rows=n) for t, n in counts.items())

    @staticmethod
    def _set_aside_triggers(connection: Connection, database: str) -> list[str]:
        """Drop every trigger and return the statements that recreate them."""
        names = _rows(
            connection,
            "listing triggers",
            "SELECT TRIGGER_NAME FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = %s",
            [database],
        )
        statements: list[str] = []
        for (name,) in names:
            trigger = qualified(TableName.of(database, str(name)))
            rows = _rows(
                connection, f"reading {name}", compose("SHOW CREATE TRIGGER {}", trigger), []
            )
            statements.append(str(rows[0][2]))
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
                    info.database,
                ],
            )

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
            with server.connect(info.database) as connection:
                yield MysqlRestored(connection)
