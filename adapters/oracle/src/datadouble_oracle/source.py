# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The source side: catalog introspection and one consistent snapshot.

The schemas are scripted with ``DBMS_METADATA`` just before the snapshot
opens, since it cannot run in a read-only transaction; the snapshot is a
``READ ONLY`` transaction, in which key reads (strategy B), the catalog check
against the plan's fingerprint and every table's rows see the same data.
Nothing is written to the source, and no program runs in a container.

Every value is converted by the server to text it reads back exactly: numbers
in full, dates and timestamps with signed years to the nanosecond, time zones
by region, intervals, and raw bytes as hexadecimal; LOBs are read whole, and
BLOBs written as hexadecimal. Fields are length-prefixed UTF-8, -1 for NULL
(Oracle stores an empty string as NULL, so there is nothing to tell apart).
"""

import datetime as dt
import struct
import time
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from pydantic import BaseModel, ConfigDict

from datadouble.catalog import Catalog
from datadouble.engine import EngineError, ExtractResult, Snapshot, TableCount, Tooling
from datadouble.names import ColumnName, TableName
from datadouble.planner import Plan
from datadouble.subset import Condition, KeyTuple
from datadouble_oracle.driver import Connection, Cursor, Error, connect, read
from datadouble_oracle.errors import database_error
from datadouble_oracle.introspect import Physical, introspect
from datadouble_oracle.render import OUTER, Renderer, columns, compose
from datadouble_oracle.script import Phase, Schema, script

_NULL = struct.pack("<i", -1)
_DATE = "SYYYY-MM-DD HH24:MI:SS"
_TIMESTAMP = "SYYYY-MM-DD HH24:MI:SS.FF9"
_ZONED = "SYYYY-MM-DD HH24:MI:SS.FF9 TZR"
SESSION = (
    "ALTER SESSION SET TIME_ZONE = '+00:00' NLS_NUMERIC_CHARACTERS = '.,'"
    " NLS_LENGTH_SEMANTICS = 'BYTE'"
)
"""Settings every connection starts with, so text forms read back the same."""


class ExtractedTable(BaseModel):
    """One table's data file in an extract."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    table: TableName
    file: str
    columns: tuple[ColumnName, ...]
    types: tuple[str, ...]
    """Each column's type family, for converting its text back."""
    rows: int


class ExtractIndex(BaseModel):
    """What an extract (or a logical image) holds, for ``load``."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    schemas: tuple[str, ...]
    server_version: str
    tables: tuple[ExtractedTable, ...]

    def table(self, name: TableName) -> ExtractedTable | None:
        """The extracted table called ``name``."""
        return next((t for t in self.tables if t.table == name), None)


_TO_TEXT = {
    "NUMBER": "TO_CHAR({}, 'TM9')",
    "FLOAT": "TO_CHAR({}, 'TM9')",
    "BINARY_FLOAT": "TO_CHAR({}, 'TM9')",
    "BINARY_DOUBLE": "TO_CHAR({}, 'TM9')",
    "DATE": f"TO_CHAR({{}}, '{_DATE}')",
    "TIMESTAMP": f"TO_CHAR({{}}, '{_TIMESTAMP}')",
    "TIMESTAMP WITH LOCAL TIME ZONE": f"TO_CHAR({{}}, '{_TIMESTAMP}')",
    "TIMESTAMP WITH TIME ZONE": f"TO_CHAR({{}}, '{_ZONED}')",
    "INTERVAL DAY TO SECOND": "TO_CHAR({})",
    "INTERVAL YEAR TO MONTH": "TO_CHAR({})",
    "RAW": "RAWTOHEX({})",
    "JSON": "JSON_SERIALIZE({} RETURNING CLOB)",
}
_FROM_TEXT = {
    "NUMBER": "TO_NUMBER({})",
    "FLOAT": "TO_NUMBER({})",
    "BINARY_FLOAT": "TO_BINARY_FLOAT({})",
    "BINARY_DOUBLE": "TO_BINARY_DOUBLE({})",
    "DATE": f"TO_DATE({{}}, '{_DATE}')",
    "TIMESTAMP": f"TO_TIMESTAMP({{}}, '{_TIMESTAMP}')",
    "TIMESTAMP WITH LOCAL TIME ZONE": f"TO_TIMESTAMP({{}}, '{_TIMESTAMP}')",
    "TIMESTAMP WITH TIME ZONE": f"TO_TIMESTAMP_TZ({{}}, '{_ZONED}')",
    "INTERVAL DAY TO SECOND": "TO_DSINTERVAL({})",
    "INTERVAL YEAR TO MONTH": "TO_YMINTERVAL({})",
    "RAW": "HEXTORAW({})",
}


def as_text(column: str, family: str) -> str:
    """An expression reading ``column`` as text the server converts back exactly."""
    return compose(_TO_TEXT.get(family, "{}"), column)


def from_text(bind: str, family: str) -> str:
    """An expression converting text written by ``as_text`` back to ``family``."""
    return compose(_FROM_TEXT.get(family, "{}"), bind)


def field(value: object) -> bytes:
    """One field: its UTF-8 length in bytes, then the text; -1 for NULL. Bytes become hex."""
    if value is None:
        return _NULL
    value = read(value)
    text = value.hex() if isinstance(value, bytes) else str(value)
    encoded = text.encode("utf-8")
    return struct.pack("<i", len(encoded)) + encoded


def write_rows(cursor: Cursor, out: BinaryIO) -> int:
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
    loaded = physical.loaded(table)
    types = physical.types.get(table, {})
    return ExtractedTable(
        table=table, file=file, columns=loaded, types=tuple(types[c] for c in loaded), rows=rows
    )


def schema_for(schema: Schema, excluded: frozenset[TableName]) -> Schema:
    """``schema`` without the statements that belong to excluded tables."""
    kept = tuple(
        statement
        for statement in schema.statements
        if statement.table not in excluded or statement.phase is Phase.SCHEMA
    )
    return schema.model_copy(update={"statements": kept})


def configure(connection: Connection) -> Connection:
    """``connection``, with the session settings applied and LOBs read as values."""
    with connection.cursor() as cursor:
        cursor.execute(SESSION)
    return connection


def server_version(connection: Connection) -> str:
    """The server's full version, such as ``23.26.3.0.0``."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT version_full FROM product_component_version WHERE ROWNUM = 1")
        ((version,),) = cursor.fetchall()
    return str(version)


