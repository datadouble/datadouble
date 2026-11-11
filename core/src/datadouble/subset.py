# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The subset model, its two execution strategies, and the conditions adapters render.

Each table's selection is the union of its sources (a seed, every row, rows
whose parents were chosen through a followed child edge, rows referenced by
chosen children), restricted by its ``drop`` references. The selection is the
least fixed point of those rules, so cycles are fine.

Strategy A (``nested``) turns each table's rule into a :class:`Condition`
that refers to other tables' selections, for the engine to evaluate in one
query; it needs the dependencies between tables to be acyclic. Strategy B
(``keys``) evaluates the rules here, reading only the key columns it needs
through a :class:`KeyReader`, until nothing changes; its conditions hold the
chosen values themselves. Both give the same rows.
"""

from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from graphlib import CycleError, TopologicalSorter
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from datadouble.names import ColumnName, TableName

type KeyTuple = tuple[Hashable, ...]
"""Values of some columns of one row, as the engine's driver returns them."""


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class EdgeMode(StrEnum):
    """How a reference from a child row to its parent is honoured."""

    FOLLOW = "follow"
    """Pull in the parent rows of every chosen child row (the default)."""
    ALL = "all"
    """Take the whole parent table (``follow: all``)."""
    NULL = "null"
    """Don't pull parents; null references whose parent is missing."""
    STUB = "stub"
    """Don't pull parents; insert placeholder parents for missing ones."""
    DROP = "drop"
    """Don't pull parents; leave out child rows whose parent is missing."""


@dataclass(frozen=True, slots=True)
class Edge:
    """A reference from ``child.columns`` to ``parent.parent_columns``."""

    __pydantic_config__ = ConfigDict(strict=True, extra="forbid")

    name: str
    child: TableName
    columns: tuple[ColumnName, ...]
    parent: TableName
    parent_columns: tuple[ColumnName, ...]
    mode: EdgeMode = EdgeMode.FOLLOW
    down: bool = False
    """Whether chosen parents also pull in their children through this edge."""
    when: str | None = None
    """For virtual references: only child rows matching this native predicate refer."""
    virtual: bool = False


class TableRule(_Model):
    """How one table's rows are chosen."""

    table: TableName
    seed: str | None = None
    every_row: bool = False
    rows_kept: bool = True
    """False for structure-only tables, which keep their definition and no rows."""


# Conditions: what adapters turn into native queries.


@dataclass(frozen=True, slots=True)
class AllRows:
    """Every row."""


@dataclass(frozen=True, slots=True)
class NoRows:
    """No row."""


@dataclass(frozen=True, slots=True)
class Predicate:
    """A native predicate from the spec; ``:as_of`` is bound by the adapter."""

    text: str


@dataclass(frozen=True, slots=True)
class ValuesIn:
    """Rows whose ``columns`` hold one of ``values`` (strategy B)."""

    columns: tuple[ColumnName, ...]
    values: frozenset[KeyTuple]


@dataclass(frozen=True, slots=True)
class SelectedBy:
    """Rows whose ``columns`` match ``source_columns`` of rows chosen in ``source`` (A).

    ``where`` narrows the source rows (a virtual reference's ``when``). When
    ``source_condition`` is set, the source rows are those matching it rather
    than ``source``'s whole selection.
    """

    columns: tuple[ColumnName, ...]
    source: TableName
    source_columns: tuple[ColumnName, ...]
    where: str | None = None
    source_condition: "Condition | None" = None


@dataclass(frozen=True, slots=True)
class AnyNull:
    """Rows where any of ``columns`` is null."""

    columns: tuple[ColumnName, ...]


@dataclass(frozen=True, slots=True)
class AnyOf:
    """Rows matching any part."""

    parts: tuple["Condition", ...]


@dataclass(frozen=True, slots=True)
class AllOf:
    """Rows matching every part."""

    parts: tuple["Condition", ...]


type Condition = AllRows | NoRows | Predicate | ValuesIn | SelectedBy | AnyNull | AnyOf | AllOf


def any_of(parts: Iterable[Condition]) -> Condition:
    """A union, simplified."""
    kept: list[Condition] = []
    for part in parts:
        if isinstance(part, AllRows):
            return part
        if not isinstance(part, NoRows) and not (isinstance(part, ValuesIn) and not part.values):
            kept.append(part)
    if not kept:
        return NoRows()
    return kept[0] if len(kept) == 1 else AnyOf(tuple(kept))


