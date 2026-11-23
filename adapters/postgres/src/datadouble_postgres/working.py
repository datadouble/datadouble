# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The scratch side: working databases, anonymization, images and restores.

``load`` builds a group's working database in a fresh cluster holding every
database the source read, under its own name: hooks, then in each database the
source's extensions at the source's versions, the schema's pre-data section,
the rows (with triggers off), fallbacks for missing parents, the post-data
section (indexes and constraints, which also check the data), sequences, and
finally dropping the tables that belong to other groups. A standalone group's
database holds exactly its closure: the tables it pulls in are kept, with
the rows of the group's own extract, and every object the closure lacks is
dropped, dependents first and without ``CASCADE``, so a dependency the
catalog missed stops the load rather than silently removing something.
``transform`` copies the stopped cluster and anonymizes the copy in one
transaction per database. ``package`` writes the data directory (physical) or
a ``pg_dump`` archive per database (logical); ``restore`` brings either back
up for verification. Spec SQL that names no table (hooks and assertions) runs
in the default database.
"""

import re
import shutil
from collections.abc import Callable, Generator, Iterable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from pydantic import BaseModel, ConfigDict

from datadouble.catalog import (
    Column,
    ColumnKind,
    ObjectKind,
    ObjectRef,
    Requirement,
    RequirementKind,
)
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
from datadouble.names import ColumnName, ColumnRef, TableName
from datadouble.planner import Fixup, Plan, PlannedGroup, PlannedRule
from datadouble.spec import AccountPrivileges, ExpressionRule, Hook, ImageFormat, NullRule
from datadouble.subset import Edge, EdgeMode
from datadouble_postgres.cluster import SUPERUSER, Cluster, Tools
from datadouble_postgres.errors import database_error
from datadouble_postgres.render import (
    Statement,
    columns,
    database_of,
    trusted,
    unqualified,
)
from datadouble_postgres.source import ExtractedDatabase, ExtractedTable, ExtractIndex

type Connection = psycopg.Connection[tuple[object, ...]]

_DATADIR = "pgdata"
_INFO = "database.json"
# An entry ends with its owner; extensions have none, so their lines end in a space.
_TOC = re.compile(r"^\d+; \d+ \d+ (.+) \S*$")
_DESCRIPTIONS = (
    "FK CONSTRAINT",
    "MATERIALIZED VIEW DATA",
    "MATERIALIZED VIEW",
    "VIEW",
    "COMMENT",
    "SCHEMA",
    "EXTENSION",
)
_PLACEHOLDERS: dict[ColumnKind, str] = {
    ColumnKind.TEXT: "",
    ColumnKind.INTEGER: "0",
    ColumnKind.DECIMAL: "0",
    ColumnKind.FLOAT: "0",
    ColumnKind.BOOLEAN: "false",
    ColumnKind.DATE: "epoch",
    ColumnKind.TIMESTAMP: "epoch",
    ColumnKind.TIME: "00:00",
    ColumnKind.UUID: "00000000-0000-0000-0000-000000000000",
    ColumnKind.JSON: "{}",
    ColumnKind.ARRAY: "{}",
    ColumnKind.BINARY: "",
}


class DatabaseInfo(BaseModel):
    """Stored beside a working database and in every image."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    databases: tuple[str, ...]
    default: str
    """The database spec SQL without a table runs in, and copies announce."""
    server_version: str
    partitioned: tuple[TableName, ...]


def dump_name(position: int) -> str:
    """The file holding the ``position``-th database's archive in a logical image."""
    return f"database-{position:03d}.dump"


_qualified = unqualified


def _table(name: TableName, partitioned: frozenset[TableName]) -> sql.Composable:
    return _qualified(name) if name in partitioned else sql.SQL("ONLY {}").format(_qualified(name))


def _and(parts: Iterable[sql.Composable]) -> sql.Composed:
    return sql.SQL(" AND ").join(parts)


def _commas(parts: Iterable[sql.Composable]) -> sql.Composed:
    return sql.SQL(", ").join(parts)


def toc_entry(line: str) -> tuple[str, str, str] | None:
    """A ``pg_restore --list`` line's ``(description, schema, tag)``, for the kinds handled."""
    match = _TOC.match(line)
    if match is None:
        return None
    description = next((d for d in _DESCRIPTIONS if match.group(1).startswith(d + " ")), None)
    if description is None:
        return None
    schema, _, tag = match.group(1)[len(description) + 1 :].partition(" ")
    return description, schema, tag


