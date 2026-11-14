# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Point-in-time searches, and subset conditions as Query DSL.

Every read of a source goes through one point in time opened over all of its
indices, so the whole extract sees the indices as they were when it opened.
Searches page through it with ``search_after``, in ``_shard_doc`` order.

Seeds and ``when`` clauses are Query DSL objects written as JSON; the string
``":as_of"`` anywhere in them stands for the plan's anchor time. Strategy B's
value sets become ``ids`` and ``terms`` queries. No query can evaluate
another index's selection, so strategy A is not offered.
"""

import datetime as dt
import json
from collections.abc import Generator, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from typing import assert_never

from pydantic import JsonValue, TypeAdapter

from datadouble.engine import EngineError
from datadouble.names import ColumnName
from datadouble.subset import (
    AllOf,
    AllRows,
    AnyNull,
    AnyOf,
    Condition,
    NoRows,
    Predicate,
    SelectedBy,
    ValuesIn,
)
from datadouble_elasticsearch.client import Client, Document, as_document, as_list, document, items
from datadouble_elasticsearch.client import path as request_path
from datadouble_elasticsearch.client import text as as_text

type Query = dict[str, JsonValue]

AS_OF = ":as_of"
ID = ColumnName("_id")
ANALYZED = frozenset({"text", "match_only_text", "search_as_you_type", "annotated_text"})
"""Field types whose values are split into terms, so no query matches them exactly."""

_PAGE = 1000
_KEEP = "10m"
_TERMS = 10000
"""Values per ``terms`` query, well under the default ``index.max_terms_count``."""
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_SORT: TypeAdapter[list[JsonValue]] = TypeAdapter(list[JsonValue])
_JSON_OBJECT: TypeAdapter[Query] = TypeAdapter(Query)


class PointInTime:
    """An open point in time on a cluster."""

    def __init__(self, client: Client, pit: str) -> None:
        """Search ``client`` through the point in time ``pit``."""
        self.client = client
        self.pit = pit

    @classmethod
    @contextmanager
    def opened(cls, client: Client, targets: Sequence[str]) -> Generator["PointInTime"]:
        """Open a point in time over the open indices ``targets`` name, for the block."""
        api = client.product.pit
        reply = client.request(
            "POST",
            request_path(",".join(targets), *api.open.split("/")),
            "opening a point in time",
            params={"keep_alive": _KEEP, "expand_wildcards": "open", "ignore_unavailable": True},
        )
        opened = cls(client, str(reply.get(api.key)))
        try:
            yield opened
        finally:
            # A point in time that cannot be closed expires by itself.
            with suppress(EngineError):
                client.request(
                    "DELETE", api.close, "closing a point in time", body=api.close_body(opened.pit)
                )

    def hits(
        self, index: str, query: Query, fields: Sequence[str] | None = None
    ) -> Iterator[Document]:
        """Every hit in ``index`` that ``query`` matches.

        Hits carry the source's ``fields``, all of it if None, none if empty.
        """
        source: JsonValue = True if fields is None else ([*fields] or False)
        after: list[JsonValue] | None = None
        while True:
            body: dict[str, JsonValue] = {
                "pit": {"id": self.pit, "keep_alive": _KEEP},
                "size": _PAGE,
                "query": {"bool": {"filter": [{"term": {"_index": index}}, query]}},
                "sort": ["_shard_doc"],
                "_source": source,
                "track_total_hits": False,
            }
            if after is not None:
                body["search_after"] = after
            reply = self.client.request("POST", "/_search", f"searching {index}", body=body)
            self.pit = str(reply.get("pit_id", self.pit))
            found = items(document(reply.get("hits"), "search hits").get("hits"), "search hits")
            if not found:
                return
            for hit in found:
                yield document(hit, "a search hit")
            after = _SORT.validate_python(document(found[-1], "a hit").get("sort"))


def _bind(value: object, as_of: str) -> object:
    if value == AS_OF:
        return as_of
    if (found := as_document(value)) is not None:
        return {key: _bind(item, as_of) for key, item in found.items()}
    if (listed := as_list(value)) is not None:
        return [_bind(item, as_of) for item in listed]
    return value


def parse(source: str, as_of: dt.datetime, what: str) -> Query:
    """A Query DSL object from the spec, with ``":as_of"`` bound."""
    try:
        parsed: object = json.loads(source)
    except ValueError as error:
        message = f"{what} is not valid JSON: {error}"
        raise EngineError(message) from None
    bound = as_document(_bind(parsed, as_of.isoformat()))
    if bound is None:
        message = f"{what} must be a JSON object"
        raise EngineError(message)
    return {key: _JSON.validate_python(value) for key, value in bound.items()}


def _exact(column: ColumnName, fields: Mapping[ColumnName, str]) -> None:
    if fields.get(column) in ANALYZED:
        message = (
            f"{column} is a {fields[column]} field, which no query matches exactly; references"
            " need keyword, numeric or _id fields"
        )
        raise EngineError(message)


def _terms(column: ColumnName, found: Sequence[str]) -> Query:
    if column == ID:
        return {"ids": {"values": [*found]}}
    return {"terms": {column: [*found]}}


def _values(condition: ValuesIn, fields: Mapping[ColumnName, str]) -> Query:
    for column in condition.columns:
        _exact(column, fields)
    rows = sorted(tuple(as_text(value) for value in row) for row in condition.values)
    if not rows:
        return {"match_none": {}}
    if len(condition.columns) == 1:
        column = condition.columns[0]
        chunks: list[JsonValue] = [
            _terms(column, [row[0] for row in rows[start : start + _TERMS]])
            for start in range(0, len(rows), _TERMS)
        ]
    else:
        chunks = [
            {
                "bool": {
                    "filter": [
                        _terms(column, [value])
                        for column, value in zip(condition.columns, row, strict=True)
                    ]
                }
            }
            for row in rows
        ]
    if len(chunks) == 1:
        return _JSON_OBJECT.validate_python(chunks[0])
    return {"bool": {"should": chunks}}


def query(condition: Condition, as_of: dt.datetime, fields: Mapping[ColumnName, str]) -> Query:
    """The Query DSL for a condition on an index whose fields have the types ``fields``."""
    match condition:
        case AllRows() | NoRows():
            return {"match_all" if isinstance(condition, AllRows) else "match_none": {}}
        case Predicate(source):
            return parse(source, as_of, "a seed or `when` query")
        case ValuesIn():
            return _values(condition, fields)
        case AnyNull(columns):
            return {
                "bool": {
                    "should": [{"bool": {"must_not": {"exists": {"field": c}}}} for c in columns]
                }
            }
        case AnyOf(parts) | AllOf(parts):
            clause = "should" if isinstance(condition, AnyOf) else "filter"
            return {"bool": {clause: [query(part, as_of, fields) for part in parts]}}
        case SelectedBy():
            message = "queries cannot evaluate nested selections; use the keys strategy"
            raise EngineError(message)
        case _:
            assert_never(condition)
