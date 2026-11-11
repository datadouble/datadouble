# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The planner: from a spec, a live catalog and a lock to an execution plan.

Planning resolves every name in the spec, applies excludes and strict mode,
builds the reference graph with its fallbacks, assigns tables to image groups,
chooses a strategy and checks the anonymization rules. It reports every
problem it finds, not just the first.
"""

import datetime as dt
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import assert_never

from pydantic import AwareDatetime, BaseModel, ConfigDict

from datadouble.catalog import Catalog, CatalogLock, Column, ColumnKind, ForeignKey, Table
from datadouble.names import ColumnRef, EngineName, GroupName, SpecName, TableName
from datadouble.resolve import Reference, ResolveError, Resolver
from datadouble.spec import (
    Assertion,
    EdgePolicy,
    ExpressionRule,
    FakerKind,
    FakerRule,
    FormatPreservingRule,
    HashRule,
    ImageFormat,
    NullRule,
    OnMissing,
    Rule,
    Spec,
    Strategy,
    StrictLevel,
)
from datadouble.strict import Excludes, StrictReport, check_strict, resolve_excludes
from datadouble.subset import (
    Edge,
    EdgeMode,
    Graph,
    SubsetError,
    TableRule,
    nested_conditions,
)


class Capabilities(BaseModel):
    """What an engine adapter can do, as far as planning is concerned."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    formats: tuple[ImageFormat, ...]
    expressions: bool
    """Whether the expression strategy is available (an engine-native language)."""
    nested: bool
    """Whether strategy A is available (the engine evaluates nested selections)."""


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
    """One image group: its tables and how references leaving it are handled."""

    name: GroupName
    tables: tuple[TableName, ...]
    requires: tuple[GroupName, ...]
    omitted_references: tuple[str, ...]
    """References whose constraints are left out because the parent is elsewhere."""
    fixups: tuple[Fixup, ...]


class PiiAllow(_Model):
    """Values the PII scan accepts in one column, as ``LIKE`` patterns."""

    column: ColumnRef
    patterns: tuple[str, ...]


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
    """The live catalog minus excluded tables."""
    warnings: tuple[str, ...]

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
    strict: StrictLevel | None = None
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

    def attempt[**P, T](
        self, action: Callable[P, T], *args: P.args, **kwargs: P.kwargs
    ) -> T | None:
        try:
            return action(*args, **kwargs)
        except ResolveError as error:
            self.problems.append(str(error))
            return None

    # Excludes and strict mode

    def strict_report(self, excluded: frozenset[TableName]) -> StrictReport | None:
        level = self.options.strict or self.spec.strict
        if self.lock is None:
            if level is StrictLevel.OFF:
                self.warnings.append("no catalog lock: strict mode cannot compare anything")
            else:
                self.problems.append(
                    f"strict mode is {level} but there is no catalog lock; "
                    "write one with `datadouble lock`"
                )
            return None
        report = check_strict(
            self.catalog, self.lock.catalog, self.lock.fingerprint, excluded, level
        )
        self.warnings.extend(
            f"strict mode: {finding.difference} {finding.object}: {finding.detail}"
            for finding in report.findings
            if finding.severity == "warn"
        )
        return report

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

    def groups(
        self, kept: tuple[TableName, ...], edges: list[Edge], rules: dict[TableName, TableRule]
    ) -> tuple[PlannedGroup, ...]:
        members: dict[GroupName, tuple[TableName, ...]] = {
            name: tuple(
                table
                for table in kept
                if any(self.resolver.matches(pattern, table) for pattern in group.include)
            )
            for name, group in self.spec.images.items()
        }
        placed = {table for tables in members.values() for table in tables}
        self.warnings.extend(
            f"{table} is in no image group: its rows are extracted but not packaged"
            for table in kept
            if table not in placed
        )
        planned: list[PlannedGroup] = []
        for name, group in self.spec.images.items():
            tables = set(members[name])
            reachable = self.required(name)
            omitted: list[str] = []
            fixups: list[Fixup] = []
            for edge in edges:
                if edge.child not in tables:
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
        allowed: frozenset[ColumnKind] | None
        match rule:
            case FakerRule():
                allowed = _FAKER_KINDS.get(rule.faker, _TEXT)
            case HashRule() | FormatPreservingRule():
                allowed = _TEXT_OR_INTEGER
            case NullRule():
                allowed = None
                if not column.nullable:
                    self.problems.append(f"{ref} cannot be null, so the null rule cannot apply")
            case ExpressionRule():
                allowed = None
                if not self.capabilities.expressions:
                    self.problems.append(
                        f"{ref}: the {self.catalog.engine} adapter has no expression language"
                    )
            case _:
                assert_never(rule)
        if allowed is not None and column.kind not in allowed:
            kinds = ", ".join(sorted(allowed))
            self.problems.append(f"{ref} is a {column.kind} column; this rule needs {kinds}")

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
        return tuple(dict.fromkeys(wanted))

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

    def seeds(self, rules: dict[TableName, TableRule], excluded: frozenset[TableName]) -> None:
        for text, predicate in self.spec.subset.seeds.items():
            table = self.attempt(self.resolver.table, text)
            if table is None:
                continue
            if table.name in excluded:
                self.problems.append(f"seed {text!r} names {table.name}, which is excluded")
            elif not rules[table.name].rows_kept:
                self.problems.append(f"seed {text!r} names {table.name}, which is structure-only")
            else:
                rules[table.name] = rules[table.name].model_copy(update={"seed": predicate})

    def plan(self) -> Plan:
        excludes = resolve_excludes(self.resolver, self.spec.exclude, self.spec.structure_only)
        excluded = excludes.excluded()
        structure_only = excludes.structure_only_tables()
        strict = self.strict_report(excluded)
        if strict is not None and strict.failed():
            failures = tuple(
                f"strict mode ({strict.level}): {f.difference} {f.object}: {f.detail}"
                for f in strict.findings
                if f.severity == "fail"
            )
            raise PlanError(failures, strict)
        kept_tables = [t for t in self.catalog.tables if t.name not in excluded]
        rules = {
            table.name: TableRule(table=table.name, rows_kept=table.name not in structure_only)
            for table in kept_tables
        }
        rules.update({name: TableRule(table=name, rows_kept=False) for name in excluded})
        self.seeds(rules, excluded)
        edges = self.edges(kept_tables)
        for edge in edges:
            self.check_edge(edge, rules, excluded)
        graph = Graph(rules, tuple(edges))
        kept = tuple(table.name for table in kept_tables)
        groups = self.groups(kept, edges, rules)
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
        formats = self.formats()
        strategy = self.strategy(graph)
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
            catalog=self.catalog.without(excluded),
            warnings=tuple(self.warnings),
        )


_FALLBACKS: dict[OnMissing, EdgeMode] = {
    OnMissing.NULL: EdgeMode.NULL,
    OnMissing.STUB: EdgeMode.STUB,
    OnMissing.DROP: EdgeMode.DROP,
}


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
    """Plan a build; raises :class:`PlanError` listing every problem found."""
    return _Planner(spec, catalog, lock, capabilities, options or PlanOptions()).plan()
