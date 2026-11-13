# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The scratch side: working databases, anonymization, images and restores.

``load`` restores the group's keys into a fresh server with ``RESTORE``,
applies the fallbacks for missing parents and saves. ``transform`` copies the
saved ``dump.rdb`` and anonymizes the copy:

- value rules rewrite a hash field or a string, and rebuild a list, set,
  sorted set or stream with each element mapped (a stream keeps its entry
  IDs, though not its consumer groups);
- null rules remove a hash field, empty a string, and empty a list, set or
  sorted set;
- expression rules run the rule's Lua for each key, with ``KEYS[1]`` the key
  and ``ARGV[1]`` the column, where ``where`` (a glob over ``_key``) matches.

``_key`` is a key column, which rules never change, as in every engine.

A physical image is the ``dump.rdb``; there is no logical format.
"""

import shutil
from collections.abc import Generator, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from datadouble.engine import EngineError, RestoredImage, TableCount, Transformation
from datadouble.names import ColumnName, ColumnRef, TableName
from datadouble.planner import Fixup, Plan, PlannedGroup, PlannedRule
from datadouble.spec import ExpressionRule, Hook, ImageFormat, NullRule
from datadouble.subset import AllRows, Edge, EdgeMode, Predicate
from datadouble_redis.driver import Call, Client, Error, failure, items, number, raw
from datadouble_redis.keys import KEY, PLACEHOLDER, Row, matches, projected
from datadouble_redis.server import RDB, RedisServer, Tools
from datadouble_redis.source import (
    SCORES,
    ExtractIndex,
    Layout,
    batches,
    database_of,
    keys_of,
    read_records,
    read_rows,
)

_INFO = "database.json"
_DATA = "data"


class DatabaseInfo(BaseModel):
    """Stored beside a working database and in every image."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    layout: Layout
    server_version: str


def server_databases(layout: Layout) -> int:
    """How many databases a server needs to hold the layout's."""
    return max(16, max(layout.databases) + 1)


def fill(pattern: str, value: str, separator: str) -> str:
    """The key of ``pattern`` whose ``_key`` is ``value``."""
    count = pattern.count(PLACEHOLDER)
    parts = value.split(separator, count - 1) if count > 1 else [value]
    if len(parts) != count:
        message = f"a key of {pattern} needs {count} values in its _key"
        raise EngineError(message)
    pieces = pattern.split(PLACEHOLDER)
    return "".join(piece + part for piece, part in zip(pieces, [*parts, ""], strict=True))


class Keyspace:
    """The keys of one working server, by table."""

    def __init__(self, server: RedisServer, layout: Layout) -> None:
        """Read the running ``server``'s keys, grouped by ``layout``."""
        self.server = server
        self.layout = layout
        self._clients: dict[int, Client] = {}

    def client(self, table: TableName) -> Client:
        """A client for the table's database, opened once."""
        database = database_of(table)
        if database not in self._clients:
            self._clients[database] = self.server.client(database)
        return self._clients[database]

    def keys(self, table: TableName) -> Iterator[tuple[bytes, str]]:
        """Each key of the table, with its ``_key``."""
        return keys_of(self.client(table), table, self.layout)

    def rows(self, table: TableName) -> Iterator[tuple[bytes, str, Row]]:
        """Each key of the table, with its type and values."""
        for batch in batches(self.keys(table)):
            for (key, _), (kind, row) in zip(
                batch, read_rows(self.client(table), batch), strict=True
            ):
                yield key, kind, row

    def close(self) -> None:
        """Close every client."""
        for client in self._clients.values():
            client.close()


class RedisRestored:
    """A database open for checks: a restored image, or a working database."""

    def __init__(self, database: Keyspace) -> None:
        """Wrap a working server's keys."""
        self.database = database

    def counts(self, tables: Iterable[TableName]) -> dict[TableName, int]:
        """Keys per table."""
        try:
            return {table: sum(1 for _ in self.database.keys(table)) for table in tables}
        except Error as error:
            raise failure(error, "counting") from None

    def orphans(self, edge: Edge) -> int:
        """Child keys with a reference (or element) that has no parent."""
        try:
            parents = projected(
                (row for _, _, row in self.database.rows(edge.parent)), edge.parent_columns
            )
            condition = Predicate(edge.when) if edge.when else AllRows()
            return sum(
                1
                for _, _, row in self.database.rows(edge.child)
                if matches(condition, row)
                and any(
                    None not in combination and combination not in parents
                    for combination in projected([row], edge.columns)
                )
            )
        except Error as error:
            raise failure(error, f"checking {edge.name}") from None

    def text_values(self, column: ColumnRef) -> Iterator[str]:
        """Distinct values of a column."""
        try:
            seen: set[str] = set()
            for _, _, row in self.database.rows(column.table):
                for value in row.get(column.column, ()):
                    if value not in seen:
                        seen.add(value)
                        yield value
        except Error as error:
            raise failure(error, f"scanning {column}") from None

    def failing_rows(self, query: str) -> int:
        """The integer a Lua assertion returns, run against the first database."""
        table = TableName.of(f"db{self.database.layout.databases[0]}", "")
        try:
            return number(self.database.client(table).command("EVAL", query, 0))
        except Error as error:
            raise failure(error, "running an assertion") from None


