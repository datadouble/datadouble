# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The source side: a catalog of key patterns, and an extract of chosen keys.

The catalog scans every key of the configured databases and places each in
its pattern (see :mod:`datadouble_redis.keys`); a pattern's columns come from
its first ``sample`` keys, and a declared pattern is a table (of the first
database) even before any key matches it. Redis has no transactions to read under, so the
extract reads each chosen key on its own: every key is copied as it was when
read, with ``DUMP``, and keys changed during the extract can disagree with
each other. Keys deleted in the meantime are left out. Copies keep no
expiry, so an image's keys never vanish between building and serving it.
"""

import datetime as dt
import struct
import time
from collections.abc import Generator, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import IO

from pydantic import BaseModel, ConfigDict

from datadouble.catalog import Catalog, Column, ColumnKind, Table
from datadouble.engine import EngineError, ExtractResult, Snapshot, TableCount, Tooling
from datadouble.names import ColumnName, EngineName, TableName
from datadouble.planner import Plan
from datadouble.subset import AllRows, Condition, KeyTuple
from datadouble_redis.driver import Call, Client, Error, failure, items, number, text, texts
from datadouble_redis.keys import (
    KEY,
    Literals,
    Pattern,
    Row,
    glob,
    matches,
    needed,
    place,
    projected,
)

ENGINE = EngineName("redis")
VALUE = ColumnName("value")
MEMBERS = ColumnName("members")
SCORES = ColumnName("scores")
BATCH = 500
"""Keys per round trip."""
_RECORD = struct.Struct(">II")

_KINDS: dict[str, tuple[ColumnKind, str]] = {
    "string": (ColumnKind.TEXT, "string"),
    "hash": (ColumnKind.TEXT, "hash field"),
    "stream": (ColumnKind.TEXT, "stream field"),
    # Elements are text, and rules apply to each; the type names the structure.
    "list": (ColumnKind.TEXT, "list"),
    "set": (ColumnKind.TEXT, "set"),
    "zset": (ColumnKind.TEXT, "zset"),
}


class Layout(BaseModel):
    """How keys are grouped into tables, kept with every extract and image."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    databases: tuple[int, ...]
    separator: str
    patterns: tuple[str, ...]

    def parsed(self) -> tuple[Pattern, ...]:
        """The declared patterns, compiled."""
        return tuple(Pattern.parse(text) for text in self.patterns)


def namespace(database: int) -> str:
    """A database's namespace in table names, such as ``db0``."""
    return f"db{database}"


def database_of(table: TableName) -> int:
    """The database a table's keys live in."""
    return int(table.namespace.removeprefix("db"))


def scan(client: Client, match: str = "*") -> Iterator[bytes]:
    """Every key matching a glob, without blocking the server."""
    cursor = 0
    while True:
        reply = items(client.command("SCAN", cursor, "MATCH", match, "COUNT", 1000))
        cursor = number(reply[0])
        for key in items(reply[1]):
            if isinstance(key, bytes):
                yield key
        if cursor == 0:
            return


def keys_of(client: Client, table: TableName, layout: Layout) -> Iterator[tuple[bytes, str]]:
    """Each key of a table's pattern, with its ``_key``."""
    declared = layout.parsed()
    for key in scan(client, glob(table.name)):
        placed = place(text(key), declared, layout.separator)
        if placed.pattern == table.name:
            yield key, placed.key(layout.separator)


def batches[T](iterable: Iterable[T]) -> Iterator[list[T]]:
    """``iterable`` in lists of ``BATCH`` items, the last perhaps shorter."""
    batch: list[T] = []
    for item in iterable:
        batch.append(item)
        if len(batch) == BATCH:
            yield batch
            batch = []
    if batch:
        yield batch


_READ: dict[str, tuple[str, tuple[str, ...]]] = {
    "string": ("GET", ()),
    "hash": ("HGETALL", ()),
    "list": ("LRANGE", ("0", "-1")),
    "set": ("SMEMBERS", ()),
    "zset": ("ZRANGE", ("0", "-1", "WITHSCORES")),
    "stream": ("XRANGE", ("-", "+")),
}
"""How each type's values are read; other types (modules') are copied unread."""


