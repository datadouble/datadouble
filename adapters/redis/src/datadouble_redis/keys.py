# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Key patterns, and the core's subset conditions evaluated against keys.

A key belongs to the first declared pattern it matches, or else to the
pattern made by writing as ``{}`` every segment that is not a name (letters,
digits, ``_`` and ``-``, starting with a letter or ``_``) or that looks like
an ID (a number, a UUID, a long hex string): ``user:42:sessions`` belongs to
``user:{}:sessions``.
The values the placeholders stand for, joined by the separator, are the
key's ``_key``.

Table names leave production in the catalog lock and the plan, so a key's
literal segments must not be data. Inferred patterns are therefore checked:
when the keys under one prefix have more literal values at one position than
the limit (64 for a key's first segment, 16 after it), those values are
almost certainly data (user names, say), and the catalog fails, naming only
the prefix and the position, until a pattern is declared for them. Fewer
name-shaped values than the limit cannot be told from names; declaring
patterns is the way to be sure.

Redis has no query language, so Datadouble evaluates the planner's
conditions itself, against each key's values: seeds and ``when`` clauses
are globs over ``_key``, and an element of a list, set or sorted set counts
as a value of its column, as an element of a MongoDB array does.
"""

import fnmatch
import itertools
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import assert_never

from datadouble.engine import EngineError
from datadouble.names import ColumnName
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

PLACEHOLDER = "{}"
KEY = ColumnName("_key")
FIRST_LITERALS = 64
"""More distinct first segments than this fails the catalog."""
LITERALS = 16
"""More distinct literal values than this at a later position of a prefix fails the catalog."""

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}")
_ID = re.compile(
    r"\d+"
    r"|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|[0-9a-fA-F]{8,}"
)

type Row = Mapping[ColumnName, Sequence[str]]
"""A key's values by column; a list, set or sorted set's column holds every element."""


@dataclass(frozen=True, slots=True)
class Pattern:
    """A declared key pattern, such as ``session:{}``."""

    text: str
    regex: re.Pattern[str]

    @classmethod
    def parse(cls, text: str) -> "Pattern":
        """A pattern with at least one ``{}``; each stands for one or more characters."""
        if PLACEHOLDER not in text:
            message = f"the key pattern {text!r} has no {PLACEHOLDER} placeholder"
            raise EngineError(message)
        parts = (re.escape(part) for part in text.split(PLACEHOLDER))
        return cls(text, re.compile("(.+?)".join(parts)))

    def match(self, key: str) -> tuple[str, ...] | None:
        """The values the placeholders stand for, if ``key`` matches."""
        found = self.regex.fullmatch(key)
        return found.groups() if found else None


@dataclass(frozen=True, slots=True)
class Placed:
    """A key's pattern, and the values its placeholders stand for."""

    pattern: str
    values: tuple[str, ...]
    inferred: bool

    def key(self, separator: str) -> str:
        """The key's ``_key``."""
        return separator.join(self.values)


def place(key: str, declared: Sequence[Pattern], separator: str) -> Placed:
    """The pattern ``key`` belongs to."""
    for pattern in declared:
        values = pattern.match(key)
        if values is not None:
            return Placed(pattern.text, values, inferred=False)
    segments = key.split(separator)
    shape = [s if _NAME.fullmatch(s) and not _ID.fullmatch(s) else PLACEHOLDER for s in segments]
    values = tuple(s for s, part in zip(segments, shape, strict=True) if part == PLACEHOLDER)
    return Placed(separator.join(shape), values, inferred=True)


class Literals:
    """Counts the literal values at each position of each inferred prefix.

    Holds at most one value over the limit per position, however many keys pass.
    """

    def __init__(self, separator: str) -> None:
        """Count literals in patterns split by ``separator``."""
        self.separator = separator
        self.seen: dict[tuple[str, ...], set[str]] = {}

    def add(self, placed: Placed) -> None:
        """Count an inferred pattern's literal segments; declared ones are trusted."""
        if not placed.inferred:
            return
        parts = placed.pattern.split(self.separator)
        for position, part in enumerate(parts):
            if part != PLACEHOLDER:
                values = self.seen.setdefault(tuple(parts[:position]), set())
                if len(values) <= _limit(position):
                    values.add(part)

    def check(self) -> None:
        """Fail if any position holds more literal values than its limit."""
        crowded = sorted(
            prefix for prefix, values in self.seen.items() if len(values) > _limit(len(prefix))
        )
        if crowded:
            where = "; ".join(
                f"segment {len(prefix) + 1} after {self.separator.join(prefix)!r}"
                if prefix
                else "the first segment"
                for prefix in crowded
            )
            message = (
                f"keys have too many distinct values in {where}, which look like data"
                " rather than names; declare patterns for them in source.connection.patterns"
                f" (such as 'user:{PLACEHOLDER}')"
            )
            raise EngineError(message)


def _limit(position: int) -> int:
    return FIRST_LITERALS if position == 0 else LITERALS


def glob(pattern: str) -> str:
    """A ``SCAN MATCH`` glob for a pattern's keys; it may match other patterns' too."""
    escaped = re.sub(r"([*?\[\]\\])", r"\\\1", pattern)
    return escaped.replace(PLACEHOLDER, "*")


def values(row: Row, columns: Sequence[ColumnName]) -> list[KeyTuple]:
    """Every combination of the columns' values, a missing column counting as None."""
    choices = [row.get(column) or (None,) for column in columns]
    return list(itertools.product(*choices))


def matches(condition: Condition, row: Row) -> bool:
    """Whether a key with these values meets ``condition``."""
    match condition:
        case AllRows() | NoRows():
            return isinstance(condition, AllRows)
        case Predicate(text=text):
            return fnmatch.fnmatchcase((row.get(KEY) or ("",))[0], text)
        case ValuesIn(columns=columns, values=chosen):
            return any(combination in chosen for combination in values(row, columns))
        case AnyNull(columns=columns):
            return any(not row.get(column) for column in columns)
        case AnyOf(parts=parts):
            return any(matches(part, row) for part in parts)
        case AllOf(parts=parts):
            return all(matches(part, row) for part in parts)
        case SelectedBy():
            message = "Redis cannot evaluate another table's selection; use strategy B"
            raise EngineError(message)
        case _:
            assert_never(condition)


def needed(condition: Condition) -> set[ColumnName]:
    """The columns a condition reads."""
    match condition:
        case Predicate():
            return {KEY}
        case ValuesIn(columns=columns) | AnyNull(columns=columns):
            return set(columns)
        case AnyOf(parts=parts) | AllOf(parts=parts):
            return set[ColumnName]().union(*(needed(part) for part in parts))
        case _:
            return set()


def projected(rows: Iterable[Row], columns: Sequence[ColumnName]) -> set[KeyTuple]:
    """The distinct value combinations of ``columns`` in ``rows``."""
    return {combination for row in rows for combination in values(row, columns)}
