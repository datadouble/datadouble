# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Turning the core's subset conditions into MongoDB query filters.

Seeds and ``when`` clauses are query filters written as MongoDB Extended JSON;
the string ``":as_of"`` anywhere in them stands for the plan's anchor time.
Strategy B's value sets become ``$in`` lists. MongoDB cannot evaluate another
collection's selection inside a query, so strategy A is not offered.
"""

import datetime as dt
from collections.abc import Hashable, Iterable, Mapping
from typing import assert_never

from bson import json_util
from pydantic import TypeAdapter, ValidationError

from datadouble.engine import EngineError
from datadouble.subset import (
    AllOf,
    AllRows,
    AnyNull,
    AnyOf,
    Condition,
    KeyTuple,
    NoRows,
    Predicate,
    SelectedBy,
    ValuesIn,
)

type Document = dict[str, object]

AS_OF = ":as_of"


_DOCUMENT: TypeAdapter[dict[str, object]] = TypeAdapter(dict[str, object])
_LIST: TypeAdapter[list[object]] = TypeAdapter(list[object])


def as_document(value: object) -> Document | None:
    """``value`` as a document, if it is one (parsed JSON and BSON are untyped)."""
    if not isinstance(value, Mapping):
        return None
    try:
        return _DOCUMENT.validate_python(value)
    except ValidationError:
        return None


def as_list(value: object) -> list[object] | None:
    """``value`` as a list, if it is one."""
    return _LIST.validate_python(value) if isinstance(value, list) else None


def _bind(value: object, as_of: dt.datetime) -> object:
    if value == AS_OF:
        return as_of
    if (document := as_document(value)) is not None:
        return {key: _bind(item, as_of) for key, item in document.items()}
    if (items := as_list(value)) is not None:
        return [_bind(item, as_of) for item in items]
    return value


def parse(text: str, as_of: dt.datetime, what: str) -> Document:
    """A JSON document from the spec, with ``":as_of"`` bound."""
    try:
        parsed: object = json_util.loads(text)
    except ValueError as error:
        message = f"{what} is not valid MongoDB Extended JSON: {error}"
        raise EngineError(message) from None
    document = as_document(_bind(parsed, as_of))
    if document is None:
        message = f"{what} must be a JSON object"
        raise EngineError(message)
    return document


def _values(condition: ValuesIn) -> Document:
    if not condition.values:
        return {"$expr": False}
    columns = condition.columns
    if len(columns) == 1:
        return {columns[0]: {"$in": [row[0] for row in condition.values]}}
    return {"$or": [dict(zip(columns, row, strict=True)) for row in condition.values]}


def query(condition: Condition, as_of: dt.datetime) -> Document:
    """The query filter for a condition."""
    match condition:
        case AllRows() | NoRows():
            return {} if isinstance(condition, AllRows) else {"$expr": False}
        case Predicate(text):
            return parse(text, as_of, "a seed or `when` filter")
        case ValuesIn():
            return _values(condition)
        case AnyNull(columns):
            return {"$or": [{column: None} for column in columns]}
        case AnyOf(parts) | AllOf(parts):
            operator = "$or" if isinstance(condition, AnyOf) else "$and"
            return {operator: [query(part, as_of) for part in parts]}
        case SelectedBy():
            message = "MongoDB cannot evaluate nested selections; use the keys strategy"
            raise EngineError(message)
        case _:
            assert_never(condition)


def lookup(document: Mapping[str, object], path: str) -> object:
    """The value at a dotted path, or None."""
    current: object = document
    for part in path.split("."):
        found = as_document(current)
        if found is None:
            return None
        current = found.get(part)
    return current


def keys(document: Mapping[str, object], columns: tuple[str, ...]) -> Iterable[KeyTuple]:
    """The key tuples a document holds; an array of references yields one per element."""
    values: list[list[Hashable]] = []
    for column in columns:
        found = lookup(document, column)
        items = as_list(found)
        candidates = items if items is not None else [found]
        values.append([item for item in candidates if isinstance(item, Hashable)])
    rows: list[KeyTuple] = [()]
    for options in values:
        rows = [(*row, option) for row in rows for option in options]
    return rows
