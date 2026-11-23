# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Package planning: what each image needs to work on its own.

Subset planning decides which rows to read. Package planning then asks, for
each image group, what the group's tables and declared objects need to work
in its image. It starts from the group's tables, its **roots** (the objects
``objects:`` in the spec and the group declare, which code outside Datadouble
expects) and the objects that come with its tables (triggers on them, views
over them alone), and follows, transitively and across namespaces:

- references from a table up to its parents, where the reference is followed
  (a fallback such as ``on_missing: null`` says the parent may be missing),
  and down to its children where the spec follows them (``follow.down``);
- what a table's definition uses: a default's sequence or function, a
  column's user-defined type;
- every object's dependency edges, exact and inferred.

The result is the group's **closure**. A ``standalone`` group pulls
everything reached that it does not include into its image: objects with
their definitions, and tables with the rows its rows reference. Other groups
pull nothing in, so they follow only what their tables and roots need of
objects; references between groups stay with ``requires:`` and fallbacks.

What an image cannot have fails the plan, naming the object and the path to
it: an excluded table an object needs (copies leave such an object out), or
an object the catalog does not list. When an inferred edge is on the path the
problem only warns, since inferred edges never fail a plan on their own.

The core knows nothing of how an engine creates an object; it records each
group's objects in dependency order, and the adapters create them from their
definitions.
"""

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

from datadouble.catalog import Catalog, ObjectKind, ObjectRef, SourceObject
from datadouble.names import GroupName, TableName
from datadouble.subset import Edge, EdgeMode

type Node = TableName | ObjectRef


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class PulledRows(StrEnum):
    """Which rows of a table a standalone group pulls in."""

    REFERENCED = "referenced"
    """The rows the group's rows reference (the default)."""
    ALL = "all"
    """Every row the extract holds."""
    NONE = "none"
    """None: the structure only."""


class Pulled(_Model):
    """A table or object a standalone group's closure pulled into its image."""

    table: TableName | None = None
    object: ObjectRef | None = None
    path: tuple[str, ...]
    """How the closure reached it: the group's table or root it started from,
    each table or object after that, and it."""
    inferred: bool = False
    """Whether an inferred edge is on the path, so a person should check it."""
    rows: PulledRows | None = None
    """For a table, which of its rows come with it."""

    @model_validator(mode="after")
    def _one_target(self) -> "Pulled":
        if (self.table is None) == (self.object is None):
            message = "a pulled-in item is either a table or an object"
            raise ValueError(message)
        return self

    def __str__(self) -> str:
        """``table shop.public.users (rows: referenced) from ...``."""
        target = f"table {self.table}" if self.object is None else str(self.object)
        rows = "" if self.rows is None else f" (rows: {self.rows})"
        hint = ", by an inferred dependency" if self.inferred else ""
        return f"{target}{rows}, reached by {' -> '.join(self.path)}{hint}"


@dataclass(frozen=True, slots=True)
class Packaging:
    """What package planning reads: the live catalog and the subset plan's choices."""

    catalog: Catalog
    excluded: frozenset[TableName]
    structure_only: frozenset[TableName]
    edges: tuple[Edge, ...]


@dataclass(frozen=True, slots=True)
class _Step:
    path: tuple[Node, ...]
    inferred: bool


@dataclass(slots=True)
class Closure:
    """One group's closure, and what it found that an image cannot have."""

    objects: tuple[ObjectRef, ...] = ()
    """Every object the image needs, dependencies first."""
    pulled: tuple[Pulled, ...] = ()
    """What a standalone group pulls in that it does not include."""
    tables: frozenset[TableName] = frozenset()
    """The tables the image holds: the group's own, and those a standalone group pulls in."""
    nodes: frozenset[Node] = frozenset()
    """Every table and object reached, the group's own tables included."""
    problems: list[str] = field(default_factory=list[str])
    warnings: list[str] = field(default_factory=list[str])
    broken: dict[ObjectRef, str] = field(default_factory=dict[ObjectRef, str])
    """Objects on a failing path, with the problem; strict mode names them."""


