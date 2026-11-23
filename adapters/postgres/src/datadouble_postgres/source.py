# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The source side: catalog introspection and consistent snapshots.

The source is one or more databases on one server; every table is named
``database.schema.table``. In each database a coordinator connection opens a
repeatable-read, read-only transaction and exports its snapshot. Key reads
(strategy B) and every table's rows are read in their database's transaction,
and ``pg_dump --snapshot`` writes each database's schema from the same point
in time. PostgreSQL cannot share a snapshot between databases, so the
transactions are opened one after another, together; no query reads across
databases, so each stays consistent on its own. Nothing is written to the
source.
"""

import datetime as dt
import time
from collections.abc import Generator, Iterable, Mapping
from contextlib import ExitStack, contextmanager
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from pydantic import BaseModel, ConfigDict

from datadouble.catalog import Catalog, Requirement, Table
from datadouble.engine import EngineError, ExtractResult, Snapshot, TableCount, Tooling
from datadouble.names import ColumnName, TableName
from datadouble.planner import Plan
from datadouble.subset import Condition, KeyTuple
from datadouble_postgres.cluster import Tools, default_image
from datadouble_postgres.errors import database_error
from datadouble_postgres.introspect import ENGINE, Extension, Physical, introspect
from datadouble_postgres.render import Renderer, Statement, database_of, unqualified

type Connection = psycopg.Connection[tuple[object, ...]]

_DEPENDENT_VIEWS = """
WITH RECURSIVE dependent(oid) AS (
    SELECT DISTINCT r.ev_class
    FROM pg_catalog.pg_rewrite r
    JOIN pg_catalog.pg_depend d ON d.classid = 'pg_catalog.pg_rewrite'::regclass AND d.objid = r.oid
    WHERE d.refobjid = ANY(%s::regclass[]) AND r.ev_class <> d.refobjid
  UNION
    SELECT r.ev_class
    FROM dependent v
    JOIN pg_catalog.pg_depend d ON d.refobjid = v.oid
    JOIN pg_catalog.pg_rewrite r
      ON d.classid = 'pg_catalog.pg_rewrite'::regclass AND d.objid = r.oid
    WHERE r.ev_class <> v.oid
)
SELECT n.nspname, c.relname
FROM dependent JOIN pg_catalog.pg_class c ON c.oid = dependent.oid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
ORDER BY 1, 2
"""


_FOREIGN_KEYS = """
SELECT n.nspname, c.relname, con.conname,
       rn.nspname, rc.relname, tn.nspname, tc.relname
FROM pg_catalog.pg_constraint con
JOIN pg_catalog.pg_class c ON c.oid = con.conrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_class rc
  ON rc.oid = coalesce(pg_catalog.pg_partition_root(con.conrelid), con.conrelid)
JOIN pg_catalog.pg_namespace rn ON rn.oid = rc.relnamespace
JOIN pg_catalog.pg_class tc
  ON tc.oid = coalesce(pg_catalog.pg_partition_root(con.confrelid), con.confrelid)
JOIN pg_catalog.pg_namespace tn ON tn.oid = tc.relnamespace
WHERE con.contype = 'f'
ORDER BY 1, 2, 3
"""


class Constraint(BaseModel):
    """A foreign-key constraint as ``pg_dump`` writes it, on a table or a partition."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    namespace: str
    """The schema of the table it is declared on."""
    table: str
    name: str
    child: TableName
    """The table it belongs to, or that table's partitioned root."""
    parent: TableName
    """The table it refers to, or that table's partitioned root."""


class ExtractedTable(BaseModel):
    """One table's data file in an extract."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    table: TableName
    file: str
    columns: tuple[ColumnName, ...]
    rows: int


class ExtractedDatabase(BaseModel):
    """One database's schema in an extract."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    name: str
    schema_file: str
    """Its ``pg_dump`` archive, schema only."""
    omitted_views: tuple[TableName, ...]
    """Views that depend on excluded tables, left out of every working database."""
    constraints: tuple[Constraint, ...]
    """Every foreign-key constraint, including those declared only on partitions."""
    extensions: tuple[Extension, ...] = ()
    """Installed extensions, which ``load`` creates at the source's versions."""


