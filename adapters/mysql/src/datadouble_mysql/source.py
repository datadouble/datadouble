# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The source side: catalog introspection and one consistent snapshot.

A connection opens ``START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY``;
key reads (strategy B) and every table's rows are read in that transaction.
Rows come back exactly as the server writes them as text, without conversion,
and are written in ``LOAD DATA``'s format. MySQL's catalog is not versioned
with the data, so the schema is read just before the snapshot opens and
checked again inside it; ``mysqldump`` then writes the schema (tables, views
and routines, and triggers separately) from a container of the source's
version. Nothing is written to the source.
"""

import datetime as dt
import time
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pymysql
from pydantic import BaseModel, ConfigDict
from pymysql.connections import Connection
from pymysql.converters import decoders

from datadouble.catalog import Catalog
from datadouble.engine import EngineError, ExtractResult, Snapshot, TableCount, Tooling
from datadouble.names import ColumnName, TableName
from datadouble.planner import Plan
from datadouble.subset import Condition, KeyTuple
from datadouble_mysql.errors import database_error
from datadouble_mysql.introspect import Physical, introspect
from datadouble_mysql.render import Renderer, compose
from datadouble_mysql.server import Tools, checked, default_image

_LOGIN_ENV = "DATADOUBLE_PASSWORD"

_DEPENDENT_VIEWS = """
SELECT DISTINCT VIEW_NAME FROM information_schema.VIEW_TABLE_USAGE
WHERE VIEW_SCHEMA = %s AND TABLE_SCHEMA = %s AND TABLE_NAME IN ({})
"""

_ESCAPES = {
    ord("\\"): b"\\\\",
    ord("\t"): b"\\t",
    ord("\n"): b"\\n",
}


class ExtractedTable(BaseModel):
    """One table's data file in an extract."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    table: TableName
    file: str
    columns: tuple[ColumnName, ...]
    hex: tuple[ColumnName, ...] = ()
    """Columns written as hexadecimal: their bytes need not be text."""
    rows: int


class ExtractIndex(BaseModel):
    """What an extract directory holds, for ``load``."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    database: str
    tables: tuple[ExtractedTable, ...]
    omitted_views: tuple[str, ...]
    """Views that depend on excluded tables, left out of the schema."""

    def table(self, name: TableName) -> ExtractedTable | None:
        """The extracted table called ``name``."""
        return next((t for t in self.tables if t.table == name), None)


def field(value: object) -> bytes:
    r"""One field in ``LOAD DATA``'s format: ``\\N`` for NULL, special bytes escaped."""
    if value is None:
        return b"\\N"
    raw = value if isinstance(value, bytes) else str(value).encode()
    if not any(byte in _ESCAPES for byte in raw):
        return raw
    return b"".join(_ESCAPES.get(byte, bytes((byte,))) for byte in raw)


_RAW_TYPES = frozenset(
    {
        "binary",
        "varbinary",
        "tinyblob",
        "blob",
        "mediumblob",
        "longblob",
        "bit",
        "geometry",
        "point",
        "linestring",
        "polygon",
        "multipoint",
        "multilinestring",
        "multipolygon",
        "geometrycollection",
        "geomcollection",
    }
)


def raw_columns(catalog: Catalog, table: TableName) -> frozenset[ColumnName]:
    """Columns that hold bytes rather than text: binary strings, bits and geometry."""
    found = catalog.table(table)
    return frozenset(
        c.name
        for c in (found.columns if found else ())
        if c.type.split("(")[0].split(" ")[0].lower() in _RAW_TYPES
    )


def loaded_columns(
    catalog: Catalog, physical: Physical, table: TableName
) -> tuple[ColumnName, ...]:
    """The columns extract writes and load reads: all but generated ones."""
    found = catalog.table(table)
    generated = physical.generated.get(table, frozenset())
    return tuple(c.name for c in found.columns if c.name not in generated) if found else ()


