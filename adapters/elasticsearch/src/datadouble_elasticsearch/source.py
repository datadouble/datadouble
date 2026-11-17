# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The source side: the catalog, and one point in time.

Each index the connection's ``indices`` patterns match is a table, keyed by
``_id``. Its columns are the field paths its mapping declares (objects and
nested fields flattened to dotted paths; multi-fields and aliases, which
hold no values of their own, left out). An index whose mapping lets
documents carry fields it does not declare (``dynamic: false`` or
``runtime``) also gets a column for each such field seen in its first
``sample`` documents, and the extract fails if a document it copies holds a
field the catalog does not list, so strict mode covers every field that
leaves the source.

The extract reads through one point in time and writes a dump (see
:mod:`datadouble_elasticsearch.documents`).
"""

import datetime as dt
import hashlib
import itertools
import time
from collections import defaultdict
from collections.abc import Generator, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from datadouble.catalog import Catalog, Column, ColumnKind, Requirement, RequirementKind, Table
from datadouble.engine import EngineError, ExtractResult, Snapshot, TableCount, Tooling
from datadouble.names import ColumnName, TableName
from datadouble.planner import Plan
from datadouble.subset import Condition, KeyTuple
from datadouble_elasticsearch.client import (
    Client,
    Document,
    as_document,
    document,
    items,
    path,
    text,
)
from datadouble_elasticsearch.documents import Dump, IndexFile, covered, paths, values
from datadouble_elasticsearch.indices import export
from datadouble_elasticsearch.search import ID, PointInTime, query

UNMAPPED = "unmapped"
"""The type of a column for a field the mapping does not declare."""

_KINDS: Mapping[str, ColumnKind] = {
    **dict.fromkeys(
        (
            "keyword",
            "constant_keyword",
            "wildcard",
            "text",
            "match_only_text",
            "search_as_you_type",
            "version",
        ),
        ColumnKind.TEXT,
    ),
    **dict.fromkeys(("long", "integer", "short", "byte", "unsigned_long"), ColumnKind.INTEGER),
    **dict.fromkeys(("double", "float", "half_float"), ColumnKind.FLOAT),
    "scaled_float": ColumnKind.DECIMAL,
    "boolean": ColumnKind.BOOLEAN,
    **dict.fromkeys(("date", "date_nanos"), ColumnKind.TIMESTAMP),
    "binary": ColumnKind.BINARY,
    **dict.fromkeys(("object", "nested", "flattened"), ColumnKind.JSON),
}
_VALUE_KINDS: tuple[tuple[type, ColumnKind], ...] = (
    (bool, ColumnKind.BOOLEAN),
    (int, ColumnKind.INTEGER),
    (float, ColumnKind.FLOAT),
    (str, ColumnKind.TEXT),
)
_OPEN = frozenset({"false", "runtime"})
"""``dynamic`` values that let documents hold fields the mapping does not declare."""


@dataclass(frozen=True, slots=True)
class Layout:
    """Which indices are the source's tables."""

    indices: tuple[str, ...]
    sample: int


def plugins(client: Client) -> tuple[Requirement, ...]:
    """The plugins installed on the cluster's nodes (``GET _nodes/plugins``), with versions."""
    reply = client.request("GET", "/_nodes/plugins", "listing the plugins")
    found: set[Requirement] = set()
    for node in document(reply.get("nodes"), "the nodes").values():
        for plugin in items(document(node, "a node").get("plugins"), "a node's plugins"):
            described = document(plugin, "a plugin")
            found.add(
                Requirement(
                    kind=RequirementKind.PLUGIN,
                    name=text(described.get("name")),
                    version=text(described.get("version")),
                )
            )
    return tuple(sorted(found, key=lambda r: (r.name, r.version or "")))


def server_version(client: Client) -> str:
    """The server's version, such as ``9.5.5``; refuses another product's server."""
    reply = client.request("GET", "/", "reading the server version")
    version = document(reply.get("version"), "the server version")
    product = client.product
    found = version.get("distribution", "elasticsearch")
    if found != product.distribution:
        message = f"the server is {found}, not {product.name}; use the {found} engine"
        raise EngineError(message)
    number = version.get("number")
    if not isinstance(number, str):
        message = "the server did not report its version"
        raise EngineError(message)
    return number