def _copy(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source / RDB, destination / RDB)


class RedisWorking:
    """Working databases and images under one scratch directory."""

    def __init__(self, tools: Tools, scratch: Path) -> None:
        """Use ``tools`` for every server under ``scratch``."""
        self.tools = tools
        self.scratch = scratch
        self._version: str | None = None

    def server_version(self) -> str:
        """The version of the image's ``redis-server``."""
        if self._version is None:
            self._version = self.tools.version(self.scratch)
        return self._version

    @contextmanager
    def _open(self, data: Path, owner: Path, layout: Layout) -> Generator[Keyspace]:
        server = RedisServer(self.tools, data, owner, server_databases(layout))
        with server.running():
            database = Keyspace(server, layout)
            try:
                yield database
                server.save()
            finally:
                database.close()

    def load(
        self,
        plan: Plan,  # noqa: ARG002 - the protocol passes the plan; restoring needs only the group
        group: PlannedGroup,
        extract: Path,
        directory: Path,
        hooks: tuple[Hook, ...],
    ) -> tuple[TableCount, ...]:
        """Restore the group's keys and apply its fallbacks."""
        if hooks:
            message = "the redis adapter does not run hooks"
            raise EngineError(message)
        index = ExtractIndex.model_validate_json((extract / "index.json").read_bytes())
        members = set(group.tables)
        with self._open(directory / _DATA, directory, index.layout) as database:
            try:
                for extracted in index.tables:
                    if extracted.table in members:
                        _restore(database.client(extracted.table), extract / extracted.file)
                for fixup in group.fixups:
                    apply_fixup(database, fixup)
            except Error as error:
                raise failure(error, "loading") from None
            counts = RedisRestored(database).counts(group.tables)
        info = DatabaseInfo(layout=index.layout, server_version=self.server_version())
        (directory / _INFO).write_text(info.model_dump_json(indent=2), "utf-8")
        return tuple(TableCount(table=t, rows=n) for t, n in counts.items())

    def transform(
        self,
        group: PlannedGroup,
        source: Path,
        directory: Path,
        transformation: Transformation,
    ) -> tuple[TableCount, ...]:
        """Copy the working database and anonymize the copy."""
        if transformation.hooks:
            message = "the redis adapter does not run hooks"
            raise EngineError(message)
        _copy(source / _DATA, directory / _DATA)
        shutil.copyfile(source / _INFO, directory / _INFO)
        info = DatabaseInfo.model_validate_json((directory / _INFO).read_bytes())
        with self._open(directory / _DATA, directory, info.layout) as database:
            for rule in transformation.rules:
                try:
                    _anonymize(database, rule, transformation)
                except Error as error:
                    raise failure(error, f"anonymizing {rule.column}") from None
            counts = RedisRestored(database).counts(group.tables)
        return tuple(TableCount(table=t, rows=n) for t, n in counts.items())

    def package(self, source: Path, image_format: ImageFormat, directory: Path) -> None:
        """Write the image: the ``dump.rdb`` and the database's description."""
        if image_format is not ImageFormat.PHYSICAL:
            message = "the redis adapter writes only physical images"
            raise EngineError(message)
        shutil.copyfile(source / _INFO, directory / _INFO)
        _copy(source / _DATA, directory / _DATA)

    @contextmanager
    def restore(
        self, image: Path, image_format: ImageFormat, directory: Path
    ) -> Generator[RestoredImage]:
        """Bring an image up in a scratch server for checks."""
        if image_format is not ImageFormat.PHYSICAL:
            message = "the redis adapter writes only physical images"
            raise EngineError(message)
        info = DatabaseInfo.model_validate_json((image / _INFO).read_bytes())
        _copy(image / _DATA, directory / _DATA)
        with self._open(directory / _DATA, directory, info.layout) as database:
            yield RedisRestored(database)


def _restore(client: Client, path: Path) -> None:
    for batch in batches(read_records(path)):
        client.commands([("RESTORE", key, 0, dump, "REPLACE") for key, dump in batch])


