# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Turning the core's subset conditions into T-SQL queries.

Seeds and ``when`` clauses are SQL from the spec, inserted as written, with
``:as_of`` bound to the plan's anchor time. Strategy B's value sets become
``IN`` lists of bound values, or a ``VALUES`` table for composite keys, since
T-SQL has no row-value ``IN``. Strategy A's references to other tables'
selections become common table expressions, one per referenced table.
Identifiers are quoted with brackets, and every table in a query has an alias
so that composite references can be correlated.
"""

import datetime as dt
import re
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import assert_never
from uuid import UUID

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
AS_OF = "CAST(%(as_of)s AS DATETIME2(7))"
"""The anchor time in SQL: typed, so it compares with ``datetime`` columns, whose
text form takes at most three fractional digits."""
_PARAMS = (str, int, float, Decimal, bytes, dt.datetime, dt.date, dt.time, UUID)

type Param = (
    str | int | float | Decimal | bool | bytes | dt.datetime | dt.date | dt.time | UUID | None
)
"""A value the driver can bind."""
OUTER = "t"
"""The alias of the table a query reads."""


def quote(name: str) -> str:
    """A bracket-quoted identifier."""
    return "[" + name.replace("]", "]]") + "]"


def qualified(table: TableName) -> str:
    """``[schema].[table]``."""
    return f"{quote(table.namespace)}.{quote(table.name)}"


def columns(names: Sequence[str], alias: str | None = None) -> str:
    """A comma-separated list of quoted column names, qualified by ``alias`` if given."""
    prefix = f"{alias}." if alias else ""
    return ", ".join(prefix + quote(name) for name in names)


def compose(template: str, *parts: str) -> str:
    """A statement from a fixed template and its parts.

    Every part is a quoted identifier (``quote``), a list of bound-parameter
    placeholders, or SQL the spec's owner wrote (``native``); no part ever
    carries a value read from the source's rows, which are always bound. All
    of this adapter's SQL is put together here.
    """
    return template.format(*parts)


def native(text: str) -> str:
    """Spec SQL with ``:as_of`` turned into a bound parameter.

    Seeds, ``when`` clauses, expressions, hooks and assertions are written by
    the operator who owns the spec; they never carry data from the source's rows.
    The driver replaces only its own placeholders and leaves other ``%`` signs
    alone, so ``LIKE '%x%'`` needs no escaping.
    """
    return _AS_OF.sub(AS_OF, text)


def bindable(value: Hashable) -> Param:
    """``value`` as a bound parameter; keys are values the driver read."""
    if value is None or isinstance(value, _PARAMS):
        return value
    message = f"cannot bind a {type(value).__name__} value"
    raise EngineError(message)


def as_of_value(as_of: dt.datetime) -> str:
    """The anchor time as T-SQL reads it: UTC, without an offset, to 100 ns."""
    utc = as_of.astimezone(dt.UTC).replace(tzinfo=None)
    return utc.isoformat(timespec="microseconds")


@dataclass
class Query:
    """A rendered statement and its named parameters."""

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
        return f"%({name})s"

    def _alias(self) -> str:
        self._aliases += 1
        return f"s{self._aliases}"

    def _bind_as_of(self) -> None:
        self.params["as_of"] = as_of_value(self.as_of)

    def _values(self, condition: ValuesIn, alias: str) -> str:
        if not condition.values:
            return "1 = 0"
        rows = sorted(condition.values, key=repr)
        if len(condition.columns) == 1:
            listed = ", ".join(self._param(row[0]) for row in rows)
            return compose("{}.{} IN ({})", alias, quote(condition.columns[0]), listed)
        values = self._alias()
        names = [f"c{i}" for i in range(len(condition.columns))]
        listed = ", ".join("(" + ", ".join(self._param(v) for v in row) + ")" for row in rows)
        matches = " AND ".join(
            compose("{}.{} = {}.{}", values, name, alias, quote(column))
            for name, column in zip(names, condition.columns, strict=True)
        )
        return compose(
            "EXISTS (SELECT 1 FROM (VALUES {}) AS {} ({}) WHERE {})",
            listed,
            values,
            ", ".join(names),
            matches,
        )

    def _cte(self, source: TableName) -> str:
        """The CTE holding ``source``'s selection, defined after those it uses."""
        if source not in self._ctes:
            condition = self.selections.get(source)
            if condition is None:
                message = f"no selection for {source}"
                raise EngineError(message)
            alias = self._alias()
            body = self.condition(condition, alias)
            name = quote(f"dd_selected_{len(self._ctes)}")
            self._ctes[source] = name
            self._cte_bodies.append(
                compose(
                    "{} AS (SELECT * FROM {} AS {} WHERE {})", name, qualified(source), alias, body
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
            self._bind_as_of()
            filters.append(f"({native(condition.where)})")
        if len(condition.columns) == 1:
            where = f" WHERE {' AND '.join(filters)}" if filters else ""
            return compose(
                "{}.{} IN (SELECT {}.{} FROM {} AS {}{})",
                alias,
                quote(condition.columns[0]),
                inner,
                quote(condition.source_columns[0]),
                origin,
                inner,
                where,
            )
        matches = [
            compose("{}.{} = {}.{}", inner, quote(theirs), alias, quote(ours))
            for ours, theirs in zip(condition.columns, condition.source_columns, strict=True)
        ]
        return compose(
            "EXISTS (SELECT 1 FROM {} AS {} WHERE {})",
            origin,
            inner,
            " AND ".join([*matches, *filters]),
        )

    def condition(self, condition: Condition, alias: str = OUTER) -> str:
        """A boolean expression over the columns of the table aliased ``alias``."""
        match condition:
            case AllRows() | NoRows():
                return "1 = 1" if isinstance(condition, AllRows) else "1 = 0"
            case Predicate(text):
                self._bind_as_of()
                return f"({native(text)})"
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
        """``SELECT expressions FROM table AS t WHERE condition``, with the CTEs it needs.

        ``expressions`` are over the alias ``t``.
        """
        where = self.condition(condition)
        head = f"WITH {', '.join(self._cte_bodies)} " if self._cte_bodies else ""
        keyword = "DISTINCT " if distinct else ""
        text = compose(
            "{}SELECT {}{} FROM {} AS {} WHERE {}",
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
