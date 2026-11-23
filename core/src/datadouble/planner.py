# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The planner: from a spec, a live catalog and a lock to an execution plan.

Planning resolves every name in the spec, applies excludes and strict mode,
builds the reference graph with its fallbacks, assigns tables to image groups,
works out what each group's image needs (package planning, in
:mod:`datadouble.closure`), chooses a strategy and checks the anonymization
rules. It reports every problem it finds, not just the first.
"""

import datetime as dt
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from typing import assert_never

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from datadouble import jailer
from datadouble.catalog import (
    Catalog,
    CatalogLock,
    Column,
    ColumnKind,
    ForeignKey,
    ObjectRef,
    Requirement,
    Table,
)
from datadouble.closure import Closure, GroupInput, Packaging, Pulled, RowsRule, close
from datadouble.names import ColumnName, ColumnRef, EngineName, GroupName, SpecName, TableName
from datadouble.resolve import Reference, ResolveError, Resolver
from datadouble.spec import (
    AccountPrivileges,
    Assertion,
    EdgePolicy,
    ExpressionRule,
    FakerKind,
    FakerRule,
    FormatPreservingRule,
    HashRule,
    ImageFormat,
    NullRule,
    ObjectRoot,
    OnMissing,
    PathsFrom,
    PulledRows,
    Rule,
    Spec,
    Strategy,
    Strict,
    StrictLevel,
    StrictOverrides,
    Unselected,
)
from datadouble.strict import (
    Difference,
    Excludes,
    Finding,
    StrictReport,
    check_strict,
    resolve_excludes,
)
from datadouble.subset import (
    Edge,
    EdgeMode,
    Graph,
    SubsetError,
    TableRule,
    nested_conditions,
)


class AccountNames(BaseModel):
    """The user names an engine can give copies' accounts (``package.accounts``)."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    max_characters: int | None = None
    """The longest name, in characters."""
    max_bytes: int | None = None
    """The longest name, in bytes of UTF-8."""
    reserved: tuple[str, ...] = ()
    """Names the engine or Datadouble keeps for itself, such as its build user."""
    reserved_prefixes: tuple[str, ...] = ()
    """Prefixes the engine keeps for its own names."""
    keep_all: tuple[str, ...] = ()
    """Names an account can have only with ``privileges: all``, such as a bootstrap
    superuser that cannot lose its privileges."""

    def problems(self, engine: str, user: str, privileges: AccountPrivileges) -> list[str]:
        """Why ``engine`` cannot give an account ``user`` these privileges, if it cannot."""
        found: list[str] = []
        if user in self.keep_all and privileges is not AccountPrivileges.ALL:
            found.append(
                f"package.accounts: {user!r} must keep every privilege on {engine}"
                " (privileges: all)"
            )
        if self.max_characters is not None and len(user) > self.max_characters:
            found.append(
                f"package.accounts: {user!r} is longer than the {self.max_characters}"
                f" characters {engine} allows"
            )
        if self.max_bytes is not None and len(user.encode()) > self.max_bytes:
            found.append(
                f"package.accounts: {user!r} is longer than the {self.max_bytes}"
                f" bytes {engine} allows"
            )
        if user in self.reserved:
            found.append(f"package.accounts: {user!r} is a name {engine} keeps for itself")
        found.extend(
            f"package.accounts: {user!r} starts with {prefix!r}, which {engine} keeps for itself"
            for prefix in self.reserved_prefixes
            if user.startswith(prefix)
        )
        return found


class Capabilities(BaseModel):
    """What an engine adapter can do, as far as planning is concerned."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    formats: tuple[ImageFormat, ...]
    expressions: bool
    """Whether the expression strategy is available (an engine-native language)."""
    nested: bool
    """Whether strategy A is available (the engine evaluates nested selections)."""
    hooks: bool = True
    """Whether ``before_load`` and ``after_transform`` hooks can run."""
    complete: bool = False
    """Whether every table must be seeded or excluded, as a file tree's top-level
    directories must be: nothing in the source goes unconsidered."""
    version_line: int = Field(default=1, ge=1)
    """How many leading numbers of a server version name its compatibility line:
    1 for PostgreSQL (``17``), 2 for MySQL (``8.4``), whose minor releases change
    what a data directory holds."""
    accounts: AccountNames | None = None
    """The names physical images' accounts (``package.accounts``) can have; None
    when the adapter cannot give images accounts."""
    standalone: bool = False
    """Whether the adapter can build standalone images: load what a closure
    pulls in, and leave out of the image every object the closure lacks."""
    joins_outermost: bool = True
    """Whether one query can join tables in different outermost namespaces.
    PostgreSQL's databases cannot be joined, so a virtual reference between
    two of them fails the plan."""


class PlanError(Exception):
    """A spec that cannot be planned against the catalog."""

    def __init__(self, problems: tuple[str, ...], strict: StrictReport | None = None) -> None:
        """Keep every problem, and the strict-mode report if it failed."""
        count = len(problems)
        super().__init__(f"the plan has {count} problem{'s' if count != 1 else ''}")
        self.problems = problems
        self.strict = strict


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class PlannedRule(_Model):
    """An anonymization rule resolved to a column."""

    column: ColumnRef
    kind: ColumnKind
    type: str
    """The column's type as the engine spells it."""
    rule: Rule


class Fixup(_Model):
    """A fallback applied to a working database at load."""

    edge: Edge
    mode: EdgeMode
    parent_present: bool
    """Whether the parent table is in the same working database."""