class ExtractIndex(BaseModel):
    """What an extract directory holds, for ``load``."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    databases: tuple[ExtractedDatabase, ...]
    default: str
    """The database spec SQL without a table runs in."""
    tables: tuple[ExtractedTable, ...]
    partitioned: tuple[TableName, ...]
    """Partitioned tables, read without ``ONLY``."""

    def table(self, name: TableName) -> ExtractedTable | None:
        """The extracted table called ``name``."""
        return next((t for t in self.tables if t.table == name), None)


def copied_columns(
    catalog: Catalog, physical: Physical, table: TableName
) -> tuple[ColumnName, ...]:
    """The columns COPY reads and writes: all but generated ones."""
    found = catalog.table(table)
    generated = physical.generated.get(table, frozenset())
    return tuple(c.name for c in found.columns if c.name not in generated) if found else ()


class PostgresSnapshot:
    """Each database's coordinator transaction and its exported snapshot."""

    def __init__(
        self, snapshots: dict[str, str], session: "PostgresSession", as_of: dt.datetime
    ) -> None:
        """Wrap the open coordinator transactions, one per database."""
        self.snapshots = snapshots
        self.session = session
        self.as_of = as_of
        self.opened = time.monotonic()
        self.catalog, self.physical = session.introspect()

    def project(
        self, table: TableName, condition: Condition, columns: tuple[ColumnName, ...]
    ) -> Iterable[KeyTuple]:
        """Distinct values of ``columns`` for the chosen rows."""
        query = Renderer(self.catalog, self.physical.partitioned, self.as_of).select(
            table, columns, condition, distinct=True
        )
        try:
            with self.session.connections[database_of(table)].cursor() as cursor:
                cursor.execute(query.text, query.params)
                return [tuple(row) for row in cursor.fetchall()]
        except psycopg.Error as error:
            raise database_error(error, f"reading keys of {table}") from None

    def extract(
        self,
        plan: Plan,
        selections: Mapping[TableName, Condition],
        directory: Path,
        tooling: Tooling,
    ) -> ExtractResult:
        """Write each database's schema and every extracted table's chosen rows."""
        directory.mkdir(parents=True, exist_ok=True)
        catalog, physical = self.catalog, self.physical
        if catalog.fingerprint() != plan.catalog_fingerprint:
            message = (
                "the source schema changed between planning and the snapshot; "
                "plan again (start a new generation)"
            )
            raise EngineError(message)
        excluded = plan.excludes.excluded()
        version = self.session.server_version()
        tools = Tools(
            tooling.runtime, tooling.image or default_image(version), directory.parent / "tools"
        )
        databases: list[ExtractedDatabase] = []
        for position, (database, snapshot) in enumerate(self.snapshots.items()):
            own = frozenset(t for t in excluded if database_of(t) == database)
            schema_file = f"schema-{position:03d}.dump"
            self.session.dump_schema(tools, database, snapshot, own, directory / schema_file)
            connection = self.session.connections[database]
            databases.append(
                ExtractedDatabase(
                    name=database,
                    schema_file=schema_file,
                    omitted_views=_dependent_views(connection, database, own),
                    constraints=_constraints(connection, database),
                    extensions=physical.extensions.get(database, ()),
                )
            )
        tables: list[ExtractedTable] = []
        for index, table in enumerate(plan.extracted()):
            names = copied_columns(catalog, physical, table)
            renderer = Renderer(catalog, physical.partitioned, plan.as_of, selections)
            query = renderer.select(table, names, selections[table])
            file = f"{index:05d}.copy"
            rows = self._copy_out(table, query.text, query.params, directory / file)
            tables.append(ExtractedTable(table=table, file=file, columns=names, rows=rows))
        index_file = ExtractIndex(
            databases=tuple(databases),
            default=self.session.default,
            tables=tuple(tables),
            partitioned=tuple(sorted(physical.partitioned)),
        )
        (directory / "index.json").write_text(index_file.model_dump_json(indent=2), "utf-8")
        return ExtractResult(
            server_version=version,
            snapshot="",
            snapshots=dict(self.snapshots),
            snapshot_seconds=round(time.monotonic() - self.opened, 3),
            counts=tuple(TableCount(table=t.table, rows=t.rows) for t in tables),
        )

    def _copy_out(
        self, table: TableName, query: Statement, params: dict[str, object], path: Path
    ) -> int:
        statement = sql.SQL("COPY ({}) TO STDOUT").format(query)
        try:
            connection = self.session.connections[database_of(table)]
            with connection.cursor() as cursor, path.open("wb") as out:
                with cursor.copy(statement, params) as copy:
                    for chunk in copy:
                        out.write(chunk)
                return cursor.rowcount
        except psycopg.Error as error:
            raise database_error(error, f"extracting {table}") from None


