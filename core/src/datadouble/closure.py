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

The result is the group's **closure**. A ``standalone`` group starts from
the objects its ``include`` patterns match too, and pulls everything reached
that it does not include into its image: objects with their definitions, and
tables with the rows ``rows:`` asks for (by default those its rows
reference; :func:`pulled_rows` reads them). Other groups pull nothing in, so
they follow only what their tables and roots need of objects; references
between groups stay with ``requires:`` and fallbacks, and so do objects
another group's patterns place.

What an image cannot have fails the plan, naming the object and the path to
it: an excluded table an object needs (copies leave such an object out), or
an object the catalog does not list. When an inferred edge is on the path the
problem only warns, since inferred edges never fail a plan on their own.

The core knows nothing of how an engine creates an object; it records each
group's objects in dependency order, and the adapters create them from their
definitions.
"""

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, model_validator

from datadouble.catalog import Catalog, ObjectKind, ObjectRef, SourceObject
from datadouble.names import GroupName, TableName
from datadouble.spec import PulledRows
from datadouble.subset import (
    AllRows,
    AnyNull,
    Condition,
    Edge,
    EdgeMode,
    KeyReader,
    KeyTuple,
    NoRows,
    Predicate,
    ValuesIn,
    all_of,
    any_of,
)

type Node = TableName | ObjectRef


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


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
    placement: dict[ObjectRef, tuple[GroupName, ...]] = field(
        default_factory=dict[ObjectRef, tuple[GroupName, ...]]
    )
    """The groups whose ``include`` patterns match each object."""


@dataclass(frozen=True, slots=True)
class RowsRule:
    """One ``rows`` override of a standalone group, resolved."""

    key: str
    """As the spec wrote it."""
    rows: PulledRows
    tables: frozenset[TableName] = frozenset()
    """The tables a table key or glob matches."""
    edge: Edge | None = None
    """The reference a ``child.column -> parent`` key names."""


@dataclass(frozen=True, slots=True)
class GroupInput:
    """One image group, as package planning sees it."""

    name: GroupName
    tables: frozenset[TableName]
    roots: tuple[ObjectRef, ...] = ()
    standalone: bool = False
    allowed: frozenset[GroupName] = frozenset()
    """The group and the groups it requires, whose objects it may need."""
    elsewhere: frozenset[TableName] = frozenset()
    """The tables of the groups it requires, provisioned alongside its image."""
    rows: tuple[RowsRule, ...] = ()


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
_VIEWS = frozenset({ObjectKind.VIEW, ObjectKind.MATERIALIZED_VIEW})


class _Walk:
    """One group's walk over the catalog's tables, references and objects."""

    def __init__(self, packaging: Packaging, group: GroupInput) -> None:
        self.packaging = packaging
        self.group = group
        self.name = group.name
        self.own = group.tables
        self.standalone = group.standalone
        self.objects = {item.ref: item for item in packaging.catalog.objects}
        self.included = frozenset(
            ref for ref, groups in packaging.placement.items() if group.name in groups
        )
        self.tables = {
            table.name: table
            for table in packaging.catalog.tables
            if table.name not in packaging.excluded
        }
        self.reached: dict[Node, _Step] = {}
        self.present: set[TableName] = set(group.tables)
        self.queue: deque[Node] = deque()
        self.closure = Closure()
        self.attaching: dict[TableName, list[SourceObject]] = {}
        self.on_views: dict[ObjectRef, list[SourceObject]] = {}
        """Triggers on each view, which come with it."""
        for item in packaging.catalog.objects:
            for table in _exact_tables(item):
                self.attaching.setdefault(table, []).append(item)
            if item.ref.kind is ObjectKind.TRIGGER:
                for need in item.depends_on:
                    if need.object is not None and need.object.kind in _VIEWS and not need.inferred:
                        self.on_views.setdefault(need.object, []).append(item)
        self.related: dict[TableName, list[tuple[TableName, Edge]]] = {}
        for edge in packaging.edges:
            # A reference to a table that is excluded, or not in the catalog,
            # already fails the subset plan.
            if edge.mode in _PULLS_PARENTS and edge.parent in self.tables:
                self.related.setdefault(edge.child, []).append((edge.parent, edge))
            if edge.down and edge.child in self.tables:
                self.related.setdefault(edge.parent, []).append((edge.child, edge))
        self.outside: list[tuple[ObjectRef, TableName, bool]] = []
        """Tables outside a group that is not standalone, the objects needing them,
        and whether by inferred edges."""
        self.reaching: dict[TableName, list[Edge]] = {}
        """The followed references that reach each table from the image."""

    def run(self) -> Closure:
        for table in sorted(self.own, key=str):
            self.reach(table, _Step((table,), inferred=False))
        for root in self.group.roots:
            self.reach(root, _Step((root,), inferred=False))
        if self.standalone:
            # A standalone image holds the objects its patterns include.
            for ref in sorted(self.included, key=str):
                self.reach(ref, _Step((ref,), inferred=False))
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
        if not self.standalone:
            self.placed(found)
            self.unheld()
        return self.closure

    def placed(self, found: list[ObjectRef]) -> None:
        """Fail objects the group needs that only groups it does not require include.

        Objects that come with the group's own tables (a trigger on one, a
        view over them alone) and its declared objects are its own, whatever
        the patterns say.
        """
        for ref in found:
            groups = self.packaging.placement.get(ref, ())
            if not groups or set(groups) & self.group.allowed or not self.is_pulled(ref):
                continue
            path = " -> ".join(label(node) for node in self.reached[ref].path)
            others = ", ".join(repr(g) for g in groups)
            self.closure.problems.append(
                f"group {self.name!r}: {ref} is in group {others}; add `requires:` for it, "
                f"or make this group standalone; reached by {path}"
            )

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
            for related, reference in self.related.get(name, []):
                self.reaching.setdefault(related, []).append(reference)
                self.reach(related, self.through(name, related, inferred=False))
        for edge in self.tables[name].depends_on:
            target = edge.target
            if isinstance(target, TableName) and target not in self.tables:
                # A policy or check reading a table copies leave out.
                self.closure.warnings.append(
                    f"group {self.name!r}: {label(name)} has a policy or check that reads "
                    f"{label(target)}, which is excluded or not in the catalog, so the image "
                    "may lack it"
                )
                continue
            self.reach(target, self.through(name, target, inferred=edge.inferred))
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
        for trigger in self.on_views.get(ref, []):
            self.reach(trigger.ref, self.through(ref, trigger.ref, inferred=False))
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
            if not self.standalone and table not in self.own and table not in self.group.elsewhere:
                self.outside.append((ref, table, inferred))
            self.reach(table, self.through(ref, table, inferred=inferred))

    def unheld(self) -> None:
        """Fail declared objects, and objects they need, that need a table no image has.

        A group that is not standalone pulls nothing in, so such an object
        would be missing from its image. An object counts as declared when
        any declared object reaches it, whatever reached it first.
        """
        rooted = self.rooted()
        for ref, table, inferred in self.outside:
            if ref not in rooted:
                continue
            nodes: tuple[Node, ...] = (*rooted[ref], table)
            path = " -> ".join(label(node) for node in nodes)
            text = (
                f"group {self.name!r}: {label(ref)} needs {label(table)}, which group "
                f"{self.name!r} does not hold; include it, or add `requires:` for a group "
                f"holding it; reached by {path}"
            )
            if inferred:
                self.closure.warnings.append(f"{text} (an inferred dependency: check it)")
            else:
                self.closure.problems.append(text)
                self.closure.broken.setdefault(ref, text)

    def rooted(self) -> dict[ObjectRef, tuple[ObjectRef, ...]]:
        """Each object a declared object reaches through objects, with a path from it."""
        found: dict[ObjectRef, tuple[ObjectRef, ...]] = {}
        pending: deque[tuple[ObjectRef, tuple[ObjectRef, ...]]] = deque(
            (root, (root,)) for root in self.group.roots
        )
        while pending:
            ref, path = pending.popleft()
            if ref in found:
                continue
            found[ref] = path
            item = self.objects.get(ref)
            for edge in item.depends_on if item is not None else ():
                if edge.object is not None and edge.object not in found:
                    pending.append((edge.object, (*path, edge.object)))
        return found

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
        trigger on one, a view over them alone, a trigger on such a view).
        """
        if len(self.reached[ref].path) == 1 or ref in self.included:
            return False
        item = self.objects[ref]
        tables = set(_exact_tables(item))
        if tables:
            return not tables <= self.own
        views = [
            e.object
            for e in item.depends_on
            if ref.kind is ObjectKind.TRIGGER
            and e.object is not None
            and e.object.kind in _VIEWS
            and e.object in self.reached
        ]
        return not views or any(self.is_pulled(view) for view in views)

    def pulled(self) -> tuple[Pulled, ...]:
        if not self.standalone:
            return ()
        pulled: list[Pulled] = []
        used: set[str] = set()
        for node, step in self.reached.items():
            path = tuple(label(n) for n in step.path)
            if isinstance(node, TableName):
                if node in self.own:
                    continue
                rows = self.rows(node, used)
                pulled.append(Pulled(table=node, path=path, inferred=step.inferred, rows=rows))
            elif node in self.objects and self.is_pulled(node):
                pulled.append(Pulled(object=node, path=path, inferred=step.inferred))
        self.closure.problems.extend(
            f"group {self.name!r}: rows: {rule.key!r} matches no table its closure pulls in"
            for rule in self.group.rows
            if rule.key not in used
        )
        return tuple(pulled)

    def rows(self, table: TableName, used: set[str]) -> PulledRows:
        """Which rows of a pulled-in table come with it.

        A reference key wins over a table key; with neither, a table a
        reference reaches comes with the rows referenced, and one reached only
        through objects comes without rows.
        """
        reaching = self.reaching.get(table, [])
        by_edge = [r for r in self.group.rows if r.edge is not None and r.edge in reaching]
        by_table = [r for r in self.group.rows if table in r.tables]
        chosen = by_edge or by_table
        used.update(rule.key for rule in (*by_edge, *by_table))
        modes = {rule.rows for rule in chosen}
        if len(modes) > 1:
            keys = ", ".join(repr(rule.key) for rule in chosen)
            self.closure.problems.append(
                f"group {self.name!r}: rows {keys} disagree about {label(table)}"
            )
        wanted = (
            chosen[0].rows if chosen else PulledRows.REFERENCED if reaching else PulledRows.NONE
        )
        if table in self.packaging.structure_only:
            if wanted is not PulledRows.NONE and chosen:
                self.closure.problems.append(
                    f"group {self.name!r}: rows asks for rows of {label(table)}, "
                    "which is structure-only"
                )
            return PulledRows.NONE
        if not chosen and not reaching:
            self.closure.warnings.append(
                f"group {self.name!r}: {label(table)} is reached only through objects or "
                "policies, not references, so it comes without rows; set `rows` for it to "
                "copy some"
            )
        return wanted