def mapped(properties: Mapping[str, object], prefix: str = "") -> Iterator[tuple[str, str]]:
    """The field paths and types a mapping's ``properties`` declare."""
    for name, spec in properties.items():
        field = as_document(spec) or {}
        kind = str(field.get("type", "object"))
        inner = as_document(field.get("properties"))
        if kind == "alias":
            continue
        if inner and kind in {"object", "nested"}:
            yield from mapped(inner, f"{prefix}{name}.")
        else:
            yield f"{prefix}{name}", kind


def is_open(mappings: Mapping[str, object]) -> bool:
    """Whether documents can hold fields the mapping does not declare."""
    if str(mappings.get("dynamic", "true")) in _OPEN:
        return True
    properties = as_document(mappings.get("properties")) or {}
    return any(is_open(as_document(field) or {}) for field in properties.values())


def _source_kept(index: str, mappings: Mapping[str, object]) -> None:
    source = as_document(mappings.get("_source")) or {}
    if source.get("enabled") is False or source.get("includes") or source.get("excludes"):
        message = (
            f"{index} does not keep whole documents in _source, so they cannot be copied;"
            " leave it out of `indices`"
        )
        raise EngineError(message)


def resolve(client: Client, patterns: Iterable[str]) -> list[str]:
    """The open indices the patterns name, but for system indices; fails on data streams."""
    reply = client.request(
        "GET",
        path("_resolve", "index", ",".join(patterns)),
        "listing indices",
        params={"expand_wildcards": "open", "ignore_unavailable": True, "allow_no_indices": True},
    )
    if items(reply.get("data_streams", []), "data streams"):
        message = (
            "the `indices` patterns match data streams, which this adapter does not copy;"
            " leave them out with a pattern such as -logs-*"
        )
        raise EngineError(message)
    names = (
        str(document(index, "an index").get("name"))
        for index in items(reply.get("indices", []), "indices")
    )
    # Names starting with a dot belong to the server, such as OpenSearch's security index.
    return sorted(name for name in names if not name.startswith("."))


def table_columns(
    index: str, mappings: Mapping[str, object], sampled: Iterable[Document]
) -> tuple[Column, ...]:
    """An index's columns: ``_id``, its declared fields, then fields only documents hold."""
    declared = dict(mapped(as_document(mappings.get("properties")) or {}))
    seen: defaultdict[str, set[ColumnKind]] = defaultdict(set)
    for hit in sampled:
        source = as_document(hit.get("_source")) or {}
        for found in paths(source):
            if not covered(found, set(declared)):
                seen[found].update(_kind(value) for value in values(source, found.split(".")))
    _source_kept(index, mappings)
    return (
        Column(name=ID, type="_id", kind=ColumnKind.TEXT, nullable=False),
        *(
            Column(
                name=ColumnName(name),
                type=kind,
                kind=_KINDS.get(kind, ColumnKind.OTHER),
                nullable=True,
            )
            for name, kind in sorted(declared.items())
        ),
        *(
            Column(
                name=ColumnName(name),
                type=UNMAPPED,
                kind=kinds.pop() if len(kinds) == 1 else ColumnKind.OTHER,
                nullable=True,
            )
            for name, kinds in sorted(seen.items())
        ),
    )


def _kind(value: object) -> ColumnKind:
    """The kind of a value in a document's source, for a field no mapping declares."""
    for kind_type, kind in _VALUE_KINDS:
        if isinstance(value, kind_type):
            return kind
    return ColumnKind.JSON


def introspect(client: Client, layout: Layout) -> Catalog:
    """Read the catalog: one table per index."""
    tables: list[Table] = []
    for index in resolve(client, layout.indices):
        what = f"reading the mapping of {index}"
        reply = client.request("GET", path(index, "_mapping"), what)
        mappings = document(document(reply.get(index), what).get("mappings") or {}, what)
        sampled: list[object] = []
        if is_open(mappings):
            found = client.request(
                "POST",
                path(index, "_search"),
                f"sampling {index}",
                body={"size": layout.sample, "sort": ["_doc"], "track_total_hits": False},
            )
            sampled = items(document(found.get("hits"), "search hits").get("hits"), "hits")
        columns = table_columns(index, mappings, (document(hit, "a hit") for hit in sampled))
        tables.append(Table(name=TableName.of("", index), columns=columns, primary_key=(ID,)))
    return Catalog(engine=client.product.engine, tables=tuple(tables), requirements=plugins(client))