def filter_toc(
    listing: str, drop: frozenset[tuple[str, str, str]], *, comments: bool = True
) -> str:
    """Remove ``pg_restore --list`` entries matching ``(description, schema, tag)``.

    Comments on a removed object are removed with it, unless ``comments`` is false.
    """
    commented = (
        {(schema, f"{description} {tag}") for description, schema, tag in drop}
        if comments
        else set[tuple[str, str]]()
    )
    kept: list[str] = []
    for line in listing.splitlines():
        entry = toc_entry(line)
        if entry is not None:
            description, schema, tag = entry
            if entry in drop or (description == "COMMENT" and (schema, tag) in commented):
                continue
        kept.append(line)
    return "\n".join(kept) + "\n"


def only_toc(listing: str, description: str) -> tuple[str, frozenset[tuple[str, str, str]]]:
    """The listing reduced to entries of one ``description``, and those entries."""
    entries = [(line, toc_entry(line)) for line in listing.splitlines()]
    kept = [(line, e) for line, e in entries if e is not None and e[0] == description]
    return "".join(line + "\n" for line, _ in kept), frozenset(e for _, e in kept)


def placeholder(table: TableName, column: Column) -> sql.Composable:
    """A stand-in value for a required column of a stub row."""
    value = _PLACEHOLDERS.get(column.kind)
    if value is None:
        message = (
            f"cannot stub {table}: no placeholder for {column.name} ({column.type}); "
            "make the column nullable, give it a default, or use another fallback"
        )
        raise EngineError(message)
    return sql.SQL("{}::{}").format(sql.Literal(value), trusted(column.type))


def _execute(connection: Connection, action: str, statement: Statement) -> None:
    try:
        connection.execute(statement)
    except psycopg.Error as error:
        raise database_error(error, action) from None


