# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Turning the core's subset conditions into MySQL queries.

Seeds and ``when`` clauses are SQL from the spec, inserted as written, with
``:as_of`` bound to the plan's anchor time. Strategy B's value sets become
``IN`` lists of bound values. Strategy A's references to other tables'
selections become common table expressions, one per referenced table.
Identifiers are quoted with backticks.
"""

import datetime as dt
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import assert_never

from datadouble.engine import EngineError
from datadouble.names import ColumnName, TableName
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

_AS_OF = re.compile(r"(?<![:\w]):as_of\b")


def quote(name: str) -> str:
    """A backtick-quoted identifier."""
    return "`" + name.replace("`", "``") + "`"


def qualified(table: TableName) -> str:
    """``database``.``table``."""
    return f"{quote(table.namespace)}.{quote(table.name)}"


def columns(names: Sequence[str]) -> str:
    """A comma-separated list of quoted column names."""
    return ", ".join(quote(name) for name in names)


def compose(template: str, *parts: str) -> str:
    """A statement from a fixed template and its parts.

    Every part is a quoted identifier (``quote``), a list of bound-parameter
    placeholders, or SQL the spec's owner wrote (``native``); no part ever
    carries a value read from the source's rows, which are always bound. All
    of this adapter's SQL is put together here.
    """
    return template.format(*parts)


def native(text: str) -> str:
    """Spec SQL with ``%`` escaped and ``:as_of`` turned into a bound parameter.

    Seeds, ``when`` clauses, expressions, hooks and assertions are written by
    the operator who owns the spec; they never carry data from the source's rows.
    """
    return _AS_OF.sub("%(as_of)s", text.replace("%", "%%"))


def _tuple(names: Sequence[ColumnName]) -> str:
    joined = columns(names)
    return joined if len(names) == 1 else f"({joined})"


@dataclass
class Query:
    """A rendered statement and its named parameters."""

    text: str
    params: dict[str, object]


@dataclass
class Renderer:
    """Renders conditions for one database."""

    as_of: dt.datetime
    selections: Mapping[TableName, Condition] = field(default_factory=dict[TableName, Condition])
    params: dict[str, object] = field(default_factory=dict[str, object])
    _ctes: dict[TableName, str] = field(default_factory=dict[TableName, str])
    _cte_bodies: list[str] = field(default_factory=list[str])

    def _param(self, value: object) -> str:
        name = f"p{len(self.params)}"
        self.params[name] = value
        return f"%({name})s"

    def _values(self, condition: ValuesIn) -> str:
        if not condition.values:
            return "FALSE"
        rows = sorted(condition.values, key=repr)
        if len(condition.columns) == 1:
            listed = ", ".join(self._param(row[0]) for row in rows)
            return f"{quote(condition.columns[0])} IN ({listed})"
        listed = ", ".join("(" + ", ".join(self._param(v) for v in row) + ")" for row in rows)
        return f"{_tuple(condition.columns)} IN ({listed})"

    def _cte(self, source: TableName) -> str:
        """The CTE holding ``source``'s selection, defined after those it uses."""
        if source not in self._ctes:
            condition = self.selections.get(source)
            if condition is None:
                message = f"no selection for {source}"
                raise EngineError(message)
            body = self.condition(condition)
            name = quote(f"dd_selected_{len(self._ctes)}")
            self._ctes[source] = name
            self._cte_bodies.append(
                compose("{} AS (SELECT * FROM {} WHERE {})", name, qualified(source), body)
            )
        return self._ctes[source]

    def _selected_by(self, condition: SelectedBy) -> str:
        if condition.source_condition is None:
            origin = self._cte(condition.source)
            filters: list[str] = []
        else:
            origin = qualified(condition.source)
            filters = [self.condition(condition.source_condition)]
        if condition.where:
            self.params["as_of"] = self.as_of
            filters.append(f"({native(condition.where)})")
        where = f" WHERE {' AND '.join(filters)}" if filters else ""
        selected = columns(condition.source_columns)
        return compose(
            "{} IN (SELECT {} FROM {}{})", _tuple(condition.columns), selected, origin, where
        )

    def condition(self, condition: Condition) -> str:
        """A boolean expression over one table's columns."""
        match condition:
            case AllRows() | NoRows():
                return "TRUE" if isinstance(condition, AllRows) else "FALSE"
            case Predicate(text):
                self.params["as_of"] = self.as_of
                return f"({native(text)})"
            case ValuesIn():
                return self._values(condition)
            case SelectedBy():
                return self._selected_by(condition)
            case AnyNull(names):
                return "(" + " OR ".join(f"{quote(n)} IS NULL" for n in names) + ")"
            case AnyOf(parts) | AllOf(parts):
                joiner = " OR " if isinstance(condition, AnyOf) else " AND "
                return "(" + joiner.join(self.condition(part) for part in parts) + ")"
            case _:
                assert_never(condition)

    def select(
        self,
        table: TableName,
        names: Sequence[ColumnName],
        condition: Condition,
        *,
        distinct: bool = False,
    ) -> Query:
        """``SELECT names FROM table WHERE condition``, with the CTEs it needs."""
        where = self.condition(condition)
        head = f"WITH {', '.join(self._cte_bodies)} " if self._cte_bodies else ""
        keyword = "DISTINCT " if distinct else ""
        text = compose(
            "{}SELECT {}{} FROM {} WHERE {}", head, keyword, columns(names), qualified(table), where
        )
        return Query(text=text, params=dict(self.params))