def _constraints(connection: Connection, database: str) -> tuple[Constraint, ...]:
    with connection.cursor() as cursor:
        cursor.execute(_FOREIGN_KEYS)
        return tuple(
            Constraint(
                namespace=str(row[0]),
                table=str(row[1]),
                name=str(row[2]),
                child=TableName.of(database, str(row[3]), str(row[4])),
                parent=TableName.of(database, str(row[5]), str(row[6])),
            )
            for row in cursor.fetchall()
        )


def _dependent_views(
    connection: Connection, database: str, excluded: frozenset[TableName]
) -> tuple[TableName, ...]:
    if not excluded:
        return ()
    names = [unqualified(t).as_string(connection) for t in sorted(excluded)]
    with connection.cursor() as cursor:
        cursor.execute(_DEPENDENT_VIEWS, [names])
        return tuple(TableName.of(database, str(nsp), str(rel)) for nsp, rel in cursor.fetchall())


def _pattern(table: TableName) -> str:
    """An exact ``pg_dump`` table pattern: schema and table double-quoted, quotes doubled."""
    return ".".join('"' + part.replace('"', '""') + '"' for part in table.parts[1:])


class PostgresSession:
    """Open connections to each database the source reads, on one server."""

    def __init__(
        self,
        connections: dict[str, Connection],
        conninfo: str,
        password: str | None,
        default: str,
    ) -> None:
        """Wrap a connection per database; ``conninfo`` and ``password`` are for ``pg_dump``.

        ``default`` is the database spec SQL without a table runs in: the
        connection string's, when it is read, or else the first read.
        """
        self.connections = connections
        self.conninfo = conninfo
        self.password = password
        self.default = default

    @property
    def connection(self) -> Connection:
        """The default database's connection."""
        return self.connections[self.default]

    def server_version(self) -> str:
        """The source's version, such as ``18.1``."""
        version = str(self.connection.info.parameter_status("server_version") or "")
        return version.split(" ", maxsplit=1)[0]

    def introspect(self) -> tuple[Catalog, Physical]:
        """Every database's catalog, merged; requirements are listed once each."""
        tables: list[Table] = []
        requirements: dict[Requirement, None] = {}
        physical = Physical(frozenset(), {}, {})
        for database, connection in self.connections.items():
            catalog, found = introspect(connection, database)
            tables.extend(catalog.tables)
            requirements.update(dict.fromkeys(catalog.requirements))
            physical = physical.merged(found)
        merged = Catalog(engine=ENGINE, tables=tuple(tables), requirements=tuple(requirements))
        return merged, physical

    def catalog(self) -> Catalog:
        """Introspect the source."""
        try:
            return self.introspect()[0]
        except psycopg.Error as error:
            raise database_error(error, "reading the catalog") from None

    def dump_schema(
        self,
        tools: Tools,
        database: str,
        snapshot: str,
        excluded: frozenset[TableName],
        path: Path,
    ) -> None:
        """``pg_dump --schema-only`` of one database from its exported snapshot, into ``path``.

        The source must be reachable from the runtime's containers at the
        address in its connection string.
        """
        arguments = [
            "--schema-only",
            "--format=custom",
            f"--snapshot={snapshot}",
            f"--file={path}",
            f"--dbname={make_conninfo(self.conninfo, dbname=database)}",
        ]
        arguments.extend(f"--exclude-table={_pattern(t)}" for t in sorted(excluded))
        secrets = {"PGPASSWORD": self.password} if self.password else {}
        tools.run("pg_dump", arguments, path.parent.parent, [path.parent], secrets)

    @contextmanager
    def snapshot(self, as_of: dt.datetime) -> Generator[Snapshot]:
        """Open every database's coordinator transaction together and export its snapshot."""
        try:
            with ExitStack() as stack:
                snapshots: dict[str, str] = {}
                for database, connection in self.connections.items():
                    stack.enter_context(connection.transaction())
                    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    row = connection.execute("SELECT pg_catalog.pg_export_snapshot()").fetchone()
                    snapshots[database] = str(row[0]) if row else ""
                yield PostgresSnapshot(snapshots, self, as_of)
        except psycopg.Error as error:
            raise database_error(error, "reading the snapshot") from None