def held(hit: Mapping[str, object], column: ColumnName) -> list[str]:
    """The values, as text, a hit holds in ``column``."""
    if column == ID:
        return [str(hit.get("_id"))]
    return [
        text(value) for value in values(as_document(hit.get("_source")) or {}, column.split("."))
    ]


def keys(hit: Mapping[str, object], columns: tuple[ColumnName, ...]) -> Iterator[KeyTuple]:
    """The key tuples a hit holds, as text; an array yields one per element."""
    return itertools.product(*(held(hit, column) for column in columns))


def _fields(catalog: Catalog, table: TableName) -> dict[ColumnName, str]:
    found = catalog.table(table)
    return {column.name: column.type for column in found.columns} if found else {}


class ElasticSnapshot:
    """A point in time on the source."""

    def __init__(self, source: "ElasticSource", pit: PointInTime, as_of: dt.datetime) -> None:
        """Read through ``pit``."""
        self.source = source
        self.pit = pit
        self.as_of = as_of
        self.opened = time.monotonic()
        # Read once the point in time is open, so a change after planning is noticed.
        self.catalog = source.catalog()

    def project(
        self, table: TableName, condition: Condition, columns: tuple[ColumnName, ...]
    ) -> Iterable[KeyTuple]:
        """Distinct values of ``columns`` for the chosen documents."""
        fields = [column for column in columns if column != ID]
        found: set[KeyTuple] = set()
        chosen = query(condition, self.as_of, _fields(self.catalog, table))
        for hit in self.pit.hits(table.name, chosen, fields):
            found.update(keys(hit, columns))
        return found

    def extract(
        self,
        plan: Plan,
        selections: Mapping[TableName, Condition],
        directory: Path,
        tooling: Tooling,  # noqa: ARG002 - extract reads over HTTP, without tools
    ) -> ExtractResult:
        """Write the chosen documents and their indices' definitions as a dump."""
        current = self.catalog
        if current.fingerprint() != plan.catalog_fingerprint:
            message = (
                "the source's indices or mappings changed between planning and the point in"
                " time; plan again (start a new generation)"
            )
            raise EngineError(message)
        directory.mkdir(parents=True, exist_ok=True)
        files: list[IndexFile] = []
        for number, table in enumerate(plan.extracted()):
            found = current.table(table)
            known = {str(column.name) for column in found.columns} if found else set[str]()
            chosen = query(selections[table], self.as_of, _fields(current, table))
            files.append(
                export(
                    self.pit,
                    table.name,
                    chosen,
                    directory / f"{number:05}.ndjson.gz",
                    partial(_listed, table.name, known),
                )
            )
        Dump(indices=tuple(files)).write(directory)
        return ExtractResult(
            server_version=server_version(self.source.client),
            snapshot=hashlib.sha256(self.pit.pit.encode()).hexdigest()[:16],
            snapshot_seconds=round(time.monotonic() - self.opened, 3),
            counts=tuple(
                TableCount(table=TableName.of("", f.index), rows=f.documents) for f in files
            ),
        )


def _listed(index: str, known: set[str], hit: Mapping[str, object]) -> None:
    for found in paths(as_document(hit.get("_source")) or {}):
        if not covered(found, known):
            message = (
                f"a document in {index} holds the field {found}, which the catalog does not"
                " list; plan again, with a larger `sample` if it is rare"
            )
            raise EngineError(message)


class ElasticSource:
    """An open connection to the source cluster."""

    def __init__(self, client: Client, layout: Layout) -> None:
        """Wrap a client."""
        self.client = client
        self.layout = layout

    def server_version(self) -> str:
        """The cluster's version."""
        return server_version(self.client)

    def catalog(self) -> Catalog:
        """Introspect the source."""
        return introspect(self.client, self.layout)

    @contextmanager
    def snapshot(self, as_of: dt.datetime) -> Generator[Snapshot]:
        """Open a point in time; every read inside it sees the indices as they were then."""
        with PointInTime.opened(self.client, self.layout.indices) as pit:
            yield ElasticSnapshot(self, pit, as_of)