class PlannedGroup(_Model):
    """One image group: its tables, what it needs, and how references leaving it are handled."""

    name: GroupName
    tables: tuple[TableName, ...]
    requires: tuple[GroupName, ...]
    omitted_references: tuple[str, ...]
    """References whose constraints are left out because the parent is elsewhere."""
    fixups: tuple[Fixup, ...]
    standalone: bool = False
    """Whether the image must work on its own, holding everything in ``pulled``."""
    roots: tuple[ObjectRef, ...] = ()
    """The objects the spec and the group declare in ``objects``."""
    objects: tuple[ObjectRef, ...] = ()
    """Every object the image needs, dependencies first: those that come with
    its tables, its roots, and what they need."""
    pulled: tuple[Pulled, ...] = ()
    """What a standalone group's closure pulled in from outside the group, and from where."""

    def packaged(self) -> tuple[TableName, ...]:
        """The tables its image holds: its own, then those its closure pulls in."""
        return (*self.tables, *(p.table for p in self.pulled if p.table is not None))

    def rowless(self) -> frozenset[TableName]:
        """Pulled-in tables that come without rows."""
        return frozenset(
            p.table for p in self.pulled if p.table is not None and p.rows is PulledRows.NONE
        )

    def rows(self) -> dict[TableName, PulledRows]:
        """Which rows of each pulled-in table its image holds."""
        return {p.table: p.rows for p in self.pulled if p.table is not None and p.rows}


class PiiAllow(_Model):
    """Values the PII scan accepts in one column, as ``LIKE`` patterns."""

    column: ColumnRef
    patterns: tuple[str, ...]


_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


class PlannedPaths(_Model):
    """How the chosen rows of one table name files: ``files.paths_from``, resolved."""

    table: TableName
    columns: tuple[ColumnName, ...]
    """The columns the template names, in the order its placeholders first use them."""
    template: str
    """The path, with ``{column}`` for each column's value."""
    match: str | None = None
    """A regular expression whose first group, where it matches, is the path."""

    def path(self, values: tuple[object, ...]) -> str | None:
        """The path one row names, given its values of ``columns``; None if it names none."""
        if any(value is None for value in values):
            return None
        by_column = dict(zip(self.columns, (str(value) for value in values), strict=True))
        filled = _PLACEHOLDER.sub(lambda found: by_column[ColumnName(found[1])], self.template)
        if self.match is None:
            return filled
        matched = re.search(self.match, filled)
        return matched[1] if matched else None


class PlannedSeed(_Model):
    """One ``subset.seeds`` key and the tables it gave a seed to."""

    pattern: str
    """The key as the spec wrote it: a table name or a glob."""
    tables: tuple[TableName, ...]
    """The tables it gave its condition to; a table's own name wins over a pattern."""


class Plan(_Model):
    """Everything the later stages need, recorded in the plan's manifest."""

    spec: SpecName
    engine: EngineName
    as_of: AwareDatetime
    strategy: Strategy
    catalog_fingerprint: str
    excludes: Excludes
    strict: StrictReport | None
    tables: tuple[TableRule, ...]
    edges: tuple[Edge, ...]
    groups: tuple[PlannedGroup, ...]
    formats: tuple[ImageFormat, ...]
    anonymization: tuple[PlannedRule, ...]
    rewrites: tuple[ColumnRef, ...]
    pii_allow: tuple[PiiAllow, ...]
    assertions: tuple[Assertion, ...]
    catalog: Catalog
    """The live catalog minus excluded tables and omitted requirements."""
    omitted: tuple[Requirement, ...] = ()
    """The source's requirements that ``source.omit`` leaves out."""
    warnings: tuple[str, ...]
    paths_from: PlannedPaths | None = None
    """How the chosen rows name the files to copy too."""
    seeds: tuple[PlannedSeed, ...] = ()
    """Each seed key and the tables it matched."""
    unselected: tuple[TableName, ...] = ()
    """Kept tables no seed or followed reference selected, handled by ``subset.unselected``."""

    def graph(self) -> Graph:
        """The selection graph."""
        return Graph({rule.table: rule for rule in self.tables}, self.edges)

    def extracted(self) -> tuple[TableName, ...]:
        """Tables whose definitions and rows are extracted."""
        excluded = self.excludes.excluded()
        return tuple(rule.table for rule in self.tables if rule.table not in excluded)


class PlanOptions(_Model):
    """Overrides from the request, applied over the spec."""

    now: AwareDatetime | None = None
    """The time ``as_of`` defaults to when the spec has none; the current time if omitted."""
    strict: StrictOverrides | None = None
    """Levels set over the spec's ``strict``, for the kinds named."""
    strategy: Strategy | None = None
    formats: tuple[ImageFormat, ...] | None = None


_FAKER_KINDS: dict[FakerKind, frozenset[ColumnKind]] = {
    FakerKind.DATE_OF_BIRTH: frozenset({ColumnKind.DATE, ColumnKind.TIMESTAMP, ColumnKind.TEXT}),
}
_TEXT = frozenset({ColumnKind.TEXT})
_TEXT_OR_INTEGER = frozenset({ColumnKind.TEXT, ColumnKind.INTEGER})