def read_rows(client: Client, keys: Sequence[tuple[bytes, str]]) -> list[tuple[str, Row]]:
    """Each key's type and values by column; a key deleted meanwhile has none."""
    types = [text(reply) for reply in client.commands([("TYPE", key) for key, _ in keys])]
    calls: list[Call] = []
    for (key, _), kind in zip(keys, types, strict=True):
        command, extra = _READ.get(kind, ("TYPE", ()))
        calls.append((command, key, *extra))
    replies = client.commands(calls)
    return [
        (kind, _row(name, kind, reply))
        for (_, name), kind, reply in zip(keys, types, replies, strict=True)
    ]


def _row(name: str, kind: str, reply: object) -> Row:
    """A key's values by column, from the reply that read it."""
    row: dict[ColumnName, list[str]] = {KEY: [name]}
    match kind:
        case "string":
            row[VALUE] = [text(reply)]
        case "hash":
            pairs = texts(reply)
            for field, value in zip(pairs[::2], pairs[1::2], strict=True):
                row[ColumnName(field)] = [value]
        case "list" | "set":
            row[MEMBERS] = sorted(texts(reply)) if kind == "set" else texts(reply)
        case "zset":
            pairs = texts(reply)
            row[MEMBERS], row[SCORES] = pairs[::2], pairs[1::2]
        case "stream":
            for entry in items(reply):
                fields = texts(items(entry)[1])
                for field, value in zip(fields[::2], fields[1::2], strict=True):
                    row.setdefault(ColumnName(field), []).append(value)
        case _:
            pass
    return row


def _column(name: ColumnName, kind: str) -> Column:
    if name == SCORES:
        return Column(name=name, type="zset scores", kind=ColumnKind.TEXT, nullable=False)
    column_kind, type_name = _KINDS.get(kind, (ColumnKind.OTHER, kind))
    return Column(
        name=name, type=type_name, kind=column_kind, nullable=name not in {VALUE, MEMBERS}
    )


def introspect(clients: dict[int, Client], layout: Layout, sample: int) -> Catalog:
    """One table per key pattern in each database, with columns from sampled keys."""
    declared = layout.parsed()
    literals = Literals(layout.separator)
    sampled: dict[TableName, list[tuple[bytes, str]]] = {}
    for database, client in clients.items():
        for key in scan(client):
            placed = place(text(key), declared, layout.separator)
            literals.add(placed)
            found = sampled.setdefault(TableName.of(namespace(database), placed.pattern), [])
            if len(found) < sample:
                found.append((key, placed.key(layout.separator)))
    literals.check()
    # A declared pattern no key matches yet is a table of the first database.
    present = {table.name for table in sampled}
    for pattern in layout.patterns:
        if pattern not in present:
            sampled[TableName.of(namespace(layout.databases[0]), pattern)] = []
    tables: list[Table] = []
    for table, keys in sorted(sampled.items()):
        columns: dict[ColumnName, tuple[str, set[str]]] = {}
        for kind, row in read_rows(clients[database_of(table)], keys):
            for name in row:
                if name != KEY:
                    columns.setdefault(name, (kind, set()))[1].add(kind)
        described = [
            _column(name, kind if len(kinds) == 1 else "mixed")
            for name, (kind, kinds) in sorted(columns.items())
        ]
        identity = Column(name=KEY, type="key", kind=ColumnKind.TEXT, nullable=False)
        tables.append(Table(name=table, columns=(identity, *described), primary_key=(KEY,)))
    return Catalog(engine=ENGINE, tables=tuple(tables))


def server_version(client: Client) -> str:
    """The server's version, such as ``8.10.3``."""
    for line in text(client.command("INFO", "server")).splitlines():
        if line.startswith("redis_version:"):
            return line.split(":", 1)[1].strip()
    message = "the server did not report its version"
    raise EngineError(message)


class ExtractedTable(BaseModel):
    """One table's keys in an extract."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    table: TableName
    file: str
    keys: int


class ExtractIndex(BaseModel):
    """What an extract directory holds, for ``load``."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    layout: Layout
    tables: tuple[ExtractedTable, ...]


def write_record(out: IO[bytes], key: bytes, dump: bytes) -> None:
    """One key and its ``DUMP`` serialization."""
    out.write(_RECORD.pack(len(key), len(dump)) + key + dump)


def read_records(path: Path) -> Iterator[tuple[bytes, bytes]]:
    """The keys and serializations :func:`write_record` wrote."""
    data = path.read_bytes()
    offset = 0
    while offset < len(data):
        if offset + _RECORD.size > len(data):
            message = f"{path.name} is truncated"
            raise EngineError(message)
        key_length, dump_length = _RECORD.unpack_from(data, offset)
        offset += _RECORD.size
        end = offset + key_length + dump_length
        if end > len(data):
            message = f"{path.name} is truncated"
            raise EngineError(message)
        yield data[offset : offset + key_length], data[offset + key_length : end]
        offset = end