class PostgresRestored:
    """Databases open for checks: a restored image, or a working database.

    Each database gets its connection when a check first needs it; checks that
    name no table run in the default database.
    """

    def __init__(
        self,
        connect: Callable[[str], Connection],
        default: str,
        partitioned: frozenset[TableName],
    ) -> None:
        """Open connections with ``connect``, by database name."""
        self.connect = connect
        self.default = default
        self.partitioned = partitioned
        self.connections: dict[str, Connection] = {}

    def close(self) -> None:
        """Close every connection opened."""
        for connection in self.connections.values():
            connection.close()

    def _connection(self, database: str) -> Connection:
        if database not in self.connections:
            self.connections[database] = self.connect(database)
        return self.connections[database]

    def _scalar(self, database: str, action: str, statement: Statement) -> int:
        try:
            row = self._connection(database).execute(statement).fetchone()
        except psycopg.Error as error:
            raise database_error(error, action) from None
        return int(str(row[0])) if row else 0

    def counts(self, tables: Iterable[TableName]) -> dict[TableName, int]:
        """Rows per table."""
        return {
            table: self._scalar(
                database_of(table),
                f"counting {table}",
                sql.SQL("SELECT count(*) FROM {}").format(_table(table, self.partitioned)),
            )
            for table in tables
        }

    def orphans(self, edge: Edge) -> int:
        """Child rows with a non-null reference and no parent."""
        present = _and(sql.SQL("c.{} IS NOT NULL").format(sql.Identifier(c)) for c in edge.columns)
        matches = _and(
            (
                sql.SQL("p.{} = c.{}").format(sql.Identifier(p), sql.Identifier(c))
                for p, c in zip(edge.parent_columns, edge.columns, strict=True)
            ),
        )
        when = sql.SQL(" AND ({})").format(trusted(edge.when)) if edge.when else sql.SQL("")
        return self._scalar(
            database_of(edge.child),
            f"checking {edge.name}",
            sql.SQL(
                "SELECT count(*) FROM {} c WHERE {}{} AND NOT EXISTS (SELECT 1 FROM {} p WHERE {})"
            ).format(
                _table(edge.child, self.partitioned),
                present,
                when,
                _table(edge.parent, self.partitioned),
                matches,
            ),
        )

    def text_values(self, column: ColumnRef) -> Iterator[str]:
        """Distinct non-null values of a column, as text, streamed."""
        statement = sql.SQL("SELECT DISTINCT {}::text FROM {} WHERE {} IS NOT NULL").format(
            sql.Identifier(column.column),
            _table(column.table, self.partitioned),
            sql.Identifier(column.column),
        )
        connection = self._connection(database_of(column.table))
        try:
            with connection.transaction(), connection.cursor(name="dd_values") as cursor:
                cursor.execute(statement)
                for (value,) in cursor:
                    yield str(value)
        except psycopg.Error as error:
            raise database_error(error, f"scanning {column}") from None

    def failing_rows(self, query: str) -> int:
        """Rows a spec assertion returns."""
        return self._scalar(
            self.default,
            "running an assertion",
            sql.SQL("SELECT count(*) FROM ({}) AS assertion").format(trusted(query)),
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


def _drop_outside(connection: Connection, plan: Plan, group: PlannedGroup, database: str) -> None:
    """Drop the database's objects the group's closure lacks, dependents first."""
    wanted = frozenset(group.objects)
    members = frozenset(group.packaged())
    # What needs a table the database lacks went with it.
    found = [
        item.ref
        for item in plan.catalog.objects
        if item.ref.namespace[0] == database
        and item.ref not in wanted
        and all(
            edge.table in members
            for edge in item.depends_on
            if edge.table is not None and not edge.inferred
        )
    ]
    for ref in reversed(ordered(found, plan.catalog.objects)):
        schema = ref.namespace[-1]
        _execute(connection, f"dropping {ref}", _drop(connection, ref, schema))


_DROPPED: dict[ObjectKind, sql.SQL] = {
    ObjectKind.FUNCTION: sql.SQL("FUNCTION"),
    ObjectKind.PROCEDURE: sql.SQL("PROCEDURE"),
    ObjectKind.VIEW: sql.SQL("VIEW"),
    ObjectKind.MATERIALIZED_VIEW: sql.SQL("MATERIALIZED VIEW"),
    ObjectKind.SEQUENCE: sql.SQL("SEQUENCE"),
}


def _drop(connection: Connection, ref: ObjectRef, schema: str) -> sql.Composed:
    """The statement that drops ``ref`` if it is there, refusing if anything depends on it."""
    name = sql.Identifier(schema, ref.name)
    if ref.kind is ObjectKind.TRIGGER:
        table, _, trigger = ref.name.partition(".")
        return sql.SQL("DROP TRIGGER IF EXISTS {} ON {}").format(
            sql.Identifier(trigger), sql.Identifier(schema, table)
        )
    if ref.kind is ObjectKind.TYPE:
        found = connection.execute(
            "SELECT t.typtype FROM pg_catalog.pg_type t"
            " JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace"
            " WHERE n.nspname = %s AND t.typname = %s",
            [schema, ref.name],
        ).fetchone()
        kind = sql.SQL("DOMAIN") if found and found[0] == "d" else sql.SQL("TYPE")
        return sql.SQL("DROP {} IF EXISTS {}").format(kind, name)
    # The signature is the catalog's own spelling of the argument types.
    arguments = trusted(ref.signature) if ref.signature else sql.SQL("")
    return sql.SQL("DROP {} IF EXISTS {}{}").format(_DROPPED[ref.kind], name, arguments)


@dataclass(frozen=True, slots=True)
class _Loading:
    """What loading each database of a group's working database needs."""

    cluster: Cluster
    plan: Plan
    group: PlannedGroup
    index: ExtractIndex
    extract: Path


class PostgresWorking:
    """Working databases and images under one scratch directory."""

    def __init__(self, tools: Tools, scratch: Path) -> None:
        """Use ``tools`` for every cluster under ``scratch``."""
        self.tools = tools
        self.scratch = scratch
        self._version: str | None = None

    def server_version(self) -> str:
        """The version of the image's server, which images are built for."""
        if self._version is None:
            self._version = self.tools.version()
        return self._version

    def close(self) -> None:
        """Nothing to stop: each call starts and stops its own servers."""

    def available(self) -> tuple[Requirement, ...]:
        """Every version of every extension the image has, from a throwaway cluster."""
        directory = self.scratch / "available"
        cluster = Cluster(self.tools, directory / _DATADIR, directory)
        try:
            cluster.initialize()
            with cluster.running(), cluster.connect("postgres") as connection:
                rows = connection.execute(
                    "SELECT name, version FROM pg_catalog.pg_available_extension_versions"
                ).fetchall()
        finally:
            shutil.rmtree(directory, ignore_errors=True)
        return tuple(
            Requirement(kind=RequirementKind.EXTENSION, name=str(name), version=str(version))
            for name, version in rows
        )

    # load

    def load(
        self,
        plan: Plan,
        group: PlannedGroup,
        extract: Path,
        directory: Path,
        hooks: tuple[Hook, ...],
    ) -> tuple[TableCount, ...]:
        """Build the group's working database, holding every database the source read."""
        index = ExtractIndex.model_validate_json((extract / "index.json").read_bytes())
        partitioned = frozenset(index.partitioned)
        cluster = Cluster(self.tools, directory / _DATADIR, directory, [extract])
        cluster.initialize()
        with cluster.running():
            for database in index.databases:
                cluster.create_database(database.name)
            with cluster.connect(index.default) as connection:
                for hook in hooks:
                    _execute(connection, "a before_load hook", trusted(hook.sql))
            loading = _Loading(cluster, plan, group, index, extract)
            for database in index.databases:
                self._load_database(loading, database)
            restored = PostgresRestored(cluster.connect, index.default, partitioned)
            try:
                counts = restored.counts(group.packaged())
            finally:
                restored.close()
        info = DatabaseInfo(
            databases=tuple(database.name for database in index.databases),
            default=index.default,
            server_version=self.server_version(),
            partitioned=index.partitioned,
        )
        (directory / _INFO).write_text(info.model_dump_json(indent=2), "utf-8")
        return tuple(TableCount(table=t, rows=n) for t, n in counts.items())

    def _load_database(self, loading: "_Loading", database: ExtractedDatabase) -> None:
        """Restore one database's schema and load its rows."""
        cluster, plan, group, index, extract = (
            loading.cluster,
            loading.plan,
            loading.group,
            loading.index,
            loading.extract,
        )
        name = database.name
        members = frozenset(group.packaged())
        rowless = group.rowless()
        dump = extract / database.schema_file
        listing = cluster.execute("pg_restore", ["--list", str(dump)])
        views = frozenset(
            (kind, view.namespace[-1], view.name)
            for view in database.omitted_views
            for kind in ("VIEW", "MATERIALIZED VIEW", "MATERIALIZED VIEW DATA")
        )
        # A constraint whose table is in the group but whose target is not (in
        # another group, or excluded) would fail on the rows: leave it out.
        # So would one into a pulled-in table that comes without rows.
        foreign = frozenset(
            ("FK CONSTRAINT", constraint.namespace, f"{constraint.table} {constraint.name}")
            for constraint in database.constraints
            if constraint.child in members
            and (
                constraint.parent not in members
                or (constraint.parent in rowless and constraint.child not in rowless)
            )
        )
        listing = self._extensions(cluster, database, plan, dump, listing)
        self._restore(cluster, name, dump, "pre-data", filter_toc(listing, views))
        own = [table for table in index.tables if database_of(table.table) == name]
        with cluster.connect(name) as connection, connection.transaction():
            connection.execute("SET LOCAL session_replication_role = replica")
            for folder, table in _sources(group, index, extract):
                if database_of(table.table) == name:
                    self._copy_in(connection, folder / table.file, table.table, table.columns)
            for fixup in group.fixups:
                if database_of(fixup.edge.child) == name:
                    self._fixup(connection, plan, index, fixup)
        self._restore(cluster, name, dump, "post-data", filter_toc(listing, views | foreign))
        with cluster.connect(name) as connection:
            self._sequences(connection)
            for table in own:
                if table.table not in members:
                    _execute(
                        connection,
                        f"dropping {table.table}",
                        sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(_qualified(table.table)),
                    )
            if group.standalone:
                _drop_outside(connection, plan, group, name)

    def _extensions(
        self, cluster: Cluster, database: ExtractedDatabase, plan: Plan, dump: Path, listing: str
    ) -> str:
        """Create the kept extensions at the source's versions; the listing left to restore.

        ``pg_dump`` writes ``CREATE EXTENSION`` without a version, which would
        install the image's default. So the schemas are restored first, then
        each extension is created at its version, and the listing returned
        holds neither. Omitted extensions are left out; whatever uses one fails
        to restore.
        """
        if not database.extensions:
            return listing
        omitted = frozenset(r.name for r in plan.omitted)
        schemas, schema_entries = only_toc(listing, "SCHEMA")
        _, extension_entries = only_toc(listing, "EXTENSION")
        if schema_entries:
            self._restore(cluster, database.name, dump, "pre-data", schemas)
        with cluster.connect(database.name) as connection:
            for extension in database.extensions:
                if extension.name in omitted:
                    continue
                _execute(
                    connection,
                    f"creating extension {extension.name} {extension.version}",
                    sql.SQL("CREATE EXTENSION IF NOT EXISTS {} WITH SCHEMA {} VERSION {}").format(
                        sql.Identifier(extension.name),
                        sql.Identifier(extension.schema_),
                        sql.Literal(extension.version),
                    ),
                )
        rest = filter_toc(listing, extension_entries)
        return filter_toc(rest, schema_entries, comments=False)

    def _restore(self, cluster: Cluster, database: str, dump: Path, section: str, toc: str) -> None:
        listing = cluster.datadir.parent / f"{section}.list"
        listing.write_text(toc, "utf-8")
        try:
            cluster.execute(
                "pg_restore",
                [
                    f"--section={section}",
                    f"--use-list={listing}",
                    "--no-owner",
                    "--no-privileges",
                    "--exit-on-error",
                    f"--dbname={cluster.local_conninfo(database)}",
                    str(dump),
                ],
            )
        finally:
            listing.unlink(missing_ok=True)

    @staticmethod
    def _copy_in(
        connection: Connection, path: Path, table: TableName, names: tuple[ColumnName, ...]
    ) -> None:
        statement = sql.SQL("COPY {} ({}) FROM STDIN").format(_qualified(table), columns(names))
        try:
            with (
                connection.cursor() as cursor,
                cursor.copy(statement) as copy,
                path.open("rb") as data,
            ):
                while chunk := data.read(1 << 20):
                    copy.write(chunk)
        except psycopg.Error as error:
            raise database_error(error, f"loading {table}") from None

    def _fixup(self, connection: Connection, plan: Plan, index: ExtractIndex, fixup: Fixup) -> None:
        """Null or stub references whose parent rows are missing."""
        edge = fixup.edge
        conditions = [sql.SQL("c.{} IS NOT NULL").format(sql.Identifier(c)) for c in edge.columns]
        if edge.when:
            conditions.append(sql.SQL("({})").format(trusted(edge.when)))
        if fixup.parent_present:
            conditions.append(
                sql.SQL("NOT EXISTS (SELECT 1 FROM {} p WHERE {})").format(
                    _table(edge.parent, frozenset(index.partitioned)),
                    _and(
                        (
                            sql.SQL("p.{} = c.{}").format(sql.Identifier(p), sql.Identifier(c))
                            for p, c in zip(edge.parent_columns, edge.columns, strict=True)
                        ),
                    ),
                )
            )
        where = _and(conditions)
        child = _qualified(edge.child)
        if fixup.mode is EdgeMode.NULL:
            assignments = _commas(
                sql.SQL("{} = NULL").format(sql.Identifier(c)) for c in edge.columns
            )
            statement: Statement = sql.SQL("UPDATE {} c SET {} WHERE {}").format(
                child, assignments, where
            )
        else:
            statement = self._stub(plan, index, edge, child, where)
        _execute(connection, f"applying the fallback for {edge.name}", statement)

    @staticmethod
    def _stub(
        plan: Plan, index: ExtractIndex, edge: Edge, child: sql.Composable, where: sql.Composable
    ) -> Statement:
        # The planner only allows stubs into tables the image has; if one is
        # missing anyway, the INSERT fails and the database says why.
        parent = plan.catalog.table(edge.parent)
        extracted = index.table(edge.parent)
        columns = parent.columns if parent else ()
        copied = extracted.columns if extracted else ()
        fillers = [
            (column.name, placeholder(edge.parent, column))
            for column in columns
            if column.name not in edge.parent_columns
            and not column.nullable
            and column.name in copied
        ]
        return sql.SQL("INSERT INTO {} ({}) SELECT DISTINCT {} FROM {} c WHERE {}").format(
            _qualified(edge.parent),
            _commas(sql.Identifier(n) for n in [*edge.parent_columns, *(n for n, _ in fillers)]),
            _commas(
                [
                    *(sql.SQL("c.{}").format(sql.Identifier(c)) for c in edge.columns),
                    *(value for _, value in fillers),
                ],
            ),
            child,
            where,
        )

    @staticmethod
    def _sequences(connection: Connection) -> None:
        """Move every column-owned sequence past the loaded rows."""
        rows = connection.execute(
            """
            SELECT n.nspname, c.relname, a.attname,
                   pg_catalog.pg_get_serial_sequence(
                       pg_catalog.format('%I.%I', n.nspname, c.relname), a.attname)
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_catalog.pg_attribute a
              ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
            WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
            """
        ).fetchall()
        for schema, table, column, sequence in rows:
            if sequence is None:
                continue
            name = sql.Identifier(str(column))
            _execute(
                connection,
                f"setting {sequence}",
                sql.SQL(
                    "SELECT pg_catalog.setval({}, coalesce(max({}), 1), max({}) IS NOT NULL) "
                    "FROM {}"
                ).format(
                    sql.Literal(str(sequence)), name, name, sql.Identifier(str(schema), str(table))
                ),
            )

    # transform

    def transform(
        self,
        group: PlannedGroup,
        source: Path,
        directory: Path,
        transformation: Transformation,
    ) -> tuple[TableCount, ...]:
        """Copy the working database and anonymize the copy, one transaction per database.

        Hooks run in the default database's transaction, after its rules.
        """
        directory.parent.mkdir(parents=True, exist_ok=True)
        _copy_tree(source, directory)
        info = DatabaseInfo.model_validate_json((directory / _INFO).read_bytes())
        partitioned = frozenset(info.partitioned)
        by_table: dict[TableName, list[PlannedRule]] = {}
        for rule in transformation.rules:
            by_table.setdefault(rule.column.table, []).append(rule)
        cluster = Cluster(self.tools, directory / _DATADIR, directory)
        last = [name for name in info.databases if name != info.default] + [info.default]
        with cluster.running():
            for database in last:
                with cluster.connect(database) as connection, connection.transaction():
                    connection.execute("SET LOCAL session_replication_role = replica")
                    for position, (table, rules) in enumerate(by_table.items()):
                        if database_of(table) == database:
                            self._anonymize(
                                connection, (position, table), rules, transformation, partitioned
                            )
                    if database == info.default:
                        for hook in transformation.hooks:
                            _execute(connection, "an after_transform hook", trusted(hook.sql))
            restored = PostgresRestored(cluster.connect, info.default, partitioned)
            try:
                counts = restored.counts(group.packaged())
            finally:
                restored.close()
        return tuple(TableCount(table=t, rows=n) for t, n in counts.items())

    def _anonymize(
        self,
        connection: Connection,
        numbered: tuple[int, TableName],
        rules: list[PlannedRule],
        transformation: Transformation,
        partitioned: frozenset[TableName],
    ) -> None:
        """Apply every rule for one table in a single UPDATE."""
        position, table = numbered
        assignments: list[sql.Composable] = []
        for number, rule in enumerate(rules):
            column = sql.Identifier(rule.column.column)
            match rule.rule:
                case NullRule():
                    assignments.append(sql.SQL("{} = NULL").format(column))
                case ExpressionRule(expression=expression, where=where):
                    value = sql.SQL("({})").format(trusted(expression))
                    if where:
                        value = sql.SQL("CASE WHEN ({}) THEN {} ELSE {} END").format(
                            trusted(where), value, column
                        )
                    assignments.append(sql.SQL("{} = {}").format(column, value))
                case _:
                    name = sql.Identifier(f"dd_map_{position}_{number}")
                    self._replacements(connection, name, rule, transformation, partitioned)
                    assignments.append(
                        sql.SQL(
                            "{} = (SELECT m.new FROM {} m WHERE m.old = t.{}::text)::{}"
                        ).format(column, name, column, trusted(rule.type))
                    )
        update = sql.SQL("UPDATE {} AS t SET {}").format(
            _table(table, partitioned), _commas(assignments)
        )
        _execute(connection, f"anonymizing {table}", update)

    @staticmethod
    def _replacements(
        connection: Connection,
        name: sql.Identifier,
        rule: PlannedRule,
        transformation: Transformation,
        partitioned: frozenset[TableName],
    ) -> None:
        """Fill a temporary table mapping each original value to its replacement."""
        column = sql.Identifier(rule.column.column)
        distinct = sql.SQL("SELECT DISTINCT {}::text FROM {} WHERE {} IS NOT NULL").format(
            column, _table(rule.column.table, partitioned), column
        )
        try:
            values = [str(row[0]) for row in connection.execute(distinct)]
        except psycopg.Error as error:
            raise database_error(error, f"reading {rule.column}") from None
        mapping = transformation.mapper(rule, values)
        create = sql.SQL(
            "CREATE TEMPORARY TABLE {} (old text PRIMARY KEY, new text) ON COMMIT DROP"
        ).format(name)
        _execute(connection, "preparing replacements", create)
        statement = sql.SQL("COPY {} (old, new) FROM STDIN").format(name)
        with connection.cursor() as cursor, cursor.copy(statement) as copy:
            for old, new in mapping.items():
                copy.write_row((old, new))

    # package and restore

    def package(self, source: Path, image_format: ImageFormat, directory: Path) -> None:
        """Write one format of the image into ``directory``."""
        info = DatabaseInfo.model_validate_json((source / _INFO).read_bytes())
        shutil.copyfile(source / _INFO, directory / _INFO)
        if image_format is ImageFormat.PHYSICAL:
            _copy_tree(source / _DATADIR, directory / _DATADIR)
            return
        cluster = Cluster(self.tools, source / _DATADIR, directory, [directory])
        with cluster.running():
            for position, database in enumerate(info.databases):
                cluster.execute(
                    "pg_dump",
                    [
                        "--format=custom",
                        "--no-owner",
                        "--no-privileges",
                        f"--file={directory / dump_name(position)}",
                        f"--dbname={cluster.local_conninfo(database)}",
                    ],
                )

    def add_accounts(self, image: Path, accounts: Sequence[Account]) -> None:
        """Give the physical image each account as a role that logs in with a password.

        A cluster starts on the image's own data directory, so the roles are
        written into it, and its ``pg_hba.conf``, which ``initdb`` wrote for
        local connections only, gains a password rule for every address. An
        ``all`` account is a superuser. A ``read`` account gets ``CONNECT`` on
        each database, and ``USAGE`` and ``SELECT`` on every table, view and
        sequence of each schema but the system ones; a copy does not change, so
        no default privileges are needed. Unlike ``pg_read_all_data`` this
        leaves ``pg_authid`` and its password hashes out of reach. Passwords
        reach the server only as SCRAM verifiers computed here, so no statement
        the server could log holds one in plain text.
        """
        datadir = image / _DATADIR
        cluster = Cluster(self.tools, datadir, image)
        with cluster.running():
            with cluster.connect(SUPERUSER) as connection:
                for account in accounts:
                    _create_role(connection, account)
                    _check_role(connection, account)
                databases = [
                    str(name)
                    for (name,) in connection.execute(
                        "SELECT datname FROM pg_database"
                        " WHERE datallowconn AND NOT datistemplate ORDER BY datname"
                    ).fetchall()
                ]
            readers = [a.user for a in accounts if a.privileges is AccountPrivileges.READ]
            if readers:
                for database in databases:
                    with cluster.connect(database) as connection:
                        _grant_reading(connection, database, readers)
            endpoint = cluster.server.endpoint
            for account in accounts:
                conninfo = make_conninfo(
                    host=endpoint.host,
                    port=str(endpoint.port),
                    dbname=SUPERUSER,
                    user=account.user,
                )
                try:
                    psycopg.connect(conninfo, password=account.password, connect_timeout=30).close()
                except psycopg.Error as error:
                    action = f"logging in with the image's account {account.user!r}"
                    raise database_error(error, action) from None
        hba = datadir / "pg_hba.conf"
        rule = "host all all all scram-sha-256"
        if rule not in hba.read_text("utf-8").splitlines():
            with hba.open("a", encoding="utf-8") as file:
                file.write(f"# package.accounts: password logins from anywhere\n{rule}\n")

    @contextmanager
    def restore(
        self, image: Path, image_format: ImageFormat, directory: Path
    ) -> Generator[RestoredImage]:
        """Bring an image up in a scratch cluster for checks."""
        info = DatabaseInfo.model_validate_json((image / _INFO).read_bytes())
        datadir = directory / _DATADIR
        directory.mkdir(parents=True, exist_ok=True)
        cluster = Cluster(self.tools, datadir, directory, [image])
        if image_format is ImageFormat.PHYSICAL:
            _copy_tree(image / _DATADIR, datadir)
            datadir.chmod(0o700)
        else:
            cluster.initialize()
        with cluster.running(), ExitStack() as stack:
            if image_format is ImageFormat.LOGICAL:
                for position, database in enumerate(info.databases):
                    cluster.create_database(database)
                    cluster.execute(
                        "pg_restore",
                        [
                            "--no-owner",
                            "--no-privileges",
                            "--exit-on-error",
                            f"--dbname={cluster.local_conninfo(database)}",
                            str(image / dump_name(position)),
                        ],
                    )
            restored = PostgresRestored(cluster.connect, info.default, frozenset(info.partitioned))
            stack.callback(restored.close)
            yield restored


def _copy_tree(source: Path, destination: Path) -> None:
    """Copy a directory, sharing blocks where the filesystem allows."""
    problem = copy_tree(source, destination)
    if problem:
        message = f"cannot copy {source.name}: {problem}"
        raise EngineError(message)


_SCRAM = "SCRAM-SHA-256$"


def _create_role(connection: Connection, account: Account) -> None:
    """Create the account's role or reset it, with a SCRAM verifier for its password."""
    action = f"giving the image the account {account.user!r}"
    exists = connection.execute(
        "SELECT 1 FROM pg_roles WHERE rolname = %s", (account.user,)
    ).fetchone()
    verifier = connection.pgconn.encrypt_password(
        account.password.encode(), account.user.encode(), b"scram-sha-256"
    ).decode()
    read = account.privileges is AccountPrivileges.READ
    attributes = (
        "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
        if read
        else "LOGIN SUPERUSER"
    )
    role = sql.Identifier(account.user)
    _execute(
        connection,
        action,
        sql.SQL("{} ROLE {} WITH {} PASSWORD {}").format(
            sql.SQL("CREATE" if exists is None else "ALTER"),
            role,
            sql.SQL(attributes),
            sql.Literal(verifier),
        ),
    )


def _check_role(connection: Connection, account: Account) -> None:
    """Fail unless the catalog shows the role as declared, with a SCRAM password.

    The login over the network alone does not prove this: the working
    cluster trusts the loopback address, which is where a port-forward arrives.
    """
    read = account.privileges is AccountPrivileges.READ
    row = connection.execute(
        sql.SQL(
            "SELECT rolcanlogin, rolsuper, left(rolpassword, {}) = {}"
            " FROM pg_authid WHERE rolname = %s"
        ).format(sql.Literal(len(_SCRAM)), sql.Literal(_SCRAM)),
        (account.user,),
    ).fetchone()
    if row != (True, not read, True):
        message = (
            f"checking the image's account {account.user!r} failed: the role does not log in"
            f" with a SCRAM password and {account.privileges} privileges"
        )
        raise EngineError(message)


def _grant_reading(connection: Connection, database: str, users: Sequence[str]) -> None:
    """Let ``users`` connect to ``database`` and read everything in its own schemas."""
    action = "granting read access"
    roles = sql.SQL(", ").join(sql.Identifier(user) for user in users)
    _execute(
        connection,
        action,
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), roles),
    )
    schemas = [
        str(name)
        for (name,) in connection.execute(
            "SELECT nspname FROM pg_namespace WHERE left(nspname, 3) <> 'pg_'"
            " AND nspname <> 'information_schema' ORDER BY nspname"
        ).fetchall()
    ]
    for schema in schemas:
        name = sql.Identifier(schema)
        for statement in (
            "GRANT USAGE ON SCHEMA {} TO {}",
            "GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}",
            "GRANT SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}",
        ):
            _execute(connection, action, sql.SQL(statement).format(name, roles))