def all_of(parts: Iterable[Condition]) -> Condition:
    """An intersection, simplified."""
    kept: list[Condition] = []
    for part in parts:
        if isinstance(part, NoRows):
            return part
        if not isinstance(part, AllRows):
            kept.append(part)
    if not kept:
        return AllRows()
    return kept[0] if len(kept) == 1 else AllOf(tuple(kept))


class SubsetError(Exception):
    """A subset that cannot be computed as asked."""


@dataclass(frozen=True, slots=True)
class Graph:
    """Table rules and edges, with the lookups both strategies need."""

    rules: Mapping[TableName, TableRule]
    edges: tuple[Edge, ...]
    by_child: Mapping[TableName, tuple[Edge, ...]] = field(init=False)
    by_parent: Mapping[TableName, tuple[Edge, ...]] = field(init=False)

    def __post_init__(self) -> None:
        """Index the edges by table."""
        by_child: dict[TableName, list[Edge]] = {}
        by_parent: dict[TableName, list[Edge]] = {}
        for edge in self.edges:
            by_child.setdefault(edge.child, []).append(edge)
            by_parent.setdefault(edge.parent, []).append(edge)
        object.__setattr__(self, "by_child", {k: tuple(v) for k, v in by_child.items()})
        object.__setattr__(self, "by_parent", {k: tuple(v) for k, v in by_parent.items()})

    def incoming(self, table: TableName) -> tuple[list[Edge], list[Edge], list[Edge]]:
        """Edges that add rows to ``table`` (down, up) and those that restrict it (drop)."""
        down = [e for e in self.by_child.get(table, ()) if e.down]
        up = [e for e in self.by_parent.get(table, ()) if e.mode is EdgeMode.FOLLOW]
        drop = [e for e in self.by_child.get(table, ()) if e.mode is EdgeMode.DROP]
        return down, up, drop

    def takes_all(self, table: TableName) -> bool:
        """Whether every row of ``table`` is chosen."""
        rule = self.rules[table]
        return rule.rows_kept and (
            rule.every_row or any(e.mode is EdgeMode.ALL for e in self.by_parent.get(table, ()))
        )

    def is_acyclic(self) -> bool:
        """Whether strategy A can run."""
        try:
            nested_conditions(self)
        except SubsetError:
            return False
        return True


# Strategy A
#
# A followed child edge parent -> child also lets the child pull in parents
# through the same reference, which would make each table depend on the other.
# Child rows reached through that edge already have their parent chosen, so the
# parent only needs the child rows chosen some other way: that partial
# condition is inlined instead of referring to the child's whole selection.
# This holds when the referenced columns identify one parent row, as declared
# foreign keys do; virtual references keep the plain dependency.


def _nested(graph: Graph, table: TableName, skip: Edge | None, stack: frozenset[Edge]) -> Condition:
    rule = graph.rules[table]
    if not rule.rows_kept:
        return NoRows()
    if graph.takes_all(table):
        return AllRows()
    down, up, drop = graph.incoming(table)
    sources: list[Condition] = [Predicate(rule.seed)] if rule.seed else []
    sources.extend(SelectedBy(e.columns, e.parent, e.parent_columns) for e in down if e != skip)
    for e in up:
        inline = e.down and not e.virtual and e not in stack
        partial = _nested(graph, e.child, e, stack | {e}) if inline else None
        sources.append(SelectedBy(e.parent_columns, e.child, e.columns, e.when, partial))
    return all_of(
        [
            any_of(sources),
            *(
                any_of([AnyNull(e.columns), SelectedBy(e.columns, e.parent, e.parent_columns)])
                for e in drop
            ),
        ]
    )


def _references(condition: Condition) -> set[TableName]:
    """Tables whose whole selections ``condition`` refers to."""
    match condition:
        case SelectedBy(source=source, source_condition=None):
            return {source}
        case SelectedBy(source_condition=partial) if partial is not None:
            return _references(partial)
        case AnyOf(parts) | AllOf(parts):
            return {name for part in parts for name in _references(part)}
        case _:
            return set()