class OracleSnapshot:
    """The read-only snapshot transaction."""

    def __init__(
        self, connection: Connection, session: "OracleSession", as_of: dt.datetime, schema: Schema
    ) -> None:
        """Wrap an open read-only transaction; ``schema`` was scripted just before it."""
        self.connection = connection
        self.session = session
        self.as_of = as_of
        self.schema = schema
        self.opened = time.monotonic()
        self.catalog, self.physical = introspect(connection, session.schemas)

    def project(
        self, table: TableName, condition: Condition, columns: tuple[ColumnName, ...]
    ) -> Iterable[KeyTuple]:
        """Distinct values of ``columns`` for the chosen rows."""
        query = Renderer(self.as_of).keys(table, columns, condition)
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(query.text, query.params)
                return [tuple(row) for row in cursor.fetchall()]
        except Error as error:
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
        schema = schema_for(self.schema, plan.excludes.excluded())
        (directory / "schema.json").write_text(schema.model_dump_json(indent=2), "utf-8")
        tables: list[ExtractedTable] = []
        for index, table in enumerate(extracted):
            query = Renderer(self.as_of, selections).select(
                table, text_columns(self.physical, table), selections[table]
            )
            file = f"{index:05d}.dat"
            try:
                with self.connection.cursor() as cursor, (directory / file).open("wb") as out:
                    cursor.execute(query.text, query.params)
                    rows = write_rows(cursor, out)
            except Error as error:
                raise database_error(error, f"extracting {table}") from None
            tables.append(extracted_table(self.physical, table, file, rows))
        version = server_version(self.connection)
        (directory / "index.json").write_text(
            ExtractIndex(
                schemas=self.session.schemas, server_version=version, tables=tuple(tables)
            ).model_dump_json(indent=2),
            "utf-8",
        )
        return ExtractResult(
            server_version=version,
            snapshot=self._change_number(),
            snapshot_seconds=round(time.monotonic() - self.opened, 3),
            counts=tuple(TableCount(table=t.table, rows=t.rows) for t in tables),
        )

    def _change_number(self) -> str:
        """The system change number the snapshot reads as of; empty without the privilege."""
        try:
            with self.connection.cursor() as cursor:
                cursor.execute("SELECT DBMS_FLASHBACK.GET_SYSTEM_CHANGE_NUMBER FROM dual")
                ((number,),) = cursor.fetchall()
        except Error:
            return ""
        return str(number)


class OracleSession:
    """An open connection to the source."""

    def __init__(
        self, connection: Connection, settings: dict[str, str], schemas: tuple[str, ...]
    ) -> None:
        """Wrap a connection; ``settings`` (user, password, dsn) are for new ones."""
        self.connection = connection
        self.settings = settings
        self.schemas = schemas

    def catalog(self) -> Catalog:
        """Introspect the source."""
        try:
            return introspect(self.connection, self.schemas)[0]
        except Error as error:
            raise database_error(error, "reading the catalog") from None

    @contextmanager
    def snapshot(self, as_of: dt.datetime) -> Generator[Snapshot]:
        """Script the schema, then open the read-only transaction on a connection of its own."""
        try:
            schema = script(self.connection, self.schemas)
            connection = configure(
                connect(self.settings["user"], self.settings["password"], self.settings["dsn"])
            )
        except Error as error:
            raise database_error(error, "opening the snapshot") from None
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
            yield OracleSnapshot(connection, self, as_of, schema)
            connection.rollback()
        except Error as error:
            raise database_error(error, "reading the snapshot") from None
        finally:
            connection.close()
