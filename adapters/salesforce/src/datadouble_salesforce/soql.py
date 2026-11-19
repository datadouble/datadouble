# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""SOQL: rendering the core's subset conditions, and translating queries to SQLite.

Seeds, ``when`` clauses and assertions are SOQL; ``:as_of`` in them stands for
the plan's anchor time, a datetime literal. Strategy B's value sets become
``IN`` lists. Salesforce cannot evaluate another object's selection inside a
query in general (semi-joins allow one level, without ``OR``), so strategy A is
not offered.

A query has a length limit, so a condition too long for one is split: its
largest value set is halved, and the halves queried separately. Conditions
have no negation, so the rows the halves choose together are the rows the
whole would have chosen.

The fake org and restored images are SQLite databases, which answer the subset
of SOQL these use: ``SELECT`` of plain fields from one object, ``WHERE`` with
comparisons, ``LIKE``, ``IN``, ``NOT IN``, ``AND``, ``OR`` and ``NOT``, then
``ORDER BY`` and ``LIMIT``. As in Salesforce, comparing text ignores case.
"""

import datetime as dt
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import assert_never

from datadouble.engine import EngineError
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

AS_OF = re.compile(r":as_of\b")

REST_LIMIT = 10_000
"""Characters of SOQL in a REST query, whose text goes in the URL."""
BULK_LIMIT = 90_000
"""Characters of SOQL in a Bulk API query; Salesforce allows 100,000."""

type Value = str | int | float | bool | None


class SoqlError(Exception):
    """A query outside the SOQL this module translates; the message names no values."""


def datetime_literal(moment: dt.datetime) -> str:
    """A SOQL datetime literal, in UTC with milliseconds, as stored copies hold them."""
    utc = moment.astimezone(dt.UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def literal(value: Value) -> str:
    """A value as a SOQL literal."""
    match value:
        case None:
            return "null"
        case bool():
            return "true" if value else "false"
        case int():
            return str(value)
        case float():
            return format(value, "f")
        case str():
            escaped = value.replace("\\", "\\\\").replace("'", "\\'")
            return f"'{escaped}'"
        case _:
            assert_never(value)


def _values(condition: ValuesIn) -> str:
    if not condition.values:
        return "Id = null"
    columns = condition.columns
    if len(columns) == 1:
        listed = ", ".join(sorted(literal(_value(row[0])) for row in condition.values))
        return f"{columns[0]} IN ({listed})"
    rows = sorted(
        " AND ".join(f"{c} = {literal(_value(v))}" for c, v in zip(columns, row, strict=True))
        for row in condition.values
    )
    return " OR ".join(f"({row})" for row in rows)


def _value(value: object) -> Value:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    message = f"a key value of type {type(value).__name__} has no SOQL literal"
    raise EngineError(message)


def _combined(condition: AnyOf | AllOf, as_of: dt.datetime) -> str | None:
    rendered = [where(part, as_of) for part in condition.parts]
    if isinstance(condition, AnyOf) and None in rendered:
        return None
    texts = [f"({text})" for text in rendered if text is not None]
    if not texts:
        return None if isinstance(condition, AllOf) else "Id = null"
    return (" OR " if isinstance(condition, AnyOf) else " AND ").join(texts)


def where(condition: Condition, as_of: dt.datetime) -> str | None:
    """The ``WHERE`` clause's text for a condition, or None for every row."""
    text: str | None = None
    match condition:
        case AllRows():
            pass
        case NoRows():
            # Every record has an Id.
            text = "Id = null"
        case Predicate(predicate):
            text = AS_OF.sub(datetime_literal(as_of), predicate)
        case ValuesIn():
            text = _values(condition)
        case AnyNull(columns):
            text = " OR ".join(f"{column} = null" for column in columns)
        case AnyOf() | AllOf():
            text = _combined(condition, as_of)
        case SelectedBy():
            message = "Salesforce cannot evaluate nested selections; use the keys strategy"
            raise EngineError(message)
        case _:
            assert_never(condition)
    return text


def _largest(condition: Condition) -> ValuesIn | None:
    match condition:
        case ValuesIn():
            return condition
        case AnyOf(parts) | AllOf(parts):
            found = [part for part in map(_largest, parts) if part is not None]
            return max(found, key=lambda part: len(part.values), default=None)
        case _:
            return None