def nested_conditions(graph: Graph) -> dict[TableName, Condition]:
    """Each table's condition in terms of other tables' selections (strategy A).

    Raises :class:`SubsetError` if the conditions refer to each other in a cycle.
    """
    conditions = {table: _nested(graph, table, None, frozenset()) for table in graph.rules}
    sorter = TopologicalSorter({table: _references(c) for table, c in conditions.items()})
    try:
        sorter.prepare()
    except CycleError as error:
        cycle = " -> ".join(str(name) for name in error.args[1])
        message = f"the selection rules form a cycle: {cycle}"
        raise SubsetError(message) from None
    return conditions


# Strategy B


class KeyReader(Protocol):
    """Reads column values of chosen rows from the source snapshot."""

    def project(
        self, table: TableName, condition: Condition, columns: tuple[ColumnName, ...]
    ) -> Iterable[KeyTuple]:
        """Values of ``columns`` for rows of ``table`` matching ``condition``.

        ``condition`` only uses :class:`AllRows`, :class:`Predicate`,
        :class:`ValuesIn`, :class:`AnyNull`, :class:`AnyOf` and :class:`AllOf`.
        """
        ...


def _non_null(rows: Iterable[KeyTuple]) -> set[KeyTuple]:
    return {row for row in rows if all(value is not None for value in row)}


@dataclass
class _KeyState:
    graph: Graph
    reader: KeyReader
    # Values pulled into each edge's target: for a down edge, the parent
    # values its child columns must match; for an up edge, the child values
    # the parent columns must match; for a drop edge, the chosen parent values.
    values: dict[Edge, set[KeyTuple]] = field(default_factory=dict[Edge, set[KeyTuple]])

    def condition(self, table: TableName) -> Condition:
        graph = self.graph
        rule = graph.rules[table]
        if not rule.rows_kept:
            return NoRows()
        if graph.takes_all(table):
            return AllRows()
        down, up, drop = graph.incoming(table)
        sources: list[Condition] = [Predicate(rule.seed)] if rule.seed else []
        sources.extend(ValuesIn(e.columns, frozenset(self.values.get(e, ()))) for e in down)
        sources.extend(ValuesIn(e.parent_columns, frozenset(self.values.get(e, ()))) for e in up)
        return all_of(
            [
                any_of(sources),
                *(
                    any_of(
                        [AnyNull(e.columns), ValuesIn(e.columns, frozenset(self.values.get(e, ())))]
                    )
                    for e in drop
                ),
            ]
        )

    def outgoing(self, table: TableName) -> list[tuple[Edge, tuple[ColumnName, ...], str | None]]:
        """Edges whose values come from ``table``'s chosen rows."""
        graph = self.graph
        found: list[tuple[Edge, tuple[ColumnName, ...], str | None]] = [
            (edge, edge.parent_columns, None)
            for edge in graph.by_parent.get(table, ())
            if edge.down or edge.mode is EdgeMode.DROP
        ]
        found.extend(
            (edge, edge.columns, edge.when)
            for edge in graph.by_child.get(table, ())
            if edge.mode is EdgeMode.FOLLOW
        )
        return found

    def target(self, edge: Edge, source: TableName) -> TableName:
        return edge.child if source == edge.parent else edge.parent

    def step(self, table: TableName) -> set[TableName]:
        """Re-read ``table``'s outgoing values; return the tables that gained any."""
        condition = self.condition(table)
        grown: set[TableName] = set()
        if isinstance(condition, NoRows):
            return grown
        for edge, columns, when in self.outgoing(table):
            narrowed = all_of([condition, Predicate(when)]) if when else condition
            fresh = _non_null(self.reader.project(table, narrowed, columns))
            known = self.values.setdefault(edge, set())
            if not fresh <= known:
                known |= fresh
                grown.add(self.target(edge, table))
        return grown


def key_conditions(graph: Graph, reader: KeyReader) -> dict[TableName, Condition]:
    """Compute the selection by reading keys until nothing changes (strategy B).

    Selections only grow, so the loop ends; each pass re-reads only tables
    whose inputs changed.
    """
    state = _KeyState(graph, reader)
    pending = [table for table in graph.rules if graph.rules[table].rows_kept]
    queued = set(pending)
    while pending:
        table = pending.pop(0)
        queued.discard(table)
        for grown in sorted(state.step(table), key=str):
            if grown not in queued:
                queued.add(grown)
                pending.append(grown)
    return {table: state.condition(table) for table in graph.rules}
