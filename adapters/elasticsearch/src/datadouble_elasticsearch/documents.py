# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Documents: their field paths, and the files indices are written to.

A column is a field path such as ``address.city``. A document's source can
hold it as nested objects (``{"address": {"city": ...}}``), as a dotted name
(``{"address.city": ...}``) or a mix, and any level can be an array of
values or objects; the helpers here find and rewrite a path in all of these.

Extracts, logical images and the copies between them are dumps: an
``indices.json`` naming each index's mappings, settings and documents file,
and one gzipped NDJSON file per index with each document's ``_id``,
``_routing`` (if any) and ``_source``.
"""

import gzip
import json
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

from datadouble.engine import EngineError
from datadouble_elasticsearch.client import Document, as_document, as_list

DUMP = "indices.json"
_JSON: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


def values(value: object, parts: Sequence[str]) -> Iterator[object]:
    """The non-null values at a field path, with arrays flattened."""
    if (listed := as_list(value)) is not None:
        for item in listed:
            yield from values(item, parts)
        return
    if not parts:
        if value is not None:
            yield value
        return
    found = as_document(value)
    if found is None:
        return
    for end in range(len(parts), 0, -1):
        key = ".".join(parts[:end])
        if key in found:
            yield from values(found[key], parts[end:])


def rewrite(value: object, parts: Sequence[str], change: Callable[[object], object]) -> object:
    """``value`` with each field at a path replaced by ``change`` of it.

    ``change`` gets a field's whole value, an array if the field holds one.
    """
    if not parts:
        return change(value)
    if (listed := as_list(value)) is not None:
        return [rewrite(item, parts, change) for item in listed]
    found = as_document(value)
    if found is None:
        return value
    result = dict(found)
    for end in range(len(parts), 0, -1):
        key = ".".join(parts[:end])
        if key in result:
            result[key] = rewrite(result[key], parts[end:], change)
    return result


def paths(value: object, prefix: str = "") -> Iterator[str]:
    """The field paths a document's source holds values at."""
    if (listed := as_list(value)) is not None:
        for item in listed:
            yield from paths(item, prefix)
        return
    found = as_document(value)
    if found is None or (not found and prefix):
        if prefix:
            yield prefix
        return
    for key, inner in found.items():
        yield from paths(inner, f"{prefix}.{key}" if prefix else key)


def covered(path: str, columns: set[str]) -> bool:
    """Whether a column holds ``path``: the path itself, or an object field above it."""
    parts = path.split(".")
    return any(".".join(parts[:end]) in columns for end in range(1, len(parts) + 1))


class IndexFile(BaseModel):
    """One index in a dump."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    index: str
    file: str
    documents: int
    mappings: dict[str, JsonValue]
    settings: dict[str, JsonValue]
    aliases: dict[str, JsonValue]


class Dump(BaseModel):
    """What a dump directory holds."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    indices: tuple[IndexFile, ...]

    def write(self, directory: Path) -> None:
        """Write ``indices.json``."""
        (directory / DUMP).write_text(self.model_dump_json(indent=2), "utf-8")

    @classmethod
    def read(cls, directory: Path) -> "Dump":
        """Read ``indices.json``."""
        return cls.model_validate_json((directory / DUMP).read_bytes())


def entry(hit: Mapping[str, object]) -> dict[str, JsonValue]:
    """A search hit as a dump line: its ``_id``, ``_routing`` and ``_source``."""
    line: dict[str, JsonValue] = {
        "_id": str(hit.get("_id")),
        "_source": _JSON.validate_python(hit.get("_source") or {}),
    }
    if (routing := hit.get("_routing")) is not None:
        line["_routing"] = str(routing)
    return line


def write_entries(path: Path, entries: Iterable[Mapping[str, JsonValue]]) -> int:
    """Write dump lines to a gzipped NDJSON file; returns how many."""
    count = 0
    with gzip.open(path, "wt", encoding="utf-8") as out:
        for line in entries:
            out.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
            count += 1
    return count


def read_entries(path: Path) -> Iterator[Document]:
    """The dump lines in a gzipped NDJSON file."""
    with gzip.open(path, "rt", encoding="utf-8") as lines:
        for number, line in enumerate(lines, 1):
            found = as_document(json.loads(line))
            if found is None or not isinstance(found.get("_id"), str):
                message = f"{path.name} line {number} is not a dump line"
                raise EngineError(message)
            yield found