class MysqlSnapshot:
    """The snapshot transaction, on a connection that returns values unconverted."""

    def __init__(self, connection: Connection, session: "MysqlSession", as_of: dt.datetime) -> None:
        """Wrap an open snapshot transaction."""
        self.connection = connection
        self.session = session
        self.as_of = as_of
        self.opened = time.monotonic()
        self.catalog, self.physical = introspect(session.connection, session.database)

    def project(
        self, table: TableName, condition: Condition, columns: tuple[ColumnName, ...]
    ) -> Iterable[KeyTuple]:
        """Distinct values of ``columns`` for the chosen rows."""
        query = Renderer(self.as_of).select(table, columns, condition, distinct=True)
        try:
            with self._converted(), self.connection.cursor() as cursor:
                cursor.execute(query.text, query.params)
                return [tuple(row) for row in cursor.fetchall()]
        except pymysql.MySQLError as error:
            raise database_error(error, f"reading keys of {table}") from None

    @contextmanager
    def _converted(self) -> Generator[None]:
        """Convert values to Python types for the block, as keys must be."""
        self.connection.decoders = dict(decoders)
        try:
            yield
        finally:
            self.connection.decoders = {}

    def extract(
        self,
        plan: Plan,
        selections: Mapping[TableName, Condition],
        directory: Path,
        tooling: Tooling,
    ) -> ExtractResult:
        """Write the schema and every extracted table's chosen rows."""
        directory.mkdir(parents=True, exist_ok=True)
        if self.catalog.fingerprint() != plan.catalog_fingerprint:
            message = (
                "the source schema changed between planning and the snapshot; "
                "plan again (start a new generation)"
            )
            raise EngineError(message)
        excluded = plan.excludes.excluded()
        omitted = self._dependent_views(excluded)
        version = self.session.server_version()
        tools = Tools(tooling.runtime, tooling.image or default_image(version))
        self.session.dump_schema(tools, excluded, omitted, directory)
        tables: list[ExtractedTable] = []
        for index, table in enumerate(plan.extracted()):
            names = loaded_columns(self.catalog, self.physical, table)
            raw = raw_columns(self.catalog, table)
            hexed = tuple(n for n in names if n in raw)
            query = Renderer(self.as_of, selections).select(table, names, selections[table])
            file = f"{index:05d}.tsv"
            positions = frozenset(i for i, n in enumerate(names) if n in raw)
            rows = self._write(table, query.text, query.params, directory / file, positions)
            tables.append(
                ExtractedTable(table=table, file=file, columns=names, hex=hexed, rows=rows)
            )
        (directory / "index.json").write_text(
            ExtractIndex(
                database=self.session.database, tables=tuple(tables), omitted_views=omitted
            ).model_dump_json(indent=2),
            "utf-8",
        )
        return ExtractResult(
            server_version=version,
            snapshot=self.session.gtid_executed(),
            snapshot_seconds=round(time.monotonic() - self.opened, 3),
            counts=tuple(TableCount(table=t.table, rows=t.rows) for t in tables),
        )

    def _write(
        self,
        table: TableName,
        query: str,
        params: dict[str, object],
        path: Path,
        hexed: frozenset[int],
    ) -> int:
        rows = 0
        try:
            with self.connection.cursor(pymysql.cursors.SSCursor) as cursor, path.open("wb") as out:
                cursor.execute(query, params)
                for row in cursor:
                    fields = (
                        field(value.hex() if i in hexed and isinstance(value, bytes) else value)
                        for i, value in enumerate(row)
                    )
                    out.write(b"\t".join(fields) + b"\n")
                    rows += 1
        except pymysql.MySQLError as error:
            raise database_error(error, f"extracting {table}") from None
        return rows

    def _dependent_views(self, excluded: frozenset[TableName]) -> tuple[str, ...]:
        """Views that read an excluded table, directly or through other views."""
        database = self.session.database
        pending = sorted(t.name for t in excluded if t.namespace == database)
        found: set[str] = set()
        with self.session.connection.cursor() as cursor:
            while pending:
                names = ", ".join(["%s"] * len(pending))
                statement = compose(_DEPENDENT_VIEWS, names)
                cursor.execute(statement, (database, database, *pending))
                new = {str(row[0]) for row in cursor.fetchall()} - found
                found |= new
                pending = sorted(new)
        return tuple(sorted(found))