def label(node: Node) -> str:
    """``table shop.public.users`` or ``function shop.public.total(integer)``."""
    return f"table {node}" if isinstance(node, TableName) else str(node)


_ROUTINES = frozenset({ObjectKind.FUNCTION, ObjectKind.PROCEDURE, ObjectKind.PACKAGE})
_PULLS_PARENTS = frozenset({EdgeMode.FOLLOW, EdgeMode.ALL})


class _Walk:
    """One group's walk over the catalog's tables, references and objects."""

    def __init__(
        self,
        packaging: Packaging,
        name: GroupName,
        own: frozenset[TableName],
        *,
        standalone: bool,
    ) -> None:
        self.packaging = packaging
        self.name = name
        self.own = own
        self.standalone = standalone
        self.objects = {item.ref: item for item in packaging.catalog.objects}
        self.tables = {
            table.name: table
            for table in packaging.catalog.tables
            if table.name not in packaging.excluded
        }
        self.reached: dict[Node, _Step] = {}
        self.present: set[TableName] = set(own)
        self.queue: deque[Node] = deque()
        self.closure = Closure()
        self.attaching: dict[TableName, list[SourceObject]] = {}
        for item in packaging.catalog.objects:
            for table in _exact_tables(item):
                self.attaching.setdefault(table, []).append(item)
        self.related: dict[TableName, list[TableName]] = {}
        for edge in packaging.edges:
            # A reference to a table that is excluded, or not in the catalog,
            # already fails the subset plan.
            if edge.mode in _PULLS_PARENTS and edge.parent in self.tables:
                self.related.setdefault(edge.child, []).append(edge.parent)
            if edge.down and edge.child in self.tables:
                self.related.setdefault(edge.parent, []).append(edge.child)

    def run(self, roots: Iterable[ObjectRef]) -> Closure:
        for table in sorted(self.own, key=str):
            self.reach(table, _Step((table,), inferred=False))
        for root in roots:
            self.reach(root, _Step((root,), inferred=False))
        while self.queue:
            node = self.queue.popleft()
            if isinstance(node, TableName):
                self.visit_table(node)
            else:
                self.visit_object(node)
        found = [n for n in self.reached if isinstance(n, ObjectRef) and n in self.objects]
        self.closure.objects = _ordered(found, self.objects)
        self.closure.nodes = frozenset(self.reached)
        self.closure.tables = frozenset(self.present)
        self.closure.pulled = self.pulled()
        return self.closure

    def reach(self, node: Node, step: _Step) -> None:
        if node in self.reached:
            return
        self.reached[node] = step
        if isinstance(node, TableName) and node not in self.own:
            if not self.standalone:
                return  # Another group's, reached through requires or a fallback.
            self.present.add(node)
        self.queue.append(node)

    def through(self, node: Node, target: Node, *, inferred: bool) -> _Step:
        step = self.reached[node]
        return _Step((*step.path, target), inferred=step.inferred or inferred)

    def visit_table(self, name: TableName) -> None:
        if self.standalone:
            for target in self.related.get(name, []):
                self.reach(target, self.through(name, target, inferred=False))
        for edge in self.tables[name].depends_on:
            self.reach(edge.target, self.through(name, edge.target, inferred=edge.inferred))
        for item in self.attaching.get(name, []):
            if set(_exact_tables(item)) <= self.present:
                self.reach(item.ref, self.through(name, item.ref, inferred=False))

    def visit_object(self, ref: ObjectRef) -> None:
        item = self.objects.get(ref)
        if item is None:
            self.unresolved(ref, ref, "is not in the catalog", inferred=False)
            return
        for edge in item.depends_on:
            target = edge.target
            if isinstance(target, TableName):
                self.needs_table(ref, target, inferred=edge.inferred)
            else:
                self.reach(target, self.through(ref, target, inferred=edge.inferred))
        if self.standalone and self.is_pulled(ref) and _inferred(item):
            self.closure.warnings.append(
                f"group {self.name!r}: {ref}'s dependencies are inferred from its body; "
                "check that the image has what it uses, or declare that in objects"
            )

    def needs_table(self, ref: ObjectRef, table: TableName, *, inferred: bool) -> None:
        if table in self.packaging.excluded:
            how = "may need" if inferred else "needs"
            reason = f"{how} {label(table)}, which is excluded, so copies leave it out"
            self.unresolved(ref, ref, reason, inferred=inferred)
        elif table not in self.tables:
            self.unresolved(
                ref, ref, f"needs {label(table)}, which is not in the catalog", inferred=inferred
            )
        else:
            self.reach(table, self.through(ref, table, inferred=inferred))

    def unresolved(self, ref: ObjectRef, subject: Node, reason: str, *, inferred: bool) -> None:
        step = self.reached[ref]
        text = (
            f"group {self.name!r}: {label(subject)} {reason}; reached by "
            f"{' -> '.join(label(node) for node in step.path)}"
        )
        if inferred or step.inferred:
            self.closure.warnings.append(f"{text} (an inferred dependency: check it)")
            return
        self.closure.problems.append(text)
        for node in step.path:
            if isinstance(node, ObjectRef):
                self.closure.broken.setdefault(node, text)

    def is_pulled(self, ref: ObjectRef) -> bool:
        """Whether an object reached is pulled in.

        Roots are not, nor objects that come with the group's own tables (a
        trigger on one, a view over them alone).
        """
        if len(self.reached[ref].path) == 1:
            return False
        item = self.objects.get(ref)
        tables = set(_exact_tables(item)) if item is not None else set[TableName]()
        return not (tables and tables <= self.own)

    def pulled(self) -> tuple[Pulled, ...]:
        if not self.standalone:
            return ()
        pulled: list[Pulled] = []
        for node, step in self.reached.items():
            path = tuple(label(n) for n in step.path)
            if isinstance(node, TableName):
                if node in self.own:
                    continue
                rows = (
                    PulledRows.NONE
                    if node in self.packaging.structure_only
                    else PulledRows.REFERENCED
                )
                pulled.append(Pulled(table=node, path=path, inferred=step.inferred, rows=rows))
            elif node in self.objects and self.is_pulled(node):
                pulled.append(Pulled(object=node, path=path, inferred=step.inferred))
        return tuple(pulled)


