# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""An in-memory engine for testing the engine-neutral core.

Tables are lists of dicts. Native predicates are a tiny language,
``column op literal`` joined by ``and``, which is enough for seeds and
``when`` clauses in tests.
"""

import operator
import re
from collections.abc import Callable, Hashable, Iterable, Mapping
from dataclasses import dataclass, field

from datadouble.catalog import Catalog, Column, ColumnKind, ForeignKey, Table
from datadouble.names import ColumnName, EngineName, TableName
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

type Row = Mapping[str, Hashable]

_OPS: dict[str, Callable[[int | str, int | str], bool]] = {
    "<=": operator.le,
    ">=": operator.ge,
    "!=": operator.ne,
    "=": operator.eq,
    "<": operator.lt,
    ">": operator.gt,
}
_TERM = re.compile(r"^\s*(\w+)\s*(<=|>=|!=|=|<|>)\s*('?)(.*?)\3\s*$")


def evaluate_predicate(text: str, row: Row) -> bool:
    """Evaluate ``a = 1 and b != 'x'`` against a row."""
    for term in re.split(r"\s+and\s+", text):
        match = _TERM.match(term)
        if match is None:
            message = f"the fake engine cannot read {term!r}"
            raise ValueError(message)
        column, op, quote, literal = match.groups()
        value: int | str = literal if quote else int(literal)
        actual = row[column]
        if not isinstance(actual, int | str) or not _OPS[op](actual, value):
            return False
    return True


def table(
    name: str,
    columns: Iterable[str],
    *,
    key: tuple[str, ...] = ("id",),
    nullable: Iterable[str] = (),
    references: Iterable[tuple[str, tuple[str, ...], str, tuple[str, ...]]] = (),
    kinds: Mapping[str, ColumnKind] | None = None,
    namespace: str = "public",
) -> Table:
    """Build a catalog table; references are ``(name, columns, parent, parent columns)``."""
    nullable = set(nullable)
    kinds = kinds or {}
    return Table(
        name=TableName.of(namespace, name),
        columns=tuple(
            Column(
                name=ColumnName(column),
                type=str(kinds.get(column, ColumnKind.INTEGER)),
                kind=kinds.get(column, ColumnKind.INTEGER),
                nullable=column in nullable,
            )
            for column in columns
        ),
        primary_key=tuple(ColumnName(c) for c in key),
        foreign_keys=tuple(
            ForeignKey(
                name=fk,
                columns=tuple(ColumnName(c) for c in fk_columns),
                target=TableName.of(namespace, parent),
                target_columns=tuple(ColumnName(c) for c in parent_columns),
            )
            for fk, fk_columns, parent, parent_columns in references
        ),
    )


def catalog(*tables: Table) -> Catalog:
    """A catalog of the fake engine."""
    return Catalog(engine=EngineName("fake"), tables=tables)


@dataclass
class FakeSource:
    """Rows per table, with an evaluator for conditions."""

    rows: dict[TableName, list[Row]]
    reads: list[TableName] = field(default_factory=list[TableName])
    toolings: list[object] = field(default_factory=list[object])
    """The engine tooling each extract and working-database set was given."""

    def matches(
        self,
        table: TableName,
        condition: Condition,
        row: Row,
        selections: Mapping[TableName, Condition],
    ) -> bool:
        """Whether ``row`` of ``table`` matches ``condition``."""
        match condition:
            case AllRows():
                return True
            case NoRows():
                return False
            case Predicate(text):
                return evaluate_predicate(text, row)
            case ValuesIn(columns, values):
                return tuple(row[c] for c in columns) in values
            case SelectedBy(columns, source, source_columns, where, partial):
                wanted = tuple(row[c] for c in columns)
                chosen = (
                    self.select(source, selections)
                    if partial is None
                    else [
                        r for r in self.rows[source] if self.matches(source, partial, r, selections)
                    ]
                )
                return any(
                    tuple(other[c] for c in source_columns) == wanted
                    for other in chosen
                    if where is None or evaluate_predicate(where, other)
                )
            case AnyNull(columns):
                return any(row[c] is None for c in columns)
            case AnyOf(parts):
                return any(self.matches(table, p, row, selections) for p in parts)
            case AllOf(parts):
                return all(self.matches(table, p, row, selections) for p in parts)

    def select(self, table: TableName, selections: Mapping[TableName, Condition]) -> list[Row]:
        """The rows a set of conditions chooses from ``table``."""
        return [
            row
            for row in self.rows.get(table, [])
            if self.matches(table, selections[table], row, selections)
        ]

    def project(
        self, table: TableName, condition: Condition, columns: tuple[ColumnName, ...]
    ) -> Iterable[KeyTuple]:
        """The :class:`~datadouble.subset.KeyReader` interface."""
        self.reads.append(table)
        return [
            tuple(row[c] for c in columns)
            for row in self.rows.get(table, [])
            if self.matches(table, condition, row, {})
        ]


TEXT = ColumnKind.TEXT


def shop() -> Catalog:
    """A small application schema with the awkward cases the planner must handle."""
    return catalog(
        table(
            "users",
            ["id", "email", "name", "created", "note"],
            nullable=["note"],
            kinds={"email": TEXT, "name": TEXT, "note": TEXT},
        ),
        table("categories", ["id", "name"], kinds={"name": TEXT}),
        table(
            "posts",
            ["id", "user_id", "category_id", "title"],
            nullable=["category_id"],
            kinds={"title": TEXT},
            references=[
                ("posts_user_id_fkey", ("user_id",), "users", ("id",)),
                ("posts_category_id_fkey", ("category_id",), "categories", ("id",)),
            ],
        ),
        table(
            "comments",
            ["id", "post_id", "author_id", "editor_id", "body"],
            nullable=["author_id", "editor_id"],
            kinds={"body": TEXT},
            references=[
                ("comments_post_id_fkey", ("post_id",), "posts", ("id",)),
                ("comments_author_id_fkey", ("author_id",), "users", ("id",)),
                ("comments_editor_id_fkey", ("editor_id",), "users", ("id",)),
            ],
        ),
        table(
            "events",
            ["id", "subject_type", "subject_id"],
            kinds={"subject_type": TEXT},
        ),
        table(
            "store",
            ["id", "manager_id"],
            references=[("store_manager_id_fkey", ("manager_id",), "staff", ("id",))],
        ),
        table(
            "staff",
            ["id", "store_id", "boss_id"],
            nullable=["boss_id"],
            references=[
                ("staff_store_id_fkey", ("store_id",), "store", ("id",)),
                ("staff_boss_id_fkey", ("boss_id",), "staff", ("id",)),
            ],
        ),
        table("dba_maint_runs", ["id"]),
        table("log", ["id", "user_id"], namespace="audit"),
        table("film_actor", ["film_id", "actor_id"], key=("film_id", "actor_id")),
    )
