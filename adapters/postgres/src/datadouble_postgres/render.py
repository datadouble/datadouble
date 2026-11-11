# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Turning the core's subset conditions into PostgreSQL queries.

Seeds and ``when`` clauses are SQL from the spec, inserted as written, with
``:as_of`` bound to the plan's anchor time. Strategy B's value sets become
typed array parameters. Strategy A's references to other tables' selections
become materialized common table expressions, one per referenced table.
"""

import datetime as dt
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import assert_never

from psycopg import sql

from datadouble.catalog import Catalog
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


type Statement = sql.SQL | sql.Composed
"""Something psycopg can execute."""


def trusted(text: str) -> sql.SQL:
    """SQL from the spec or the catalog, inserted as written.

    Seeds, ``when`` clauses, expressions, hooks and assertions are written by
    the operator who owns the spec, and type names come from the source's own
    catalog; neither carries data from the source's rows.
    """
    return sql.SQL(text)  # pyright: ignore[reportArgumentType] - operator-written spec SQL, not row data


def native(text: str) -> sql.SQL:
    """Spec SQL with ``%`` escaped and ``:as_of`` turned into a bound parameter."""
    return trusted(_AS_OF.sub("%(as_of)s", text.replace("%", "%%")))


def columns(names: Sequence[ColumnName]) -> sql.Composed:
    """A comma-separated list of quoted column names."""
    return sql.SQL(", ").join(sql.Identifier(name) for name in names)


def _tuple(names: Sequence[ColumnName]) -> sql.Composable:
    joined = columns(names)
    return joined if len(names) == 1 else sql.SQL("({})").format(joined)


@dataclass
class Query:
    """A rendered statement and its parameters."""

    text: Statement
    params: dict[str, object]


@dataclass
class Renderer:
    """Renders conditions against one catalog."""

    catalog: Catalog
    partitioned: frozenset[TableName]
    as_of: dt.datetime
    selections: Mapping[TableName, Condition] = field(default_factory=dict[TableName, Condition])
    params: dict[str, object] = field(default_factory=dict[str, object])
    _ctes: dict[TableName, sql.Identifier] = field(default_factory=dict[TableName, sql.Identifier])
    _cte_bodies: list[sql.Composable] = field(default_factory=list[sql.Composable])

    def table(self, name: TableName) -> sql.Composable:
        """A table reference; ``ONLY`` keeps inheritance children out of plain tables."""
        qualified = sql.Identifier(name.namespace, name.name)
        if name in self.partitioned:
            return qualified
        return sql.SQL("ONLY {}").format(qualified)

    def _type(self, table: TableName, column: ColumnName) -> sql.Composable:
        found = self.catalog.table(table)
        spec = found.column(column) if found else None
        if spec is None:
            message = f"{table}.{column} is not in the catalog"
            raise EngineError(message)
        return trusted(spec.type)

    def _param(self, value: object) -> sql.Composable:
        name = f"p{len(self.params)}"
        self.params[name] = value
        return sql.Placeholder(name)

    def _values(self, table: TableName, condition: ValuesIn) -> sql.Composable:
        if not condition.values:
            return sql.SQL("FALSE")
        rows = list(condition.values)
        arrays = [
            sql.SQL("{}::{}[]").format(
                self._param([row[i] for row in rows]), self._type(table, column)
            )
            for i, column in enumerate(condition.columns)
        ]
        if len(arrays) == 1:
            return sql.SQL("{} = ANY({})").format(sql.Identifier(condition.columns[0]), arrays[0])
        return sql.SQL("{} IN (SELECT * FROM unnest({}))").format(
            _tuple(condition.columns), sql.SQL(", ").join(arrays)
        )

    def _cte(self, source: TableName) -> sql.Identifier:
        """The CTE holding ``source``'s selection, defined after those it uses."""
        if source not in self._ctes:
            condition = self.selections.get(source)
            if condition is None:
                message = f"no selection for {source}"
                raise EngineError(message)
            body = self.condition(source, condition)
            name = sql.Identifier(f"dd_selected_{len(self._ctes)}")
            self._ctes[source] = name
            self._cte_bodies.append(
                sql.SQL("{} AS MATERIALIZED (SELECT * FROM {} WHERE {})").format(
                    name, self.table(source), body
                )
            )
        return self._ctes[source]

    def _selected_by(self, condition: SelectedBy) -> sql.Composable:
        if condition.source_condition is None:
            origin: sql.Composable = self._cte(condition.source)
            filters: list[sql.Composable] = []
        else:
            origin = self.table(condition.source)
            filters = [self.condition(condition.source, condition.source_condition)]
        if condition.where:
            filters.append(sql.SQL("({})").format(native(condition.where)))
        where = (
            sql.SQL(" WHERE {}").format(sql.SQL(" AND ").join(filters)) if filters else sql.SQL("")
        )
        return sql.SQL("{} IN (SELECT {} FROM {}{})").format(
            _tuple(condition.columns), columns(condition.source_columns), origin, where
        )

    def condition(self, table: TableName, condition: Condition) -> sql.Composable:
        """A boolean expression over the columns of ``table``."""
        match condition:
            case AllRows() | NoRows():
                return sql.SQL("TRUE" if isinstance(condition, AllRows) else "FALSE")
            case Predicate(text):
                self.params["as_of"] = self.as_of
                return sql.SQL("({})").format(native(text))
            case ValuesIn():
                return self._values(table, condition)
            case SelectedBy():
                return self._selected_by(condition)
            case AnyNull(names):
                parts = [sql.SQL("{} IS NULL").format(sql.Identifier(n)) for n in names]
                return sql.SQL("({})").format(sql.SQL(" OR ").join(parts))
            case AnyOf(parts) | AllOf(parts):
                joiner = " OR " if isinstance(condition, AnyOf) else " AND "
                rendered = [self.condition(table, part) for part in parts]
                return sql.SQL("({})").format(sql.SQL(joiner).join(rendered))
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
        where = self.condition(table, condition)
        head = (
            sql.SQL("WITH {} ").format(sql.SQL(", ").join(self._cte_bodies))
            if self._cte_bodies
            else sql.SQL("")
        )
        text = sql.SQL("{}SELECT {}{} FROM {} WHERE {}").format(
            head,
            sql.SQL("DISTINCT ") if distinct else sql.SQL(""),
            columns(names),
            self.table(table),
            where,
        )
        return Query(text=text, params=dict(self.params))