def _exact_tables(item: SourceObject) -> tuple[TableName, ...]:
    """The tables an object's exact edges name: those it comes with in a copy."""
    return tuple(e.table for e in item.depends_on if e.table is not None and not e.inferred)


def _inferred(item: SourceObject) -> bool:
    """Whether a routine has dependencies inferred from its body."""
    return item.ref.kind in _ROUTINES and any(e.inferred for e in item.depends_on)


def ordered(found: Iterable[ObjectRef], catalog: Iterable[SourceObject]) -> tuple[ObjectRef, ...]:
    """Objects in dependency order: each after those it needs, cycles broken by name.

    Adapters create objects in this order, and drop them in its reverse.
    """
    return _ordered(list(found), {item.ref: item for item in catalog})


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


def close(packaging: Packaging, group: GroupInput) -> Closure:
    """The closure of one group: its tables, its roots and everything they need."""
    return _Walk(packaging, group).run()


_KEEPS_PARENTS = frozenset({EdgeMode.FOLLOW, EdgeMode.ALL, EdgeMode.DROP})
"""Edges whose child rows need their parents in the image: followed ones, and
``drop`` ones, whose child rows were kept because their parents were chosen."""


class _Rows:
    """The rows of a standalone image beyond the shared extract, read by keys.

    An own table's rows are its shared ones (``baseline``) and the extra rows
    references bring to it. Only the extra rows lead anywhere new between own
    tables, since the shared extract already holds what the shared rows need.
    """

    def __init__(
        self,
        edges: Iterable[Edge],
        own: frozenset[TableName],
        modes: Mapping[TableName, PulledRows],
        baseline: Mapping[TableName, Condition],
    ) -> None:
        self.own, self.modes, self.baseline = own, modes, baseline
        # Own tables the subset keeps without rows (structure-only) never gain any.
        self.rowless = frozenset(t for t in own if isinstance(baseline[t], NoRows))
        self.up: list[Edge] = []
        self.down: list[Edge] = []
        self.drops: list[Edge] = []
        """``drop`` references to parents that gain no rows: the rows a table
        gains keep them null, as the shared extract keeps its own."""
        for edge in edges:
            if edge.mode in _KEEPS_PARENTS and self.fills(edge.child) and self.takes(edge.parent):
                self.up.append(edge)
            elif edge.mode is EdgeMode.DROP and self.fills(edge.child):
                self.drops.append(edge)
            if edge.down and self.fills(edge.parent) and self.takes(edge.child):
                self.down.append(edge)
        self.values: dict[tuple[Edge, bool], set[KeyTuple]] = {}
        """Values read along each edge, upward (False) or downward (True)."""
        self.parents: dict[Edge, set[KeyTuple]] = {}
        """The parent keys each reference may hold, once rows are dropped."""

    def fills(self, table: TableName) -> bool:
        """Whether ``table`` is in the image with rows that can reference others."""
        if table in self.own:
            return table not in self.rowless
        return self.modes.get(table) in {PulledRows.REFERENCED, PulledRows.ALL}

    def takes(self, table: TableName) -> bool:
        """Whether ``table`` gains the rows references bring.

        Own tables with rows do, and ``referenced`` pulled-in ones.
        """
        if table in self.own:
            return table not in self.rowless
        return self.modes.get(table) is PulledRows.REFERENCED

    def kept(self, table: TableName, condition: Condition) -> Condition:
        """``condition`` without the rows the image cannot keep.

        A ``drop`` reference to a parent without rows drops a row, as the
        shared extract does; so does a reference to a parent row dropped so.
        """
        return all_of(
            [
                condition,
                *(AnyNull(e.columns) for e in self.drops if e.child == table),
                *(
                    any_of([AnyNull(e.columns), ValuesIn(e.columns, frozenset(keys))])
                    for e, keys in self.parents.items()
                    if e.child == table
                ),
            ]
        )

    def prune(self, reader: KeyReader) -> None:
        """Drop the rows whose parent rows were dropped, until nothing changes.

        Only a ``drop`` reference to a parent without rows drops a row the
        image gained, so without one there is nothing to do. Parent rows only
        leave, so the loop ends.
        """
        if not self.drops:
            return
        references = list(dict.fromkeys((*self.up, *self.down)))
        changed = True
        while changed:
            changed = False
            for edge in references:
                parent = self.condition(edge.parent)
                keys = {
                    row
                    for row in reader.project(edge.parent, parent, edge.parent_columns)
                    if all(value is not None for value in row)
                }
                if self.parents.get(edge) != keys:
                    self.parents[edge] = keys
                    changed = True

    def brought(self, table: TableName) -> Condition:
        """The rows references bring to ``table``, beyond the shared ones."""
        return any_of(
            [
                *(
                    ValuesIn(e.parent_columns, frozenset(self.values.get((e, False), ())))
                    for e in self.up
                    if e.parent == table
                ),
                *(
                    _when(e, ValuesIn(e.columns, frozenset(self.values.get((e, True), ()))))
                    for e in self.down
                    if e.child == table
                ),
            ]
        )

    def condition(self, table: TableName) -> Condition:
        if table in self.own:
            return any_of([self.baseline[table], self.gains(table)])
        if self.modes[table] is PulledRows.ALL or any(
            e.parent == table and e.mode is EdgeMode.ALL for e in self.up
        ):
            return self.kept(table, AllRows())
        return self.gains(table)

    def gains(self, table: TableName) -> Condition:
        """The rows references bring to ``table`` that it keeps."""
        return self.kept(table, self.brought(table))

    def gained(self, table: TableName) -> bool:
        """Whether references brought ``table`` any rows."""
        return any(self.values.get((e, False)) for e in self.up if e.parent == table) or any(
            self.values.get((e, True)) for e in self.down if e.child == table
        )

    def step(self, table: TableName, reader: KeyReader) -> set[TableName]:
        """Re-read what ``table``'s rows reference; the tables that gained rows."""
        full = self.condition(table)
        extra = self.gains(table) if table in self.own else full
        grown: set[TableName] = set()
        outgoing = [(e, False, e.columns, e.when, e.parent) for e in self.up if e.child == table]
        outgoing += [
            (e, True, e.parent_columns, None, e.child) for e in self.down if e.parent == table
        ]
        for edge, downward, columns, when, target in outgoing:
            # Between own tables, the shared rows' references are already in place.
            condition = extra if target in self.own else full
            if isinstance(condition, NoRows):
                continue
            narrowed = all_of([condition, Predicate(when)]) if when else condition
            fresh = {
                row
                for row in reader.project(table, narrowed, columns)
                if all(value is not None for value in row)
            }
            known = self.values.setdefault((edge, downward), set())
            if not fresh <= known:
                known |= fresh
                grown.add(target)
        return grown


