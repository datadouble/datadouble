# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The source side: catalog introspection and one consistent snapshot.

A connection opens a transaction under ``SNAPSHOT`` isolation, which the
source database must allow (``ALLOW_SNAPSHOT_ISOLATION ON``); its first read
fixes the snapshot, and key reads (strategy B), the schema script and every
table's rows are read in it. A table whose definition changes while the
snapshot is open fails the read, and the catalog is checked against the plan's
fingerprint. Nothing is written to the source, and no program runs in a
container: the schema is scripted from the catalog views (:mod:`.script`).

Every value is converted to text by the server, in a form it reads back
exactly (binary as hexadecimal, times to 100 ns, floats to 17 digits), and
written as length-prefixed UTF-16 fields, where a length of -1 is NULL; this
keeps empty strings apart from NULL, which SQL Server's CSV reader cannot.
"""

import datetime as dt
import struct
import time
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

import pymssql
from pydantic import BaseModel, ConfigDict
from pymssql import Connection

from datadouble.catalog import Catalog
from datadouble.engine import EngineError, ExtractResult, Snapshot, TableCount, Tooling
from datadouble.names import ColumnName, TableName
from datadouble.planner import Plan
from datadouble.subset import Condition, KeyTuple
from datadouble_mssql.errors import database_error
from datadouble_mssql.introspect import Physical, introspect
from datadouble_mssql.render import OUTER, Renderer, columns, compose, quote
from datadouble_mssql.script import Phase, Schema, script
from datadouble_mssql.server import SESSION, product_version

_NULL = struct.pack("<i", -1)
_BINARY = frozenset(
    {"binary", "varbinary", "image", "timestamp", "geometry", "geography", "hierarchyid"}
)
_TIMES = frozenset({"date", "time", "datetime", "datetime2", "smalldatetime", "datetimeoffset"})
_STYLES = {"float": 3, "real": 3, "money": 2, "smallmoney": 2}


class ExtractedTable(BaseModel):
    """One table's data file in an extract."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    table: TableName
    file: str
    columns: tuple[ColumnName, ...]
    ordinals: tuple[int, ...]
    """Each column's position in the table, from 1; computed columns have none."""
    rows: int


class ExtractIndex(BaseModel):
    """What an extract (or a logical image) holds, for ``load``."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    database: str
    server_version: str
    tables: tuple[ExtractedTable, ...]

    def table(self, name: TableName) -> ExtractedTable | None:
        """The extracted table called ``name``."""
        return next((t for t in self.tables if t.table == name), None)


def as_text(column: str, system_type: str) -> str:
    """An expression reading ``column`` as text the server converts back exactly."""
    if system_type in _BINARY:
        return compose("CONVERT(NVARCHAR(MAX), CAST({} AS VARBINARY(MAX)), 2)", column)
    if system_type in _TIMES:
        return compose("CONVERT(NVARCHAR(40), {}, 126)", column)
    if system_type in _STYLES:
        return compose("CONVERT(NVARCHAR(40), {}, {})", column, str(_STYLES[system_type]))
    return compose("CAST({} AS NVARCHAR(MAX))", column)


def field(value: object) -> bytes:
    """One field: its UTF-16 length in bytes, then the text; -1 for NULL."""
    if value is None:
        return _NULL
    encoded = str(value).encode("utf-16-le")
    return struct.pack("<i", len(encoded)) + encoded


def write_rows(cursor: pymssql.Cursor, out: BinaryIO) -> int:
    """Write every row ``cursor`` returns; return how many."""
    rows = 0
    for row in cursor:
        out.write(b"".join(field(value) for value in row))
        rows += 1
    return rows


def text_columns(physical: Physical, table: TableName) -> list[str]:
    """``SELECT`` expressions for the columns extract writes, over the alias ``t``."""
    types = physical.types.get(table, {})
    return [as_text(columns([name], OUTER), types[name]) for name in physical.loaded(table)]


def extracted_table(physical: Physical, table: TableName, file: str, rows: int) -> ExtractedTable:
    """The index entry for one table's data file."""
    every = list(physical.types.get(table, {}))
    loaded = physical.loaded(table)
    return ExtractedTable(
        table=table,
        file=file,
        columns=loaded,
        ordinals=tuple(every.index(name) + 1 for name in loaded),
        rows=rows,
    )


def schema_for(schema: Schema, excluded: frozenset[TableName]) -> Schema:
    """``schema`` without the statements that belong to excluded tables."""
    kept = tuple(
        statement
        for statement in schema.statements
        if statement.table not in excluded or statement.phase is Phase.SCHEMA
    )
    return schema.model_copy(update={"statements": kept})