class _Planner:
    def __init__(
        self,
        spec: Spec,
        catalog: Catalog,
        lock: CatalogLock | None,
        capabilities: Capabilities,
        options: PlanOptions,
    ) -> None:
        self.spec = spec
        self.catalog = catalog
        self.lock = lock
        self.capabilities = capabilities
        self.options = options
        self.resolver = Resolver(catalog, case_sensitive=spec.case_sensitive)
        self.problems: list[str] = []
        self.warnings: list[str] = []
        self.group_roots: dict[GroupName, tuple[ObjectRef, ...]] = {}
        self.closures: dict[GroupName, Closure] = {}

    def attempt[**P, T](
        self, action: Callable[P, T], *args: P.args, **kwargs: P.kwargs
    ) -> T | None:
        try:
            return action(*args, **kwargs)
        except ResolveError as error:
            self.problems.append(str(error))
            return None

    # Excludes and strict mode

    def levels(self) -> Strict:
        overrides = self.options.strict
        return self.spec.strict if overrides is None else overrides.over(self.spec.strict)

    def strict_report(self, excluded: frozenset[TableName]) -> StrictReport | None:
        strict = self.levels()
        if self.lock is None:
            if strict.data is StrictLevel.OFF and not strict.failing():
                self.warnings.append("no catalog lock: strict mode cannot compare anything")
            else:
                self.problems.append(
                    "strict mode cannot compare the catalog without a catalog lock; "
                    "write one with `datadouble lock`"
                )
            return None
        report = check_strict(self.catalog, self.lock, excluded, strict, self.breaks)
        self.warnings.extend(
            f"strict mode: {finding}" for finding in report.findings if finding.severity == "warn"
        )
        return report

    def breaks(self, finding: Finding) -> str | None:
        """What a difference from the lock breaks, if anything.

        A changed column breaks its anonymization rule when the rule no longer
        fits it. A removed object breaks a packaged table or a root that still
        needs it; a new or changed object breaks an image whose closure reaches
        it and then something the image cannot have (package planning).
        """
        if finding.ref is not None:
            return self.object_breaks(finding, finding.ref)
        if finding.difference is not Difference.COLUMN_CHANGED:
            return None
        found = self.rules_by_column().get(finding.object)
        if found is None:
            return None
        column, rule = found
        misfit = _misfit(column, rule)
        return None if misfit is None else f"its anonymization rule no longer fits: {misfit}"

    def object_breaks(self, finding: Finding, ref: ObjectRef) -> str | None:
        """What a new, changed or removed object breaks in the images, if anything."""
        if finding.difference is not Difference.OBJECT_REMOVED:
            reasons = [c.broken[ref] for c in self.closures.values() if ref in c.broken]
            return "; ".join(dict.fromkeys(reasons)) or None
        # Strict mode compares only with a lock.
        lock = self.lock.catalog if self.lock is not None else self.catalog
        packaged = {t for closure in self.closures.values() for t in closure.tables}
        needing = [
            f"packaged table {table.name} still needs it"
            for table in lock.tables
            if table.name in packaged
            and any(edge.object == ref and not edge.inferred for edge in table.depends_on)
        ]
        locked = Resolver(lock, case_sensitive=self.spec.case_sensitive)
        for name, roots in self.declared():
            for root in roots:
                kind, text = next(iter(root.items()))
                try:
                    declared = locked.object(kind, text)
                except ResolveError:
                    continue
                if declared.ref == ref:
                    needing.append(f"{_declarer(name)} declares it in objects")
        return "; ".join(dict.fromkeys(needing)) or None

    def declared(self) -> list[tuple[GroupName | None, tuple[ObjectRoot, ...]]]:
        """The spec's roots, and each group's."""
        return [
            (None, self.spec.objects),
            *((name, group.objects) for name, group in self.spec.images.items()),
        ]

    def roots(self) -> dict[GroupName, tuple[ObjectRef, ...]]:
        """Each group's roots, the spec's first; a root that does not resolve is a problem."""
        resolved: dict[GroupName | None, list[ObjectRef]] = {}
        for name, roots in self.declared():
            found: list[ObjectRef] = []
            for root in roots:
                kind, text = next(iter(root.items()))
                try:
                    found.append(self.resolver.object(kind, text).ref)
                except ResolveError as error:
                    self.problems.append(f"{_declarer(name)}: objects: {error}")
            resolved[name] = found
        shared = resolved[None]
        return {name: tuple(dict.fromkeys((*shared, *resolved[name]))) for name in self.spec.images}

    def rules_by_column(self) -> dict[str, tuple[Column, Rule]]:
        """Each anonymized column, by name, with its rule.

        Names that do not resolve are skipped here and reported by :meth:`anonymization`.
        """
        found: dict[str, tuple[Column, Rule]] = {}
        for table_text, columns in self.spec.anonymize.items():
            try:
                table = self.resolver.table(table_text)
                for column_text, rule in columns.items():
                    column = self.resolver.column_of(table, column_text)
                    found[str(ColumnRef(table=table.name, column=column.name))] = (column, rule)
            except ResolveError:
                continue
        return found

    # References

    def _declared(self, reference: Reference) -> ForeignKey | None:
        candidates = [
            fk
            for fk in reference.child.foreign_keys
            if fk.target == reference.parent.name
            and (reference.columns is None or fk.columns == reference.columns)
            and (reference.parent_columns is None or fk.target_columns == reference.parent_columns)
        ]
        if len(candidates) > 1:
            names = ", ".join(fk.name for fk in candidates)
            message = (
                f"{reference.child.name} has several references to {reference.parent.name} "
                f"({names}); name the columns, as child.column -> parent"
            )
            raise ResolveError(message)
        return candidates[0] if candidates else None

    def virtual_edges(self) -> list[Edge]:
        edges: list[Edge] = []
        for text, virtual in self.spec.subset.virtual_fks.items():
            reference = self.attempt(self.resolver.reference, text)
            if reference is None:
                continue
            if reference.columns is None:
                self.problems.append(f"virtual reference {text!r} must name the child columns")
                continue
            child, parent = reference.child.name, reference.parent.name
            if not self.capabilities.joins_outermost and child.outermost != parent.outermost:
                self.problems.append(
                    f"virtual reference {text!r} joins {child.outermost} and {parent.outermost},"
                    f" which one {self.catalog.engine} query cannot read together"
                )
                continue
            parent_columns = reference.parent_columns or reference.parent.primary_key
            if len(parent_columns) != len(reference.columns):
                self.problems.append(
                    f"virtual reference {text!r} has {len(reference.columns)} child column(s) "
                    f"and {len(parent_columns)} parent column(s)"
                )
                continue
            edges.append(
                Edge(
                    name=f"virtual: {text}",
                    child=reference.child.name,
                    columns=reference.columns,
                    parent=reference.parent.name,
                    parent_columns=parent_columns,
                    when=virtual.when,
                    virtual=True,
                    parent_unique=parent_columns == reference.parent.primary_key,
                )
            )
        return edges

    def find_edge(self, edges: list[Edge], reference: Reference, text: str) -> int | None:
        try:
            declared = self._declared(reference)
        except ResolveError as error:
            self.problems.append(str(error))
            return None
        for index, edge in enumerate(edges):
            if edge.child != reference.child.name or edge.parent != reference.parent.name:
                continue
            if declared is not None and not edge.virtual and edge.name == declared.name:
                return index
            if edge.virtual and (reference.columns is None or edge.columns == reference.columns):
                return index
        self.problems.append(f"{text!r} matches no reference in the catalog or virtual_fks")
        return None

    def edges(self, tables: Iterable[Table]) -> list[Edge]:
        edges = [
            Edge(
                name=fk.name,
                child=table.name,
                columns=fk.columns,
                parent=fk.target,
                parent_columns=fk.target_columns,
            )
            for table in tables
            for fk in table.foreign_keys
        ]
        edges.extend(self.virtual_edges())
        for text, policy in self.spec.subset.edges.items():
            reference = self.attempt(self.resolver.reference, text)
            index = None if reference is None else self.find_edge(edges, reference, text)
            if index is not None:
                edges[index] = replace(edges[index], mode=_mode(policy))
        for text in self.spec.subset.follow.down:
            reference = self.attempt(self.resolver.downward, text)
            index = None if reference is None else self.find_edge(edges, reference, text)
            if index is not None:
                edges[index] = replace(edges[index], down=True)
        return edges

    def check_edge(
        self, edge: Edge, rules: dict[TableName, TableRule], excluded: frozenset[TableName]
    ) -> None:
        parent_rule = rules.get(edge.parent)
        parent_keeps_rows = parent_rule is not None and parent_rule.rows_kept
        if edge.mode in {EdgeMode.FOLLOW, EdgeMode.ALL} and not parent_keeps_rows:
            why = "is excluded" if edge.parent in excluded else "keeps no rows"
            self.problems.append(
                f"{edge.child} references {edge.parent} ({edge.name}), which {why}; "
                "give the reference a fallback: on_missing null, stub or drop"
            )
        child = self.catalog.table(edge.child)
        if child is not None and edge.mode is EdgeMode.NULL:
            fixed = [c for c in edge.columns if not (col := child.column(c)) or not col.nullable]
            if fixed:
                self.problems.append(
                    f"{edge.name}: on_missing null needs nullable columns, but "
                    f"{', '.join(fixed)} cannot be null"
                )
        if edge.mode is EdgeMode.STUB:
            self.check_stub(edge, parent_keeps_rows=parent_keeps_rows)
        if edge.mode is EdgeMode.DROP and edge.when is not None:
            self.problems.append(
                f"{edge.name}: on_missing drop is not supported on a reference with `when`; "
                "use on_missing null"
            )

    def check_stub(self, edge: Edge, *, parent_keeps_rows: bool) -> None:
        parent = self.catalog.table(edge.parent)
        if parent is None or not parent_keeps_rows:
            self.problems.append(
                f"{edge.name}: on_missing stub needs {edge.parent} in the image, with rows"
            )
            return
        own = {c for fk in parent.foreign_keys for c in fk.columns} - set(edge.parent_columns)
        blocked = [c.name for c in parent.columns if c.name in own and not c.nullable]
        if blocked:
            self.problems.append(
                f"{edge.name}: cannot stub {edge.parent}, whose references "
                f"{', '.join(blocked)} cannot be null"
            )

    # Groups

    def placement(self) -> dict[ObjectRef, tuple[GroupName, ...]]:
        """The groups whose ``include`` patterns match each object, from the right."""
        placed: dict[ObjectRef, tuple[GroupName, ...]] = {}
        for item in self.catalog.objects:
            parts = (*item.ref.namespace, item.ref.name)
            groups = tuple(
                name
                for name, group in self.spec.images.items()
                if any(self.resolver.matches_parts(pattern, parts) for pattern in group.include)
            )
            if groups:
                placed[item.ref] = groups
        return placed

    def rows_rules(self, name: GroupName, edges: list[Edge]) -> tuple[RowsRule, ...]:
        """A group's ``rows`` overrides, resolved: references to edges, tables to names."""
        found: list[RowsRule] = []
        for key, rows in self.spec.images[name].rows.items():
            if "->" in key:
                reference = self.attempt(self.resolver.reference, key)
                index = None if reference is None else self.find_edge(edges, reference, key)
                if index is not None:
                    found.append(RowsRule(key=key, rows=rows, edge=edges[index]))
                continue
            if _is_glob(key):
                tables = self.resolver.glob(key.strip())
            else:
                table = self.attempt(self.resolver.table, key)
                tables = () if table is None else (table.name,)
            if tables:
                found.append(RowsRule(key=key, rows=rows, tables=frozenset(tables)))
            elif _is_glob(key):
                self.problems.append(f"group {name!r}: rows: {key!r} matches no table")
        return tuple(found)

    def check_standalone(self) -> None:
        """Refuse standalone groups the engine adapter cannot build."""
        if self.capabilities.standalone:
            return
        for name, group in self.spec.images.items():
            closure = self.closures[name]
            if group.standalone and (closure.pulled or self.catalog.objects):
                self.problems.append(
                    f"group {name!r} is standalone, and the {self.catalog.engine} adapter "
                    "cannot build standalone images yet"
                )

    def members(self, kept: tuple[TableName, ...]) -> dict[GroupName, tuple[TableName, ...]]:
        """The kept tables each group's patterns include."""
        return {
            name: tuple(
                table
                for table in kept
                if any(self.resolver.matches(pattern, table) for pattern in group.include)
            )
            for name, group in self.spec.images.items()
        }

    def groups(
        self,
        kept: tuple[TableName, ...],
        edges: list[Edge],
        rules: dict[TableName, TableRule],
        members: dict[GroupName, tuple[TableName, ...]],
    ) -> tuple[PlannedGroup, ...]:
        placed = {table for tables in members.values() for table in tables}
        placed |= {t for closure in self.closures.values() for t in closure.tables}
        self.warnings.extend(
            f"{table} is in no image group: its rows are extracted but not packaged"
            for table in kept
            if table not in placed
        )
        planned: list[PlannedGroup] = []
        for name, group in self.spec.images.items():
            closure = self.closures[name]
            # A standalone group holds what its closure pulls in, too; a
            # pulled-in table without rows cannot keep references to it.
            tables = set(closure.tables)
            rowless = {p.table for p in closure.pulled if p.rows is PulledRows.NONE}
            reachable = self.required(name)
            omitted: list[str] = []
            fixups: list[Fixup] = []
            for edge in edges:
                if edge.child not in tables:
                    continue
                if edge.parent in rowless and edge.child not in rowless:
                    omitted.append(edge.name)
                    continue
                parent_here = edge.parent in tables
                parent_rule = rules.get(edge.parent)
                parent_rows = parent_rule is not None and parent_rule.rows_kept
                if not parent_here:
                    omitted.append(edge.name)
                    self.check_crossing(
                        name, edge, parent_rows=parent_rows, members=members, reachable=reachable
                    )
                if edge.mode in {EdgeMode.NULL, EdgeMode.STUB}:
                    fixups.append(Fixup(edge=edge, mode=edge.mode, parent_present=parent_here))
            planned.append(
                PlannedGroup(
                    name=name,
                    tables=members[name],
                    requires=group.requires,
                    omitted_references=tuple(omitted),
                    fixups=tuple(fixups),
                    standalone=group.standalone,
                    roots=self.group_roots[name],
                    objects=closure.objects,
                    pulled=closure.pulled,
                )
            )
        return tuple(planned)

    def required(self, name: GroupName) -> frozenset[GroupName]:
        seen: set[GroupName] = set()
        pending = list(self.spec.images[name].requires)
        while pending:
            group = pending.pop()
            if group not in seen:
                seen.add(group)
                pending.extend(self.spec.images[group].requires)
        return frozenset(seen)

    def check_crossing(
        self,
        name: GroupName,
        edge: Edge,
        *,
        parent_rows: bool,
        members: dict[GroupName, tuple[TableName, ...]],
        reachable: frozenset[GroupName],
    ) -> None:
        if not parent_rows or edge.mode is EdgeMode.NULL:
            return
        if edge.mode in {EdgeMode.STUB, EdgeMode.DROP}:
            self.problems.append(
                f"group {name!r}: {edge.name} leaves the group with on_missing "
                f"{edge.mode}, which version 1 supports only within a group; "
                "use on_missing null or `requires`"
            )
            return
        if not any(edge.parent in members[group] for group in reachable):
            self.problems.append(
                f"group {name!r}: {edge.child} references {edge.parent} ({edge.name}) in "
                "another group; add `requires:` for a group containing it, or give the "
                "reference a fallback"
            )

    # Anonymization

    def anonymization(
        self, excluded: frozenset[TableName], key_columns: frozenset[ColumnRef]
    ) -> tuple[PlannedRule, ...]:
        planned: list[PlannedRule] = []
        for table_text, columns in self.spec.anonymize.items():
            table = self.attempt(self.resolver.table, table_text)
            if table is None:
                continue
            if table.name in excluded:
                self.problems.append(f"anonymization rules name {table.name}, which is excluded")
                continue
            for column_text, rule in columns.items():
                column = self.attempt(self.resolver.column_of, table, column_text)
                if column is None:
                    continue
                ref = ColumnRef(table=table.name, column=column.name)
                self.check_rule(ref, column, rule, key_columns)
                planned.append(
                    PlannedRule(column=ref, kind=column.kind, type=column.type, rule=rule)
                )
        return tuple(planned)

    def check_rule(
        self,
        ref: ColumnRef,
        column: Column,
        rule: Rule,
        key_columns: frozenset[ColumnRef],
    ) -> None:
        if ref in key_columns:
            self.problems.append(
                f"{ref} is part of a primary or foreign key; anonymizing key columns "
                "is not supported"
            )
        if isinstance(rule, ExpressionRule) and not self.capabilities.expressions:
            self.problems.append(
                f"{ref}: the {self.catalog.engine} adapter has no expression language"
            )
        misfit = _misfit(column, rule)
        if misfit is not None:
            self.problems.append(f"{ref} {misfit}")

    # The whole plan

    def formats(self) -> tuple[ImageFormat, ...]:
        wanted = self.options.formats or self.spec.package.formats or self.capabilities.formats
        unsupported = [f for f in wanted if f not in self.capabilities.formats]
        if unsupported:
            supported = ", ".join(self.capabilities.formats)
            self.problems.append(
                f"the {self.catalog.engine} adapter cannot produce "
                f"{', '.join(unsupported)} images; it supports {supported}"
            )
        self.accounts(wanted)
        return tuple(dict.fromkeys(wanted))

    def accounts(self, wanted: tuple[ImageFormat, ...]) -> None:
        """Check ``package.accounts`` against the adapter and the formats."""
        accounts = self.spec.package.accounts
        if not accounts:
            return
        users = [account.user for account in accounts]
        self.problems.extend(
            f"package.accounts names {user!r} more than once"
            for user in dict.fromkeys(users)
            if users.count(user) > 1
        )
        engine = self.catalog.engine
        names = self.capabilities.accounts
        if names is None:
            self.problems.append(
                f"the {engine} adapter cannot give copies accounts (package.accounts)"
            )
            return
        if ImageFormat.PHYSICAL not in wanted:
            self.problems.append(
                "package.accounts applies to physical images, and this build makes none"
            )
        firsts = {account.user: account for account in reversed(accounts)}
        for user in dict.fromkeys(users):
            self.problems.extend(names.problems(engine, user, firsts[user].privileges))

    def strategy(self, graph: Graph) -> Strategy:
        wanted = self.options.strategy or self.spec.subset.strategy
        acyclic = graph.is_acyclic() and self.capabilities.nested
        if wanted is Strategy.AUTO:
            return Strategy.NESTED if acyclic else Strategy.KEYS
        if wanted is Strategy.NESTED:
            if not self.capabilities.nested:
                self.problems.append(
                    f"the {self.catalog.engine} adapter cannot run the nested strategy"
                )
            else:
                try:
                    nested_conditions(graph)
                except SubsetError as error:
                    self.problems.append(f"{error}; the nested strategy cannot run, use keys")
        return wanted

    def seeds(
        self, rules: dict[TableName, TableRule], excluded: frozenset[TableName]
    ) -> tuple[PlannedSeed, ...]:
        """Give each seeded table its condition; a table's own name wins over a pattern."""
        exact: dict[TableName, tuple[str, str]] = {}
        by_pattern: dict[TableName, list[tuple[str, str]]] = {}
        for text, predicate in self.spec.subset.seeds.items():
            if _is_glob(text):
                for name in self.seed_pattern(text, rules, excluded):
                    by_pattern.setdefault(name, []).append((text, predicate))
            elif (seeded := self.seed_table(text, rules, excluded)) is not None:
                if seeded in exact:
                    self.problems.append(
                        f"seeds {exact[seeded][0]!r} and {text!r} both name {seeded}"
                    )
                exact[seeded] = (text, predicate)
        for name, found in by_pattern.items():
            if name in exact:
                continue
            if len(found) > 1:
                patterns = ", ".join(repr(text) for text, _ in found)
                self.problems.append(
                    f"{name} matches more than one seed pattern ({patterns}); "
                    "give it a seed of its own, or narrow the patterns"
                )
            else:
                exact[name] = found[0]
        seeded_by: dict[str, list[TableName]] = {text: [] for text in self.spec.subset.seeds}
        for name, (text, predicate) in exact.items():
            rules[name] = rules[name].model_copy(update={"seed": predicate})
            seeded_by[text].append(name)
        return tuple(
            PlannedSeed(pattern=text, tables=tuple(sorted(names, key=str)))
            for text, names in seeded_by.items()
        )

    def seed_pattern(
        self, text: str, rules: dict[TableName, TableRule], excluded: frozenset[TableName]
    ) -> tuple[TableName, ...]:
        """The tables a seed glob gives rows to: those it matches that keep rows."""
        matched = tuple(
            name
            for name in self.resolver.glob(text.strip())
            if name not in excluded and rules[name].rows_kept
        )
        if not matched:
            self.problems.append(f"seed pattern {text!r} matches no table that keeps rows")
        return matched

    def seed_table(
        self, text: str, rules: dict[TableName, TableRule], excluded: frozenset[TableName]
    ) -> TableName | None:
        """The table a seed names, if it can take a seed."""
        table = self.attempt(self.resolver.table, text)
        if table is None:
            return None
        if table.name in excluded:
            self.problems.append(f"seed {text!r} names {table.name}, which is excluded")
            return None
        if not rules[table.name].rows_kept:
            self.problems.append(f"seed {text!r} names {table.name}, which is structure-only")
            return None
        return table.name

    def unselected(
        self, rules: dict[TableName, TableRule], edges: list[Edge], kept: tuple[TableName, ...]
    ) -> tuple[TableName, ...]:
        """Kept tables nothing selects rows from, handled as ``subset.unselected`` says."""
        if self.capabilities.complete:
            return ()  # Every table must already be seeded or excluded.
        keeps = frozenset(name for name in kept if rules[name].rows_kept)
        chosen = _reached(rules, edges, keeps)
        missing = tuple(name for name in kept if name in keeps and name not in chosen)
        policy = self.spec.subset.unselected or Unselected.FAIL
        match policy:
            case Unselected.FAIL:
                self.problems.extend(
                    f"no seed or followed reference selects rows of {name}; seed it "
                    "(seed keys take globs), follow a reference to it, make it "
                    "structure_only, exclude it, or set subset.unselected to none or all"
                    for name in missing
                )
            case Unselected.NONE:
                self.warnings.extend(
                    f"no seed or followed reference selects rows of {name}; "
                    "it is copied without rows (subset.unselected: none)"
                    for name in missing
                )
            case Unselected.ALL:
                for name in missing:
                    rules[name] = rules[name].model_copy(update={"every_row": True})
            case _:
                assert_never(policy)
        return missing

    def plan(self) -> Plan:
        excludes = resolve_excludes(self.resolver, self.spec.exclude, self.spec.structure_only)
        excluded = excludes.excluded()
        structure_only = excludes.structure_only_tables()
        kept_tables = [t for t in self.catalog.tables if t.name not in excluded]
        kept = tuple(table.name for table in kept_tables)
        # Package planning comes before strict mode, which asks it what a
        # difference from the lock breaks.
        edges = self.edges(kept_tables)
        members = self.members(kept)
        self.group_roots = self.roots()
        packaging = Packaging(
            self.catalog, excluded, structure_only, tuple(edges), self.placement()
        )
        self.closures = {
            name: close(
                packaging,
                GroupInput(
                    name=name,
                    tables=frozenset(members[name]),
                    roots=self.group_roots[name],
                    standalone=group.standalone,
                    allowed=self.required(name) | {name},
                    rows=self.rows_rules(name, edges),
                ),
            )
            for name, group in self.spec.images.items()
        }
        self.check_standalone()
        strict = self.strict_report(excluded)
        if strict is not None and strict.failed():
            failures = tuple(f"strict mode: {f}" for f in strict.findings if f.severity == "fail")
            raise PlanError(failures, strict)
        for closure in self.closures.values():
            self.problems.extend(closure.problems)
            self.warnings.extend(closure.warnings)
        rules = {
            table.name: TableRule(table=table.name, rows_kept=table.name not in structure_only)
            for table in kept_tables
        }
        rules.update({name: TableRule(table=name, rows_kept=False) for name in excluded})
        seeds = self.seeds(rules, excluded)
        if self.capabilities.complete:
            self.problems.extend(
                f"{rule.table} is neither selected nor excluded"
                for rule in rules.values()
                if rule.seed is None and rule.table not in excluded and rule.rows_kept
            )
        for edge in edges:
            self.check_edge(edge, rules, excluded)
        unselected = self.unselected(rules, edges, kept)
        graph = Graph(rules, tuple(edges))
        groups = self.groups(kept, edges, rules, members)
        key_columns = frozenset(
            [ColumnRef(table=e.child, column=c) for e in edges for c in e.columns]
            + [ColumnRef(table=e.parent, column=c) for e in edges for c in e.parent_columns]
            + [ColumnRef(table=t.name, column=c) for t in kept_tables for c in t.primary_key]
        )
        anonymization = self.anonymization(excluded, key_columns)
        rewrites = tuple(
            ref
            for hook in self.spec.hooks.after_transform
            for text in hook.rewrites
            if (ref := self.attempt(self.resolver.column, text)) is not None
        )
        pii_allow = tuple(
            PiiAllow(column=ref, patterns=patterns)
            for text, patterns in self.spec.verify.pii_scan.allow.items()
            if (ref := self.attempt(self.resolver.column, text)) is not None
        )
        if not self.capabilities.hooks and (
            self.spec.hooks.before_load or self.spec.hooks.after_transform
        ):
            self.problems.append(f"the {self.catalog.engine} adapter does not run hooks")
        omitted = self.omitted()
        formats = self.formats()
        strategy = self.strategy(graph)
        paths_from = self.paths_from(excluded)
        if self.problems:
            raise PlanError(tuple(self.problems), strict)
        now = self.options.now or dt.datetime.now(tz=dt.UTC)
        as_of = self.spec.subset.as_of or now.replace(microsecond=0)
        return Plan(
            spec=self.spec.name,
            engine=self.catalog.engine,
            as_of=as_of,
            strategy=strategy,
            catalog_fingerprint=self.catalog.fingerprint(),
            excludes=excludes,
            strict=strict,
            tables=tuple(rules[name] for name in (*kept, *sorted(excluded, key=str))),
            edges=tuple(edges),
            groups=groups,
            formats=formats,
            anonymization=anonymization,
            rewrites=rewrites,
            pii_allow=pii_allow,
            assertions=self.spec.verify.assertions,
            catalog=self.catalog.without(excluded).model_copy(
                update={
                    "requirements": tuple(r for r in self.catalog.requirements if r not in omitted)
                }
            ),
            omitted=omitted,
            warnings=tuple(self.warnings),
            paths_from=paths_from,
            seeds=seeds,
            unselected=unselected,
        )

    def omitted(self) -> tuple[Requirement, ...]:
        found: list[Requirement] = []
        for name in self.spec.source.omit:
            requirement = self.catalog.requirement(name)
            if requirement is None:
                self.problems.append(f"source.omit names {name!r}, which the source does not use")
            else:
                found.append(requirement)
        return tuple(found)

    def placeholders(self, table: Table, template: str) -> dict[str, ColumnName]:
        names: dict[str, ColumnName] = {}
        for text in _PLACEHOLDER.findall(template):
            column = self.attempt(self.resolver.column_of, table, text)
            if column is not None:
                names[text] = column.name
        if not names:
            self.problems.append("files.paths_from.path names no column, as {column}")
        return names

    def check_match(self, pattern: str) -> None:
        try:
            groups = re.compile(pattern).groups
        except re.error as error:
            self.problems.append(f"files.paths_from.match is not a regular expression: {error}")
            return
        if groups < 1:
            self.problems.append("files.paths_from.match needs a group for the path")

    def paths_from(self, excluded: frozenset[TableName]) -> PlannedPaths | None:
        files = self.spec.files
        if files is None or files.paths_from is None:
            return None
        given = files.paths_from
        if isinstance(given, str):
            # `table.column` is the template `{column}` over that table.
            ref = self.attempt(self.resolver.column, given)
            if ref is None:
                return None
            given = PathsFrom(table=str(ref.table), path=f"{{{ref.column}}}")
        table = self.attempt(self.resolver.table, given.table)
        if table is None:
            return None
        if table.name in excluded:
            self.problems.append(f"files.paths_from names {table.name}, which is excluded")
        names = self.placeholders(table, given.path)
        if given.match is not None:
            self.check_match(given.match)
        template = _PLACEHOLDER.sub(
            lambda found: f"{{{names.get(found[1], found[1])}}}", given.path
        )
        return PlannedPaths(
            table=table.name,
            columns=tuple(dict.fromkeys(names.values())),
            template=template,
            match=given.match,
        )