def _when(edge: Edge, condition: Condition) -> Condition:
    return all_of([condition, Predicate(edge.when)]) if edge.when else condition


def pulled_rows(
    edges: Iterable[Edge],
    own: frozenset[TableName],
    modes: Mapping[TableName, PulledRows],
    baseline: Mapping[TableName, Condition],
    reader: KeyReader,
) -> dict[TableName, Condition]:
    """The rows a standalone group's image needs beyond the shared extract.

    ``modes`` gives each pulled-in table's rows, and ``baseline`` the
    extract's conditions of the group's own tables, standing alone (as the
    keys strategy computes them). Starting from those rows, references are
    followed up to parents and down where the spec follows them, reading
    keys through ``reader`` until nothing changes, as the keys strategy does.
    The result holds a condition for each pulled-in table with rows, and for
    each own table that gained rows, so every reference in the image keeps
    its parent: a gained row's own references are followed too, up to
    parents along followed and ``drop`` references and down where the spec
    follows them, among own tables as well.
    """
    state = _Rows(edges, own, modes, baseline)
    pending = sorted(own | frozenset(modes), key=str)
    queued = set(pending)
    while pending:
        table = pending.pop(0)
        queued.discard(table)
        for grown in sorted(state.step(table, reader), key=str):
            if grown not in queued:
                queued.add(grown)
                pending.append(grown)
    state.prune(reader)
    result = {
        table: state.condition(table)
        for table, mode in modes.items()
        if mode is not PulledRows.NONE
    }
    result.update((table, state.condition(table)) for table in own if state.gained(table))
    return result