def _replace(condition: Condition, old: ValuesIn, new: ValuesIn) -> Condition:
    if condition is old:
        return new
    match condition:
        case AnyOf(parts):
            return AnyOf(tuple(_replace(part, old, new) for part in parts))
        case AllOf(parts):
            return AllOf(tuple(_replace(part, old, new) for part in parts))
        case _:
            return condition


def wheres(condition: Condition, as_of: dt.datetime, limit: int) -> list[str | None]:
    """``WHERE`` texts no longer than ``limit`` that together choose ``condition``'s rows."""
    text = where(condition, as_of)
    if text is None or len(text) <= limit:
        return [text]
    largest = _largest(condition)
    if largest is None or len(largest.values) <= 1:
        message = f"a condition of {len(text)} characters is longer than SOQL allows"
        raise EngineError(message)
    values = sorted(largest.values, key=repr)
    middle = len(values) // 2
    halves = (values[:middle], values[middle:])
    return [
        part
        for half in halves
        for part in wheres(
            _replace(condition, largest, ValuesIn(largest.columns, frozenset(half))), as_of, limit
        )
    ]


_TOKEN = re.compile(
    r"""\s*(?:
        (?P<string>'(?:[^'\\]|\\.)*')
      | (?P<datetime>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2}))
      | (?P<date>\d{4}-\d{2}-\d{2})
      | (?P<number>-?\d+(?:\.\d+)?)
      | (?P<name>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)
      | (?P<operator>!=|<>|<=|>=|=|<|>)
      | (?P<punctuation>[(),])
    )""",
    re.VERBOSE,
)
_ESCAPE = re.compile(r"\\(.)")
_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}
_OPERATORS = {"=": "=", "!=": "!=", "<>": "!=", "<": "<", "<=": "<=", ">": ">", ">=": ">="}


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str
    text: str


def _tokens(query: str) -> list[_Token]:
    found: list[_Token] = []
    position = 0
    while position < len(query.rstrip()):
        match = _TOKEN.match(query, position)
        if match is None or match.lastgroup is None:
            message = f"SOQL the copies cannot run, at character {position + 1}"
            raise SoqlError(message)
        found.append(_Token(match.lastgroup, match[match.lastgroup]))
        position = match.end()
    return found


@dataclass(frozen=True, slots=True)
class Translated:
    """A SOQL query as SQLite, with its parameters."""

    object: str
    """The object queried, as its definition spells it."""
    fields: tuple[str, ...]
    """The fields selected, as their definitions spell them."""
    sql: str
    parameters: tuple[Value, ...]


type Fields = Callable[[str], Mapping[str, str] | None]
"""An object's fields by lower-case name, as definitions spell them; None if no such object."""