class MysqlSession:
    """An open connection to the source database."""

    def __init__(self, connection: Connection, settings: dict[str, str], database: str) -> None:
        """Wrap a connection; ``settings`` (host, port, user, password) are for new ones."""
        self.connection = connection
        self.settings = settings
        self.database = database

    def catalog(self) -> Catalog:
        """Introspect the source."""
        try:
            return introspect(self.connection, self.database)[0]
        except pymysql.MySQLError as error:
            raise database_error(error, "reading the catalog") from None

    def server_version(self) -> str:
        """The source's version, such as ``8.4.13``."""
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT VERSION()")
            row = cursor.fetchone()
        return str(row[0] if row else "").split("-", maxsplit=1)[0]

    def gtid_executed(self) -> str:
        """The source's executed GTID set, which identifies the snapshot; empty without GTIDs."""
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT @@GLOBAL.gtid_executed")
            row = cursor.fetchone()
        return str(row[0]) if row and row[0] else ""

    def dump_schema(
        self,
        tools: Tools,
        excluded: frozenset[TableName],
        omitted_views: tuple[str, ...],
        directory: Path,
    ) -> None:
        """``mysqldump`` the schema (and, separately, the triggers) into ``directory``.

        The source must be reachable from the runtime's containers at the
        address in its URL. Its password reaches ``mysqldump`` as an option
        file on standard input.
        """
        ignored = [
            f"--ignore-table={self.database}.{name}"
            for name in sorted({t.name for t in excluded if t.namespace == self.database})
            + list(omitted_views)
        ]
        common = [
            f"--host={self.settings['host']}",
            f"--port={self.settings['port']}",
            f"--user={self.settings['user']}",
            "--single-transaction",
            "--skip-lock-tables",
            "--no-tablespaces",
            "--set-gtid-purged=OFF",
            "--skip-comments",
            "--skip-dump-date",
            *ignored,
            "--databases",
            self.database,
        ]
        dumps = {
            "schema.sql": ["--no-data", "--skip-triggers", "--routines", "--events"],
            "triggers.sql": ["--no-data", "--no-create-info", "--no-create-db", "--triggers"],
        }
        for name, options in dumps.items():
            script = (
                'printf "[client]\\npassword=%s\\n" "$' + _LOGIN_ENV + '"'
                f' | exec mysqldump --defaults-extra-file=/dev/stdin "$@" > {directory / name}'
            )
            container = tools.container(
                ["sh", "-c", script, "mysqldump", *options, *common], directory.parent, [directory]
            )
            secrets = {_LOGIN_ENV: self.settings["password"]}
            checked("mysqldump", tools.runtime.run(replace(container, secrets=secrets)))

    @contextmanager
    def snapshot(self, as_of: dt.datetime) -> Generator[Snapshot]:
        """Open the snapshot transaction on a connection of its own."""
        try:
            connection = pymysql.connect(
                host=self.settings["host"],
                port=int(self.settings["port"]),
                user=self.settings["user"],
                password=self.settings["password"],
                database=self.database,
                charset="utf8mb4",
            )
        except pymysql.MySQLError as error:
            raise database_error(error, "opening the snapshot") from None
        # Values come back exactly as the server writes them, unconverted.
        connection.decoders = {}
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
            yield MysqlSnapshot(connection, self, as_of)
            connection.rollback()
        except pymysql.MySQLError as error:
            raise database_error(error, "reading the snapshot") from None
        finally:
            connection.close()