def _declarer(group: GroupName | None) -> str:
    """Who declares roots: ``the spec`` or ``group 'billing'``."""
    return "the spec" if group is None else f"group {group!r}"


def _misfit(column: Column, rule: Rule) -> str | None:
    """Why ``rule`` cannot apply to ``column``, if it cannot."""
    allowed: frozenset[ColumnKind] | None
    match rule:
        case FakerRule():
            allowed = _FAKER_KINDS.get(rule.faker, _TEXT)
        case HashRule() | FormatPreservingRule():
            allowed = _TEXT_OR_INTEGER
        case NullRule():
            return None if column.nullable else "cannot be null, so the null rule cannot apply"
        case ExpressionRule():
            return None
        case _:
            assert_never(rule)
    if column.kind in allowed:
        return None
    return f"is a {column.kind} column; this rule needs {', '.join(sorted(allowed))}"


_FALLBACKS: dict[OnMissing, EdgeMode] = {
    OnMissing.NULL: EdgeMode.NULL,
    OnMissing.STUB: EdgeMode.STUB,
    OnMissing.DROP: EdgeMode.DROP,
}


_PULLS_PARENTS = frozenset({EdgeMode.FOLLOW, EdgeMode.ALL})
"""Edge modes under which a chosen child row brings in its parent rows."""