class _Parser:
    def __init__(self, query: str, fields: Fields) -> None:
        self.tokens = _tokens(query)
        self.position = 0
        self.lookup = fields
        self.known: Mapping[str, str] = {}
        self.parameters: list[Value] = []

    def peek(self, word: str | None = None) -> _Token | None:
        if self.position >= len(self.tokens):
            return None
        token = self.tokens[self.position]
        if word is not None and (token.kind != "name" or token.text.upper() != word):
            return None
        return token

    def take(self, word: str | None = None, kind: str | None = None) -> _Token:
        token = self.peek(word)
        if token is None or (kind is not None and token.kind != kind):
            wanted = word or kind or "more"
            message = f"SOQL the copies cannot run: expected {wanted} at token {self.position + 1}"
            raise SoqlError(message)
        self.position += 1
        return token

    def punctuation(self, text: str) -> bool:
        token = self.peek()
        if token is not None and token.kind == "punctuation" and token.text == text:
            self.position += 1
            return True
        return False

    def field(self) -> str:
        return self.resolve(self.take(kind="name").text)

    def resolve(self, name: str) -> str:
        if "." in name:
            message = "SOQL the copies cannot run: relationship fields are not supported"
            raise SoqlError(message)
        found = self.known.get(name.lower())
        if found is None:
            message = f"no field {name} in the queried object"
            raise SoqlError(message)
        return f'"{found}"'

    def value(self) -> tuple[Value, bool]:
        """A literal, and whether it is a string (compared ignoring case)."""
        token = self.take()
        match token.kind:
            case "string":
                inner = token.text[1:-1]
                return _ESCAPE.sub(lambda m: _ESCAPES.get(m[1], m[1]), inner), True
            case "number":
                return (float(token.text) if "." in token.text else int(token.text)), False
            case "datetime":
                parsed = dt.datetime.fromisoformat(token.text)
                return datetime_literal(parsed), False
            case "date":
                return token.text, False
            case "name" if token.text.lower() in {"true", "false", "null"}:
                lowered = token.text.lower()
                return (None if lowered == "null" else lowered == "true"), False
            case _:
                message = f"SOQL the copies cannot run: expected a value at token {self.position}"
                raise SoqlError(message)

    def bind(self, value: Value) -> str:
        self.parameters.append(value)
        return "?"

    def comparison(self) -> str:
        column = self.field()
        if self.peek("LIKE"):
            self.position += 1
            pattern, _ = self.value()
            return f"{column} LIKE {self.bind(pattern)} ESCAPE '\\'"
        negated = self.peek("NOT") is not None
        if negated:
            self.position += 1
        if self.peek("IN"):
            self.position += 1
            self.take(kind="punctuation")
            values = [self.value()]
            while self.punctuation(","):
                values.append(self.value())
            self.take(kind="punctuation")
            collate = " COLLATE NOCASE" if any(text for _, text in values) else ""
            listed = ", ".join(self.bind(value) for value, _ in values)
            return f"{column}{collate} {'NOT IN' if negated else 'IN'} ({listed})"
        if negated:
            message = "SOQL the copies cannot run: NOT must come before a condition or IN"
            raise SoqlError(message)
        operator = _OPERATORS[self.take(kind="operator").text]
        value, text = self.value()
        if value is None:
            if operator not in {"=", "!="}:
                message = "SOQL the copies cannot run: null compares only with = and !="
                raise SoqlError(message)
            return f"{column} IS {'NOT ' if operator == '!=' else ''}NULL"
        if isinstance(value, bool):
            value = int(value)
        collate = " COLLATE NOCASE" if text else ""
        return f"{column} {operator} {self.bind(value)}{collate}"

    def primary(self) -> str:
        if self.peek("NOT"):
            self.position += 1
            return f"NOT ({self.primary()})"
        if self.punctuation("("):
            inner = self.disjunction()
            self.take(kind="punctuation")
            return f"({inner})"
        return self.comparison()

    def conjunction(self) -> str:
        parts = [self.primary()]
        while self.peek("AND"):
            self.position += 1
            parts.append(self.primary())
        return " AND ".join(parts)

    def disjunction(self) -> str:
        parts = [self.conjunction()]
        while self.peek("OR"):
            self.position += 1
            parts.append(self.conjunction())
        return " OR ".join(parts)

    def query(self) -> Translated:
        self.take("SELECT")
        names = [self.take(kind="name").text]
        while self.punctuation(","):
            names.append(self.take(kind="name").text)
        self.take("FROM")
        object_name = self.take(kind="name").text
        known = self.lookup(object_name)
        if known is None:
            message = f"no object {object_name}"
            raise SoqlError(message)
        self.known = known
        fields = [self.resolve(name) for name in names]
        sql = f'SELECT {", ".join(fields)} FROM "{object_name}"'
        if self.peek("WHERE"):
            self.position += 1
            sql += f" WHERE {self.disjunction()}"
        if self.peek("ORDER"):
            self.position += 1
            self.take("BY")
            orders = [self.order()]
            while self.punctuation(","):
                orders.append(self.order())
            sql += f" ORDER BY {', '.join(orders)}"
        if self.peek("LIMIT"):
            self.position += 1
            limit = self.take(kind="number").text
            sql += f" LIMIT {int(limit)}"
        if self.peek() is not None:
            message = f"SOQL the copies cannot run: unexpected text at token {self.position + 1}"
            raise SoqlError(message)
        return Translated(
            object=object_name,
            fields=tuple(field.strip('"') for field in fields),
            sql=sql,
            parameters=tuple(self.parameters),
        )

    def order(self) -> str:
        column = self.field()
        if self.peek("DESC") or self.peek("ASC"):
            return f"{column} {self.take(kind='name').text.upper()}"
        return column


def translate(query: str, fields: Fields) -> Translated:
    """A SOQL query as SQLite over tables named for objects, with columns named for fields."""
    return _Parser(query, fields).query()
