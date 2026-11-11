# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The source side: catalog introspection and one consistent snapshot.

A coordinator connection opens a repeatable-read, read-only transaction and
exports its snapshot. Key reads (strategy B) and every table's rows are read in
that transaction, and ``pg_dump --snapshot`` writes the schema from the same
point in time. Nothing is written to the source.
"""

import datetime as dt
import time
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg import sql
from pydantic import BaseModel, ConfigDict

from datadouble.catalog import Catalog
from datadouble.engine import EngineError, ExtractResult, Snapshot, TableCount
from datadouble.names import ColumnName, TableName
from datadouble.planner import Plan
from datadouble.subset import Condition, KeyTuple
from datadouble_postgres.cluster import Binaries
from datadouble_postgres.errors import database_error
from datadouble_postgres.introspect import Physical, introspect
from datadouble_postgres.render import Renderer, Statement

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


class ExtractIndex(BaseModel):
    """What an extract directory holds, for ``load``."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    database: str
    tables: tuple[ExtractedTable, ...]
    partitioned: tuple[TableName, ...]
    """Partitioned tables, read without ``ONLY``."""
    omitted_views: tuple[TableName, ...]
    """Views that depend on excluded tables, left out of every working database."""
    constraints: tuple[Constraint, ...]
    """Every foreign-key constraint, including those declared only on partitions."""

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
    """The coordinator transaction and its exported snapshot."""

    def __init__(
        self,
        connection: Connection,
        snapshot: str,
        session: "PostgresSession",
        as_of: dt.datetime,
    ) -> None:
        """Wrap an open coordinator transaction."""
        self.connection = connection
        self.snapshot = snapshot
        self.session = session
        self.as_of = as_of
        self.opened = time.monotonic()
        self.catalog, self.physical = introspect(connection)

    def project(
        self, table: TableName, condition: Condition, columns: tuple[ColumnName, ...]
    ) -> Iterable[KeyTuple]:
        """Distinct values of ``columns`` for the chosen rows."""
        query = Renderer(self.catalog, self.physical.partitioned, self.as_of).select(
            table, columns, condition, distinct=True
        )
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(query.text, query.params)
                return [tuple(row) for row in cursor.fetchall()]
        except psycopg.Error as error:
            raise database_error(error, f"reading keys of {table}") from None

    def extract(
        self, plan: Plan, selections: Mapping[TableName, Condition], directory: Path
    ) -> ExtractResult:
        """Write the schema and every extracted table's chosen rows."""
        directory.mkdir(parents=True, exist_ok=True)
        catalog, physical = self.catalog, self.physical
        if catalog.fingerprint() != plan.catalog_fingerprint:
            message = (
                "the source schema changed between planning and the snapshot; "
                "plan again (start a new generation)"
            )
            raise EngineError(message)
        excluded = plan.excludes.excluded()
        self.session.dump_schema(self.snapshot, excluded, directory / "schema.dump")
        tables: list[ExtractedTable] = []
        for index, table in enumerate(plan.extracted()):
            names = copied_columns(catalog, physical, table)
            renderer = Renderer(catalog, physical.partitioned, plan.as_of, selections)
            query = renderer.select(table, names, selections[table])
            file = f"{index:05d}.copy"
            rows = self._copy_out(table, query.text, query.params, directory / file)
            tables.append(ExtractedTable(table=table, file=file, columns=names, rows=rows))
        omitted = self._dependent_views(excluded)
        constraints = self._constraints()
        index_file = ExtractIndex(
            database=self.session.database,
            tables=tuple(tables),
            partitioned=tuple(sorted(physical.partitioned)),
            omitted_views=omitted,
            constraints=constraints,
        )
        (directory / "index.json").write_text(index_file.model_dump_json(indent=2), "utf-8")
        version = str(self.connection.info.parameter_status("server_version") or "")
        return ExtractResult(
            server_version=version.split(" ", maxsplit=1)[0],
            snapshot=self.snapshot,
            snapshot_seconds=round(time.monotonic() - self.opened, 3),
            counts=tuple(TableCount(table=t.table, rows=t.rows) for t in tables),
        )

    def _copy_out(
        self, table: TableName, query: Statement, params: dict[str, object], path: Path
    ) -> int:
        statement = sql.SQL("COPY ({}) TO STDOUT").format(query)
        try:
            with self.connection.cursor() as cursor, path.open("wb") as out:
                with cursor.copy(statement, params) as copy:
                    for chunk in copy:
                        out.write(chunk)
                return cursor.rowcount
        except psycopg.Error as error:
            raise database_error(error, f"extracting {table}") from None

    def _constraints(self) -> tuple[Constraint, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(_FOREIGN_KEYS)
            return tuple(
                Constraint(
                    namespace=str(row[0]),
                    table=str(row[1]),
                    name=str(row[2]),
                    child=TableName.of(str(row[3]), str(row[4])),
                    parent=TableName.of(str(row[5]), str(row[6])),
                )
                for row in cursor.fetchall()
            )

    def _dependent_views(self, excluded: frozenset[TableName]) -> tuple[TableName, ...]:
        if not excluded:
            return ()
        names = [
            sql.Identifier(t.namespace, t.name).as_string(self.connection) for t in sorted(excluded)
        ]
        with self.connection.cursor() as cursor:
            cursor.execute(_DEPENDENT_VIEWS, [names])
            return tuple(TableName.of(str(nsp), str(rel)) for nsp, rel in cursor.fetchall())


def _pattern(table: TableName) -> str:
    """An exact ``pg_dump`` table pattern: each part double-quoted, quotes doubled."""
    return ".".join('"' + part.replace('"', '""') + '"' for part in (table.namespace, table.name))


class PostgresSession:
    """An open connection to the source database."""

    def __init__(
        self, connection: Connection, conninfo: str, password: str | None, binaries: Binaries
    ) -> None:
        """Wrap a connection; ``conninfo`` and ``password`` are for ``pg_dump``."""
        self.connection = connection
        self.conninfo = conninfo
        self.password = password
        self.binaries = binaries
        self.database = str(connection.info.dbname)

    def catalog(self) -> Catalog:
        """Introspect the source."""
        try:
            return introspect(self.connection)[0]
        except psycopg.Error as error:
            raise database_error(error, "reading the catalog") from None

    def dump_schema(self, snapshot: str, excluded: frozenset[TableName], path: Path) -> None:
        """``pg_dump --schema-only`` from the exported snapshot."""
        arguments = [
            "--schema-only",
            "--format=custom",
            f"--snapshot={snapshot}",
            f"--file={path}",
            f"--dbname={self.conninfo}",
        ]
        arguments.extend(f"--exclude-table={_pattern(t)}" for t in sorted(excluded))
        env = {"PGPASSWORD": self.password} if self.password else {}
        self.binaries.run("pg_dump", arguments, env)

    @contextmanager
    def snapshot(self, as_of: dt.datetime) -> Generator[Snapshot]:
        """Open the coordinator transaction and export its snapshot."""
        connection = self.connection
        try:
            with connection.transaction():
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                row = connection.execute("SELECT pg_catalog.pg_export_snapshot()").fetchone()
                yield PostgresSnapshot(connection, str(row[0]) if row else "", self, as_of)
        except psycopg.Error as error:
            raise database_error(error, "reading the snapshot") from None
