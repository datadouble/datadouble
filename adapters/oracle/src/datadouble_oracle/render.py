# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Turning the core's subset conditions into Oracle queries.

Seeds and ``when`` clauses are SQL from the spec, inserted as written; their
``:as_of`` is already an Oracle bind variable, bound to the plan's anchor time.
Strategy B's value sets become ``IN`` lists of bound values, at most 1000 to a
list as Oracle allows. Strategy A's references to other tables' selections
become common table expressions, one per referenced table. Identifiers are
double-quoted with their exact case, and every table in a query has an alias
(Oracle takes no ``AS`` before one).
"""

import datetime as dt
import re
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
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
from datadouble_oracle.driver import Value

OUTER = "t"
"""The alias of the table a query reads."""
_LIST = 1000
_AS_OF = re.compile(r"(?<![:\w]):as_of\b")
_PARAMS = (str, int, float, Decimal, bytes, dt.datetime, dt.date)

type Param = Value
"""A value the driver can bind."""


def quote(name: str) -> str:
    """A double-quoted identifier."""
    return '"' + name.replace('"', '""') + '"'


def qualified(table: TableName) -> str:
    """``"OWNER"."TABLE"``."""
    return f"{quote(table.namespace)}.{quote(table.name)}"


def columns(names: Sequence[str], alias: str | None = None) -> str:
    """A comma-separated list of quoted column names, qualified by ``alias`` if given."""
    prefix = f"{alias}." if alias else ""
    return ", ".join(prefix + quote(name) for name in names)


def compose(template: str, *parts: str) -> str:
    """A statement from a fixed template and its parts.

    Every part is a quoted identifier (``quote``), a list of bind variables,
    or SQL the spec's owner wrote; no part ever carries a value read from the
    source's rows, which are always bound. All of this adapter's SQL is put
    together here.
    """
    return template.format(*parts)


def uses_as_of(text: str) -> bool:
    """Whether spec SQL refers to the anchor time."""
    return _AS_OF.search(text) is not None


def bindable(value: Hashable) -> Param:
    """``value`` as a bind variable; keys are values the driver read."""
    if value is None or isinstance(value, _PARAMS):
        return value
    message = f"cannot bind a {type(value).__name__} value"
    raise EngineError(message)


def as_of_value(as_of: dt.datetime) -> dt.datetime:
    """The anchor time as Oracle compares it: UTC, without an offset."""
    return as_of.astimezone(dt.UTC).replace(tzinfo=None)


def _tuple(names: Sequence[str], alias: str) -> str:
    joined = columns(names, alias)
    return joined if len(names) == 1 else f"({joined})"


@dataclass
class Query:
    """A rendered statement and its named bind variables."""

    text: str
    params: dict[str, Param]


@dataclass
class Renderer:
    """Renders conditions for one database."""

    as_of: dt.datetime
    selections: Mapping[TableName, Condition] = field(default_factory=dict[TableName, Condition])
    params: dict[str, Param] = field(default_factory=dict[str, Param])
    _ctes: dict[TableName, str] = field(default_factory=dict[TableName, str])
    _cte_bodies: list[str] = field(default_factory=list[str])
    _aliases: int = 0

    def _param(self, value: Hashable) -> str:
        name = f"p{len(self.params)}"
        self.params[name] = bindable(value)
        return f":{name}"

    def _alias(self) -> str:
        self._aliases += 1
        return f"s{self._aliases}"

    def _native(self, text: str) -> str:
        if uses_as_of(text):
            self.params["as_of"] = as_of_value(self.as_of)
        return f"({text})"

    def _values(self, condition: ValuesIn, alias: str) -> str:
        if not condition.values:
            return "1 = 0"
        rows = sorted(condition.values, key=repr)
        target = _tuple(condition.columns, alias)
        lists: list[str] = []
        for start in range(0, len(rows), _LIST):
            chunk = rows[start : start + _LIST]
            if len(condition.columns) == 1:
                listed = ", ".join(self._param(row[0]) for row in chunk)
            else:
                listed = ", ".join(
                    "(" + ", ".join(self._param(v) for v in row) + ")" for row in chunk
                )
            lists.append(f"{target} IN ({listed})")
        return lists[0] if len(lists) == 1 else "(" + " OR ".join(lists) + ")"

    def _cte(self, source: TableName) -> str:
        """The CTE holding ``source``'s selection, defined after those it uses."""
        if source not in self._ctes:
            condition = self.selections.get(source)
            if condition is None:
                message = f"no selection for {source}"
                raise EngineError(message)
            alias = self._alias()
            body = self.condition(condition, alias)
            name = quote(f"DD_SELECTED_{len(self._ctes)}")
            self._ctes[source] = name
            self._cte_bodies.append(
                compose(
                    "{} AS (SELECT * FROM {} {} WHERE {})", name, qualified(source), alias, body
                )
            )
        return self._ctes[source]

    def _selected_by(self, condition: SelectedBy, alias: str) -> str:
        inner = self._alias()
        if condition.source_condition is None:
            origin = self._cte(condition.source)
            filters: list[str] = []
        else:
            origin = qualified(condition.source)
            filters = [self.condition(condition.source_condition, inner)]
        if condition.where:
            filters.append(self._native(condition.where))
        where = f" WHERE {' AND '.join(filters)}" if filters else ""
        return compose(
            "{} IN (SELECT {} FROM {} {}{})",
            _tuple(condition.columns, alias),
            columns(condition.source_columns, inner),
            origin,
            inner,
            where,
        )

    def condition(self, condition: Condition, alias: str = OUTER) -> str:
        """A boolean expression over the columns of the table aliased ``alias``."""
        match condition:
            case AllRows() | NoRows():
                return "1 = 1" if isinstance(condition, AllRows) else "1 = 0"
            case Predicate(text):
                return self._native(text)
            case ValuesIn():
                return self._values(condition, alias)
            case SelectedBy():
                return self._selected_by(condition, alias)
            case AnyNull(names):
                return "(" + " OR ".join(f"{alias}.{quote(n)} IS NULL" for n in names) + ")"
            case AnyOf(parts) | AllOf(parts):
                joiner = " OR " if isinstance(condition, AnyOf) else " AND "
                return "(" + joiner.join(self.condition(part, alias) for part in parts) + ")"
            case _:
                assert_never(condition)

    def select(
        self,
        table: TableName,
        expressions: Sequence[str],
        condition: Condition,
        *,
        distinct: bool = False,
    ) -> Query:
        """``SELECT expressions FROM table t WHERE condition``, with the CTEs it needs."""
        where = self.condition(condition)
        head = f"WITH {', '.join(self._cte_bodies)} " if self._cte_bodies else ""
        keyword = "DISTINCT " if distinct else ""
        text = compose(
            "{}SELECT {}{} FROM {} {} WHERE {}",
            head,
            keyword,
            ", ".join(expressions),
            qualified(table),
            OUTER,
            where,
        )
        return Query(text=text, params=dict(self.params))

    def keys(self, table: TableName, names: Sequence[ColumnName], condition: Condition) -> Query:
        """The distinct values of ``names`` for the chosen rows."""
        return self.select(table, [columns([n], OUTER) for n in names], condition, distinct=True)