def apply_fixup(database: Keyspace, fixup: Fixup) -> None:
    """Remove references to missing parents, or add stub parents for them."""
    edge = fixup.edge
    if edge.parent_columns != (KEY,) or len(edge.columns) != 1:
        message = f"{edge.name}: a Redis reference names one column and the parent's _key"
        raise EngineError(message)
    column = edge.columns[0]
    present = (
        {name for _, name in database.keys(edge.parent)} if fixup.parent_present else set[str]()
    )
    condition = Predicate(edge.when) if edge.when else AllRows()
    missing: set[str] = set()
    client = database.client(edge.child)
    for key, kind, row in list(database.rows(edge.child)):
        if not matches(condition, row):
            continue
        absent = [value for value in row.get(column, ()) if value not in present]
        if not absent:
            continue
        missing.update(absent)
        if fixup.mode is EdgeMode.NULL:
            client.commands([_unset(key, kind, column, value, edge) for value in absent])
    if fixup.mode is EdgeMode.STUB:
        parent = database.client(edge.parent)
        sep = database.layout.separator
        parent.commands(
            [("SET", raw(fill(edge.parent.name, value, sep)), "") for value in sorted(missing)]
        )


def _unset(key: bytes, kind: str, column: ColumnName, value: str, edge: Edge) -> Call:
    match kind, column:
        case "hash", _ if column != KEY:
            return ("HDEL", key, raw(column))
        case "string", "value":
            return ("DEL", key)
        case "list", "members":
            return ("LREM", key, 0, raw(value))
        case "set", "members":
            return ("SREM", key, raw(value))
        case "zset", "members":
            return ("ZREM", key, raw(value))
        case _:
            message = f"{edge.name}: Redis cannot null a reference held in {kind} {column}"
            raise EngineError(message)


def _anonymize(database: Keyspace, rule: PlannedRule, transformation: Transformation) -> None:
    table, column = rule.column.table, rule.column.column
    client = database.client(table)
    match rule.rule:
        case ExpressionRule(expression=expression, where=where):
            condition = Predicate(where) if where else AllRows()
            calls: list[Call] = [
                ("EVAL", expression, 1, key, raw(column))
                for key, _, row in database.rows(table)
                if matches(condition, row)
            ]
            client.commands(calls)
        case NullRule():
            client.commands(
                [_cleared(key, kind, column, row) for key, kind, row in database.rows(table)]
            )
        case _:
            rows = list(database.rows(table))
            originals = sorted({value for _, _, row in rows for value in row.get(column, ())})
            mapping = transformation.mapper(rule, originals)
            rewrite = _Rewrite(table, column, mapping, database.layout.separator)
            for batch in batches(rows):
                client.commands(
                    [
                        call
                        for key, kind, row in batch
                        for call in (
                            _stream(client, key, column, mapping)
                            if kind == "stream" and column in row
                            else rewrite.calls(key, kind, row)
                        )
                    ]
                )


def _cleared(key: bytes, kind: str, column: ColumnName, row: Row) -> Call:
    if column not in row:
        return ("EXISTS", key)
    match kind, column:
        case "hash", _ if column != KEY:
            return ("HDEL", key, raw(column))
        case "string", "value":
            return ("SET", key, "", "KEEPTTL")
        case (("list" | "set" | "zset"), "members"):
            return ("DEL", key)
        case _:
            message = f"a null rule cannot clear {kind} {column}; use an expression rule"
            raise EngineError(message)


@dataclass(frozen=True, slots=True)
class _Rewrite:
    """A value rule's mapping, applied to one table's keys."""

    table: TableName
    column: ColumnName
    mapping: Mapping[str, str]
    separator: str

    def calls(self, key: bytes, kind: str, row: Row) -> list[Call]:
        """The commands that store the key's values mapped."""
        values = row.get(self.column)
        if not values:
            return []
        mapped = [raw(self.mapping.get(value, value)) for value in values]
        match kind, self.column:
            case "string", "value":
                return [("SET", key, mapped[0], "KEEPTTL")]
            case "hash", _:
                return [("HSET", key, raw(self.column), mapped[0])]
            case (("list" | "set"), "members"):
                return [("DEL", key), ("RPUSH" if kind == "list" else "SADD", key, *mapped)]
            case "zset", "members":
                scores = [raw(score) for score in row.get(SCORES, ())]
                pairs = [item for pair in zip(scores, mapped, strict=True) for item in pair]
                return [("DEL", key), ("ZADD", key, *pairs)]
            case _:
                message = (
                    f"a value rule cannot rewrite {kind} {self.column}; use an expression rule"
                )
                raise EngineError(message)


def _stream(
    client: Client, key: bytes, column: ColumnName, mapping: Mapping[str, str]
) -> list[Call]:
    """The stream rebuilt with one field mapped in every entry, keeping entry IDs."""
    calls: list[Call] = [("DEL", key)]
    for entry in items(client.command("XRANGE", key, "-", "+")):
        identifier, fields = items(entry)
        pairs = items(fields)
        rewritten: list[bytes] = []
        for name, value in zip(pairs[::2], pairs[1::2], strict=True):
            field, current = _bytes(name), _bytes(value)
            if field == raw(column):
                original = current.decode("utf-8", "surrogateescape")
                current = raw(mapping.get(original, original))
            rewritten += [field, current]
        calls.append(("XADD", key, _bytes(identifier), *rewritten))
    return calls


def _bytes(reply: object) -> bytes:
    return reply if isinstance(reply, bytes) else str(reply).encode()