def _is_glob(text: str) -> bool:
    """Whether a seed key is a pattern rather than a table name."""
    return any(character in text for character in "*?[")


def _reached(
    rules: Mapping[TableName, TableRule], edges: Iterable[Edge], keeps: frozenset[TableName]
) -> set[TableName]:
    """Tables among ``keeps`` that a seed, ``follow: all`` or a followed edge gives rows."""
    edges = tuple(edges)
    chosen = {name for name in keeps if rules[name].seed is not None or rules[name].every_row}
    chosen |= {e.parent for e in edges if e.mode is EdgeMode.ALL and e.parent in keeps}
    grew = True
    while grew:
        grew = False
        for edge in edges:
            pulled: list[TableName] = []
            if edge.child in chosen and edge.mode in _PULLS_PARENTS:
                pulled.append(edge.parent)
            if edge.down and edge.parent in chosen:
                pulled.append(edge.child)
            for name in pulled:
                if name in keeps and name not in chosen:
                    chosen.add(name)
                    grew = True
    return chosen


def _mode(policy: EdgePolicy) -> EdgeMode:
    # EdgePolicy holds exactly one of `follow: all` and `on_missing`.
    return EdgeMode.ALL if policy.on_missing is None else _FALLBACKS[policy.on_missing]


def plan(
    spec: Spec,
    catalog: Catalog,
    lock: CatalogLock | None,
    capabilities: Capabilities,
    options: PlanOptions | None = None,
) -> Plan:
    """Plan a build; raises :class:`PlanError` listing every problem found.

    A subset from Jailer is translated against ``catalog`` first; its excluded
    tables join the spec's.
    """
    warnings: tuple[str, ...] = ()
    if spec.subset.jailer is not None:
        try:
            translated = jailer.load(
                spec.subset.jailer.model,
                spec.subset.jailer.datamodel,
                catalog,
                case_sensitive=spec.case_sensitive,
            )
        except jailer.JailerError as error:
            raise PlanError((f"the Jailer subset: {error}",)) from None
        subset = translated.subset.model_copy(
            update={"as_of": spec.subset.as_of, "strategy": spec.subset.strategy}
        )
        exclude = tuple(dict.fromkeys((*spec.exclude, *translated.exclude)))
        spec = spec.model_copy(update={"subset": subset, "exclude": exclude})
        warnings = tuple(f"Jailer subset: {line}" for line in translated.report)
    planned = _Planner(spec, catalog, lock, capabilities, options or PlanOptions()).plan()
    return planned.model_copy(update={"warnings": (*warnings, *planned.warnings)})