class MssqlSnapshot:
    """The snapshot transaction."""

    def __init__(self, connection: Connection, session: "MssqlSession", as_of: dt.datetime) -> None:
        """Wrap an open snapshot transaction, whose first read fixes the snapshot."""
        self.connection = connection
        self.session = session
        self.as_of = as_of
        self.opened = time.monotonic()
        self.catalog, self.physical = introspect(connection)

    def project(
        self, table: TableName, condition: Condition, columns: tuple[ColumnName, ...]
    ) -> Iterable[KeyTuple]:
        """Distinct values of ``columns`` for the chosen rows."""
        query = Renderer(self.as_of).keys(table, columns, condition)
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(query.text, query.params or None)
                return [tuple(row) for row in cursor.fetchall()]
        except pymssql.Error as error:
            raise database_error(error, f"reading keys of {table}") from None

    def extract(
        self,
        plan: Plan,
        selections: Mapping[TableName, Condition],
        directory: Path,
        tooling: Tooling,
    ) -> ExtractResult:
        """Write the schema and every extracted table's chosen rows.

        Nothing runs in a container, so ``tooling`` is not used.
        """
        del tooling
        directory.mkdir(parents=True, exist_ok=True)
        if self.catalog.fingerprint() != plan.catalog_fingerprint:
            message = (
                "the source schema changed between planning and the snapshot; "
                "plan again (start a new generation)"
            )
            raise EngineError(message)
        extracted = plan.extracted()
        refused = [
            f"{table} {self.physical.unsupported[table]}"
            for table in extracted
            if table in self.physical.unsupported
        ]
        if refused:
            message = "cannot extract: " + "; ".join(refused) + "; exclude these tables"
            raise EngineError(message)
        schema = schema_for(script(self.connection), plan.excludes.excluded())
        (directory / "schema.json").write_text(schema.model_dump_json(indent=2), "utf-8")
        tables: list[ExtractedTable] = []
        for index, table in enumerate(extracted):
            query = Renderer(self.as_of, selections).select(
                table, text_columns(self.physical, table), selections[table]
            )
            file = f"{index:05d}.dat"
            try:
                with self.connection.cursor() as cursor, (directory / file).open("wb") as out:
                    cursor.execute(query.text, query.params or None)
                    rows = write_rows(cursor, out)
            except pymssql.Error as error:
                raise database_error(error, f"extracting {table}") from None
            tables.append(extracted_table(self.physical, table, file, rows))
        version = self.session.server_version()
        (directory / "index.json").write_text(
            ExtractIndex(
                database=self.session.database, server_version=version, tables=tuple(tables)
            ).model_dump_json(indent=2),
            "utf-8",
        )
        return ExtractResult(
            server_version=version,
            snapshot=self._sequence_number(),
            snapshot_seconds=round(time.monotonic() - self.opened, 3),
            counts=tuple(TableCount(table=t.table, rows=t.rows) for t in tables),
        )

    def _sequence_number(self) -> str:
        """The snapshot transaction's sequence number; empty without VIEW SERVER STATE."""
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "SELECT transaction_sequence_num FROM sys.dm_tran_current_transaction"
                )
                row = cursor.fetchone()
        except pymssql.Error:
            return ""
        return str(row[0]) if row and row[0] is not None else ""


class MssqlSession:
    """An open connection to the source database."""

    def __init__(self, connection: Connection, settings: dict[str, str], database: str) -> None:
        """Wrap a connection; ``settings`` (server, port, user, password) are for new ones."""
        self.connection = connection
        self.settings = settings
        self.database = database

    def catalog(self) -> Catalog:
        """Introspect the source."""
        try:
            return introspect(self.connection)[0]
        except pymssql.Error as error:
            raise database_error(error, "reading the catalog") from None

    def server_version(self) -> str:
        """The source's version and update level, such as ``16.0.4325.4-CU28``."""
        return product_version(self.connection)

    def _snapshot_allowed(self) -> bool:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT snapshot_isolation_state FROM sys.databases WHERE database_id = DB_ID()"
            )
            row = cursor.fetchone()
        return bool(row and int(str(row[0])) == 1)

    @contextmanager
    def snapshot(self, as_of: dt.datetime) -> Generator[Snapshot]:
        """Open the snapshot transaction on a connection of its own."""
        if not self._snapshot_allowed():
            message = (
                f"the source database {self.database} does not allow snapshot isolation, which"
                " extract reads under; enable it with ALTER DATABASE "
                f"{quote(self.database)} SET ALLOW_SNAPSHOT_ISOLATION ON"
            )
            raise EngineError(message)
        try:
            connection = pymssql.connect(
                server=self.settings["server"],
                port=self.settings["port"],
                user=self.settings["user"],
                password=self.settings["password"],
                database=self.database,
                # The isolation level must be set before the transaction
                # starts, which the driver would otherwise do on connecting.
                autocommit=True,
            )
        except pymssql.Error as error:
            raise database_error(error, "opening the snapshot") from None
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL SNAPSHOT")
                cursor.execute("BEGIN TRANSACTION")
                cursor.execute(SESSION)
            yield MssqlSnapshot(connection, self, as_of)
            with connection.cursor() as cursor:
                cursor.execute("ROLLBACK TRANSACTION")
        except pymssql.Error as error:
            raise database_error(error, "reading the snapshot") from None
        finally:
            connection.close()