def _exact_tables(item: SourceObject) -> tuple[TableName, ...]:
    """The tables an object's exact edges name: those it comes with in a copy."""
    return tuple(e.table for e in item.depends_on if e.table is not None and not e.inferred)


def _inferred(item: SourceObject) -> bool:
    """Whether a routine has dependencies inferred from its body."""
    return item.ref.kind in _ROUTINES and any(e.inferred for e in item.depends_on)


def _ordered(
    found: list[ObjectRef], objects: dict[ObjectRef, SourceObject]
) -> tuple[ObjectRef, ...]:
    """``found`` in dependency order: each object after those it needs, cycles broken by name."""
    members = set(found)
    done: dict[ObjectRef, None] = {}
    visiting: set[ObjectRef] = set()

    def visit(ref: ObjectRef) -> None:
        if ref in done or ref in visiting:
            return
        visiting.add(ref)
        needs = [e.object for e in objects[ref].depends_on if e.object is not None]
        for need in sorted(dict.fromkeys(n for n in needs if n in members), key=str):
            visit(need)
        visiting.discard(ref)
        done[ref] = None

    for ref in sorted(found, key=str):
        visit(ref)
    return tuple(done)


def close(
    packaging: Packaging,
    name: GroupName,
    tables: Iterable[TableName],
    roots: Iterable[ObjectRef],
    *,
    standalone: bool,
) -> Closure:
    """The closure of one group: its tables, its roots and everything they need."""
    walk = _Walk(packaging, name, frozenset(tables), standalone=standalone)
    return walk.run(roots)
