# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Index definitions, and dumps written from and loaded into a cluster.

An index is copied with its mappings, aliases and the settings that describe
the index itself (shards, analysis, sorting, mode). Settings that tie it to
the source cluster are left out: its identity and creation, allocation to
nodes, lifecycle policies, ingest pipelines, which the copy does not have,
and write blocks. Copies have no replicas, so a single node serves them.
"""

from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

from datadouble_elasticsearch.client import Client, Document, Params, as_document, document, path
from datadouble_elasticsearch.documents import Dump, IndexFile, entry, read_entries, write_entries
from datadouble_elasticsearch.search import PointInTime, Query

_DROPPED = (
    "archived.",
    "index.auto_expand_replicas",
    "index.blocks.",
    "index.creation_date",
    "index.default_pipeline",
    "index.downsample.",
    "index.final_pipeline",
    "index.frozen",
    "index.history.uuid",
    "index.lifecycle.",
    "index.number_of_replicas",
    "index.provided_name",
    "index.recovery.",
    "index.resize.",
    "index.rollup.",
    "index.routing.allocation.",
    "index.search.throttled",
    "index.shrink.",
    "index.store.",
    "index.uuid",
    "index.verified_before_close",
    "index.version.",
)
_BATCH = 500
_JSON: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


def portable(settings: Mapping[str, object]) -> dict[str, JsonValue]:
    """The flat settings a copy of an index keeps, with no replicas."""
    kept: dict[str, JsonValue] = {
        key: str(value)
        for key, value in sorted(settings.items())
        if not key.startswith(_DROPPED) and value is not None
    }
    kept["index.number_of_replicas"] = "0"
    return kept


class Definition(BaseModel):
    """An index's mappings, portable settings and aliases."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    mappings: dict[str, JsonValue]
    settings: dict[str, JsonValue]
    aliases: dict[str, JsonValue]


def definition(client: Client, index: str) -> Definition:
    """Read an index's definition."""
    what = f"reading the definition of {index}"

    def part(suffix: str, key: str, params: Params | None = None) -> dict[str, JsonValue]:
        reply = client.request("GET", path(index, suffix), what, params=params)
        return _JSON.validate_python(document(reply.get(index), what).get(key) or {})

    return Definition(
        mappings=part("_mapping", "mappings"),
        settings=portable(part("_settings", "settings", {"flat_settings": True})),
        aliases=part("_alias", "aliases"),
    )


def create(client: Client, file: IndexFile) -> None:
    """Create an index from its definition in a dump."""
    client.request(
        "PUT",
        path(file.index),
        f"creating {file.index}",
        body={"settings": file.settings, "mappings": file.mappings, "aliases": file.aliases},
    )


def operations(index: str, line: Document) -> list[dict[str, JsonValue]]:
    """The two ``_bulk`` lines that index one dump line."""
    action: dict[str, JsonValue] = {"_index": index, "_id": str(line.get("_id"))}
    if (routing := line.get("_routing")) is not None:
        action["routing"] = str(routing)
    return [{"index": action}, _JSON.validate_python(as_document(line.get("_source")) or {})]


def put(client: Client, index: str, lines: Iterable[Document], action: str) -> int:
    """Index dump lines into ``index`` in batches; returns how many."""
    batch: list[dict[str, JsonValue]] = []
    count = 0
    for line in lines:
        batch.extend(operations(index, line))
        count += 1
        if len(batch) >= 2 * _BATCH:
            client.bulk(batch, action)
            batch = []
    client.bulk(batch, action)
    return count


def refresh(client: Client, indices: Iterable[str]) -> None:
    """Make every write to ``indices`` visible to searches."""
    for index in indices:
        client.request("POST", path(index, "_refresh"), f"refreshing {index}")


def load(client: Client, directory: Path, indices: set[str] | None = None) -> None:
    """Create and fill the dump's indices, or only those in ``indices``."""
    dump = Dump.read(directory)
    for file in dump.indices:
        if indices is None or file.index in indices:
            create(client, file)
            put(client, file.index, read_entries(directory / file.file), f"loading {file.index}")
    refresh(client, (f.index for f in dump.indices if indices is None or f.index in indices))


def export(
    pit: PointInTime,
    index: str,
    query: Query,
    target: Path,
    check: Callable[[Document], None] | None = None,
) -> IndexFile:
    """Write an index's definition and the documents ``query`` matches.

    ``check`` sees every hit before it is written.
    """

    def lines() -> Iterable[dict[str, JsonValue]]:
        for hit in pit.hits(index, query):
            if check is not None:
                check(hit)
            yield entry(hit)

    found = definition(pit.client, index)
    count = write_entries(target, lines())
    return IndexFile(
        index=index,
        file=target.name,
        documents=count,
        mappings=found.mappings,
        settings=found.settings,
        aliases=found.aliases,
    )