def chosen(
    client: Client,
    table: TableName,
    layout: Layout,
    condition: Condition,
    columns: Iterable[ColumnName],
) -> Iterator[tuple[bytes, Row]]:
    """The keys of a table meeting ``condition``, with the values of ``columns`` read."""
    wanted = (needed(condition) | set(columns)) - {KEY}
    for batch in batches(keys_of(client, table, layout)):
        if wanted:
            rows = [row for _, row in read_rows(client, batch)]
        else:
            rows = [{KEY: [name]} for _, name in batch]
        for (key, _), row in zip(batch, rows, strict=True):
            if matches(condition, row):
                yield key, row


class RedisSnapshot:
    """Reads of the source, each key as it is when read."""

    def __init__(self, source: "RedisSource", as_of: dt.datetime) -> None:
        """Read through ``source``'s clients."""
        self.source = source
        self.as_of = as_of
        self.opened = time.monotonic()

    def project(
        self, table: TableName, condition: Condition, columns: tuple[ColumnName, ...]
    ) -> Iterable[KeyTuple]:
        """Distinct values of ``columns`` for the chosen keys."""
        client = self.source.client(table)
        try:
            rows = (row for _, row in chosen(client, table, self.source.layout, condition, columns))
            return projected(rows, columns)
        except Error as error:
            raise failure(error, f"reading keys of {table}") from None

    def extract(
        self,
        plan: Plan,
        selections: Mapping[TableName, Condition],
        directory: Path,
        tooling: Tooling,  # noqa: ARG002 - extract reads through the client, without tools
    ) -> ExtractResult:
        """Write each chosen key's ``DUMP`` serialization, by table."""
        if self.source.catalog().fingerprint() != plan.catalog_fingerprint:
            message = (
                "the source's key patterns or sampled fields changed between planning and the"
                " extract; plan again (start a new generation)"
            )
            raise EngineError(message)
        directory.mkdir(parents=True, exist_ok=True)
        extracted: list[ExtractedTable] = []
        try:
            for position, table in enumerate(plan.extracted()):
                client = self.source.client(table)
                file = f"{position:05d}.keys"
                count = 0
                with (directory / file).open("wb") as out:
                    keys = chosen(
                        client, table, self.source.layout, selections.get(table, AllRows()), ()
                    )
                    for batch in batches(key for key, _ in keys):
                        for key, dump in zip(
                            batch, client.commands([("DUMP", key) for key in batch]), strict=True
                        ):
                            if isinstance(dump, bytes):
                                write_record(out, key, dump)
                                count += 1
                extracted.append(ExtractedTable(table=table, file=file, keys=count))
            version = server_version(self.source.first)
        except Error as error:
            raise failure(error, "extracting") from None
        index = ExtractIndex(layout=self.source.layout, tables=tuple(extracted))
        (directory / "index.json").write_text(index.model_dump_json(indent=2), "utf-8")
        return ExtractResult(
            server_version=version,
            snapshot="",
            snapshot_seconds=round(time.monotonic() - self.opened, 3),
            counts=tuple(TableCount(table=e.table, rows=e.keys) for e in extracted),
        )


class RedisSource:
    """Open connections to the source's databases."""

    def __init__(self, clients: dict[int, Client], layout: Layout, sample: int) -> None:
        """Wrap one client per database."""
        self.clients = clients
        self.layout = layout
        self.sample = sample

    @property
    def first(self) -> Client:
        """The first configured database's client."""
        return self.clients[self.layout.databases[0]]

    def client(self, table: TableName) -> Client:
        """The client for a table's database."""
        return self.clients[database_of(table)]

    def catalog(self) -> Catalog:
        """Introspect the source."""
        try:
            return introspect(self.clients, self.layout, self.sample)
        except Error as error:
            raise failure(error, "reading the catalog") from None

    @contextmanager
    def snapshot(self, as_of: dt.datetime) -> Generator[Snapshot]:
        """Reads of the source; Redis has no snapshot to open."""
        yield RedisSnapshot(self, as_of)

    def close(self) -> None:
        """Close every connection."""
        for client in self.clients.values():
            client.close()
