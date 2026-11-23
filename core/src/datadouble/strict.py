# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Excludes and strict mode.

Excludes are resolved first, against the live catalog. Strict mode then
compares what is left with the catalog lock. ``strict.data`` sets how tables,
columns and requirements are treated:

=========================================  =======  ========  ===========
Difference from the lock                   off      tables    columns
=========================================  =======  ========  ===========
Known table or column removed              fail     fail      fail
Known table's keys changed                 fail     fail      fail
Known column's type or nullability changed warn     warn      fail
New table matching an exclude pattern      ignored  ignored   ignored
New table, not excluded                    warn     fail      fail
New column in a known table                warn     warn      fail
Requirement removed or at another version  fail     fail      fail
New requirement                            warn     fail      fail
=========================================  =======  ========  ===========

A difference that breaks something fails whatever the level. The caller says
which do through a :data:`Breaks` function, since only planning knows what a
difference breaks: a changed column whose anonymization rule no longer fits
it, for one.

Requirements are the extensions, plugins and modules the source uses; see
:class:`~datadouble.catalog.Requirement`.

Each kind of object (functions and procedures, views, triggers, types,
sequences, events) has its own level: ``off`` compares nothing, ``warn``
names each new, changed or removed object, and ``fail`` fails on any
difference. A removed object that a remaining object still depends on breaks
that object, so it fails at every level, naming both. Everything else an
object difference might break (a packaged table needing a changed function, a
new trigger needing what an image cannot have) is for the planner's
:data:`Breaks` function to say.
"""

from collections.abc import Callable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from datadouble.catalog import (
    Catalog,
    CatalogLock,
    Dependency,
    ObjectKind,
    ObjectRef,
    Requirement,
    SourceObject,
    Table,
)
from datadouble.names import TableName
from datadouble.resolve import Resolver
from datadouble.spec import ObjectLevel, Strict, StrictKind, StrictLevel


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class PatternMatch(_Model):
    """The tables one exclude or structure-only pattern matched."""

    pattern: str
    tables: tuple[TableName, ...]


class Excludes(_Model):
    """Excludes and structure-only patterns, resolved once at plan time."""

    exclude: tuple[PatternMatch, ...]
    structure_only: tuple[PatternMatch, ...]

    def excluded(self) -> frozenset[TableName]:
        """Every excluded table."""
        return frozenset(name for match in self.exclude for name in match.tables)

    def structure_only_tables(self) -> frozenset[TableName]:
        """Every table kept without rows."""
        return frozenset(name for match in self.structure_only for name in match.tables)


def resolve_excludes(
    resolver: Resolver, exclude: tuple[str, ...], structure_only: tuple[str, ...]
) -> Excludes:
    """Match exclude and structure-only patterns against the catalog."""
    return Excludes(
        exclude=tuple(PatternMatch(pattern=p, tables=resolver.glob(p)) for p in exclude),
        structure_only=tuple(
            PatternMatch(pattern=p, tables=resolver.glob(p)) for p in structure_only
        ),
    )


class Difference(StrEnum):
    """Ways the live catalog can differ from the lock."""

    TABLE_REMOVED = "table-removed"
    TABLE_CHANGED = "table-changed"
    COLUMN_REMOVED = "column-removed"
    COLUMN_CHANGED = "column-changed"
    NEW_TABLE = "new-table"
    NEW_COLUMN = "new-column"
    REQUIREMENT_REMOVED = "requirement-removed"
    REQUIREMENT_CHANGED = "requirement-changed"
    NEW_REQUIREMENT = "new-requirement"
    OBJECT_REMOVED = "object-removed"
    OBJECT_CHANGED = "object-changed"
    NEW_OBJECT = "new-object"


class Severity(StrEnum):
    """What a difference does to the run."""

    FAIL = "fail"
    WARN = "warn"


RESOLUTIONS = (
    "exclude it",
    "add it to the subset model and re-lock with `datadouble lock`",
    "add anonymization rules for it, then re-lock with `datadouble lock`",
)
"""The ways to resolve a new table or column, in the order to consider them."""

REQUIREMENT_RESOLUTIONS = (
    "check that the working image provides it, then re-lock with `datadouble lock`",
    "leave it out with `source.omit`, then re-lock with `datadouble lock`",
)
"""The ways to resolve a new requirement."""

CHANGE_RESOLUTIONS = (
    (
        "check that the copies and the anonymization rules still suit it, "
        "then re-lock with `datadouble lock`"
    ),
)
"""The way to resolve a changed column or object."""

OBJECT_RESOLUTIONS = (
    (
        "check what it does to the copies (a trigger or function can change what they "
        "hold), then re-lock with `datadouble lock`"
    ),
)
"""The way to resolve a new, changed or removed object."""

SETTINGS: dict[ObjectKind, StrictKind] = {
    ObjectKind.FUNCTION: StrictKind.FUNCTIONS,
    ObjectKind.PROCEDURE: StrictKind.FUNCTIONS,
    ObjectKind.PACKAGE: StrictKind.FUNCTIONS,
    ObjectKind.VIEW: StrictKind.VIEWS,
    ObjectKind.MATERIALIZED_VIEW: StrictKind.VIEWS,
    ObjectKind.TRIGGER: StrictKind.TRIGGERS,
    ObjectKind.TYPE: StrictKind.TYPES,
    ObjectKind.SEQUENCE: StrictKind.SEQUENCES,
    ObjectKind.EVENT: StrictKind.EVENTS,
}
"""The ``strict`` setting each kind of object falls under."""


class Finding(_Model):
    """One difference between the live catalog and the lock."""

    difference: Difference
    severity: Severity
    object: str
    detail: str
    resolutions: tuple[str, ...] = ()
    breaks: str | None = None
    """What the difference breaks, when it does; such a difference always fails."""
    kind: StrictKind = StrictKind.DATA
    """The ``strict`` setting the difference falls under."""
    ref: ObjectRef | None = None
    """The object that differs, for differences in objects."""

    def __str__(self) -> str:
        """``new-column public.users.phone: detail``, and what it breaks, if anything."""
        text = f"{self.difference} {self.object}: {self.detail}"
        return text if self.breaks is None else f"{text}; {self.breaks}"


type Breaks = Callable[[Finding], str | None]
"""What a difference breaks, if anything: planning's judgement, given to strict mode."""


def _breaks_nothing(_finding: Finding) -> str | None:
    return None


class StrictReport(_Model):
    """Strict mode's verdict on a plan."""

    strict: Strict
    """The levels the catalog was compared at."""
    lock_fingerprint: str
    findings: tuple[Finding, ...]

    def failed(self) -> bool:
        """Whether any finding stops the run."""
        return any(finding.severity is Severity.FAIL for finding in self.findings)


def _severity_new_table(level: StrictLevel) -> Severity:
    return Severity.WARN if level is StrictLevel.OFF else Severity.FAIL


def _severity_new_column(level: StrictLevel) -> Severity:
    return Severity.FAIL if level is StrictLevel.COLUMNS else Severity.WARN


def _compare_table(live: Table, locked: Table, level: StrictLevel) -> list[Finding]:
    findings: list[Finding] = []
    live_columns = {column.name: column for column in live.columns}
    for column in locked.columns:
        found = live_columns.pop(column.name, None)
        name = f"{live.name}.{column.name}"
        if found is None:
            findings.append(
                Finding(
                    difference=Difference.COLUMN_REMOVED,
                    severity=Severity.FAIL,
                    object=name,
                    detail="the column is in the lock but not in the live catalog",
                )
            )
        elif found != column:
            findings.append(
                Finding(
                    difference=Difference.COLUMN_CHANGED,
                    severity=_severity_new_column(level),
                    object=name,
                    detail=(
                        f"locked as {column.type} ({'null' if column.nullable else 'not null'}),"
                        f" now {found.type} ({'null' if found.nullable else 'not null'})"
                    ),
                    resolutions=CHANGE_RESOLUTIONS,
                )
            )
    findings.extend(
        Finding(
            difference=Difference.NEW_COLUMN,
            severity=_severity_new_column(level),
            object=f"{live.name}.{name}",
            detail="the column is not in the lock and has no anonymization rule on record",
            resolutions=RESOLUTIONS,
        )
        for name in live_columns
    )
    if live.primary_key != locked.primary_key or set(live.foreign_keys) != set(locked.foreign_keys):
        findings.append(
            Finding(
                difference=Difference.TABLE_CHANGED,
                severity=Severity.FAIL,
                object=str(live.name),
                detail="its primary key or foreign keys differ from the lock; re-lock",
            )
        )
    return findings


def _compare_requirements(
    live: tuple[Requirement, ...], locked: tuple[Requirement, ...], level: StrictLevel
) -> list[Finding]:
    findings: list[Finding] = []
    current = {(r.kind, r.name): r for r in live}
    for requirement in locked:
        found = current.pop((requirement.kind, requirement.name), None)
        if found is None:
            findings.append(
                Finding(
                    difference=Difference.REQUIREMENT_REMOVED,
                    severity=Severity.FAIL,
                    object=str(requirement),
                    detail="the source no longer uses it; re-lock",
                )
            )
        elif found.version != requirement.version:
            findings.append(
                Finding(
                    difference=Difference.REQUIREMENT_CHANGED,
                    severity=Severity.FAIL,
                    object=str(found),
                    detail=f"locked at version {requirement.version}; re-lock",
                )
            )
    findings.extend(
        Finding(
            difference=Difference.NEW_REQUIREMENT,
            severity=_severity_new_table(level),
            object=str(requirement),
            detail="the source uses it, and the lock does not",
            resolutions=REQUIREMENT_RESOLUTIONS,
        )
        for requirement in current.values()
    )
    return findings


def _same_name(ref: ObjectRef) -> tuple[StrictKind, tuple[str, ...], str]:
    """What an inferred edge can tell: the kind of setting, namespace and name, no signature."""
    return (SETTINGS[ref.kind], ref.namespace, ref.name)


def _names(edge: Dependency, ref: ObjectRef, *, by_name: bool) -> bool:
    """Whether ``edge`` names ``ref``: exactly, or by kind of setting, namespace and name."""
    if edge.object is None:
        return False
    return _same_name(edge.object) == _same_name(ref) if by_name else edge.object == ref


class _Objects:
    """The objects of the lock and the live catalog, compared under each kind's level.

    Objects whose exact edges name only excluded tables are left out of copies,
    so they are ignored, as excluded tables are.
    """

    def __init__(self, live: Catalog, lock: Catalog, excluded: frozenset[TableName]) -> None:
        self.live = {o.ref: o for o in live.objects if not o.needs_only(excluded)}
        self.locked = {o.ref: o for o in lock.objects if not o.needs_only(excluded)}
        self.excluded = excluded
        self.names = {_same_name(ref) for ref in self.live}

    def findings(self, strict: Strict) -> list[tuple[Finding, bool]]:
        """Each difference, and whether its kind's level ignores it unless it breaks something."""
        found: list[tuple[Finding, bool]] = []
        for ref, locked in self.locked.items():
            current = self.live.get(ref)
            if current is None:
                found.append(self.removed(ref, strict))
            elif current.fingerprint != locked.fingerprint:
                found.append(
                    self.finding(
                        Difference.OBJECT_CHANGED,
                        ref,
                        strict,
                        "its definition differs from the lock",
                    )
                )
        found.extend(
            self.finding(Difference.NEW_OBJECT, ref, strict, "the object is not in the lock")
            for ref in self.live
            if ref not in self.locked
        )
        return found

    def removed(self, ref: ObjectRef, strict: Strict) -> tuple[Finding, bool]:
        """A removed object, naming what may still need it by an inferred edge.

        Inferred edges never fail the plan, but a removal they rest on warns
        whatever the level.
        """
        detail = "the object is in the lock but not in the live catalog"
        maybe = self.needing(ref, inferred=True)
        if maybe is None:
            return self.finding(Difference.OBJECT_REMOVED, ref, strict, detail)
        finding, _ = self.finding(Difference.OBJECT_REMOVED, ref, strict, f"{detail}; {maybe}")
        return finding, False

    def finding(
        self, difference: Difference, ref: ObjectRef, strict: Strict, detail: str
    ) -> tuple[Finding, bool]:
        kind = SETTINGS[ref.kind]
        level = strict.level(kind)
        finding = Finding(
            difference=difference,
            severity=Severity.FAIL if level is ObjectLevel.FAIL else Severity.WARN,
            object=str(ref),
            detail=detail,
            resolutions=OBJECT_RESOLUTIONS,
            kind=kind,
            ref=ref,
        )
        return finding, level is ObjectLevel.OFF

    def breaks(self, finding: Finding) -> str | None:
        """For a removed object, the remaining objects whose exact edges still name it."""
        if finding.difference is not Difference.OBJECT_REMOVED or finding.ref is None:
            return None
        return self.needing(finding.ref, inferred=False)

    def needing(self, ref: ObjectRef, *, inferred: bool) -> str | None:
        """The remaining objects that need ``ref`` by exact, or by inferred, edges.

        A remaining object's edges are its live ones, and its locked ones when
        its definition is unchanged. Objects that need an excluded table are
        left out of copies, so they need nothing. An inferred edge knows a name,
        not a signature, so it needs ``ref`` only when nothing of that name is
        left.
        """
        if inferred and _same_name(ref) in self.names:
            return None
        how = "may still need it (an inferred dependency)" if inferred else "still needs it"
        needing = [
            f"{item.ref} {how}"
            for item in self.live.values()
            if not item.needs_table(self.excluded)
            for edge in self.edges(item)
            if edge.inferred == inferred and _names(edge, ref, by_name=inferred)
        ]
        return "; ".join(dict.fromkeys(needing)) or None

    def edges(self, item: SourceObject) -> tuple[Dependency, ...]:
        locked = self.locked.get(item.ref)
        if locked is None or locked.fingerprint != item.fingerprint:
            return item.depends_on
        return (*item.depends_on, *locked.depends_on)


def _judged(finding: Finding, breaks: Breaks) -> Finding:
    """``finding``, failing with the reason when it breaks something."""
    if finding.severity is Severity.FAIL and finding.difference in _ALWAYS_FAIL:
        return finding
    reason = breaks(finding)
    if reason is None:
        return finding
    return finding.model_copy(update={"severity": Severity.FAIL, "breaks": reason})


_ALWAYS_FAIL = frozenset(
    {
        Difference.TABLE_REMOVED,
        Difference.TABLE_CHANGED,
        Difference.COLUMN_REMOVED,
        Difference.REQUIREMENT_REMOVED,
        Difference.REQUIREMENT_CHANGED,
    }
)
"""Differences that fail at every level, whatever they break."""


def check_strict(
    live: Catalog,
    locked: CatalogLock,
    excluded: frozenset[TableName],
    strict: Strict,
    breaks: Breaks = _breaks_nothing,
) -> StrictReport:
    """Compare the live catalog, minus excluded tables, with the lock.

    ``breaks`` says what a difference breaks, if anything; one that breaks
    something fails whatever the levels.
    """
    level = strict.data
    lock = locked.catalog
    findings: list[Finding] = []
    for locked_table in lock.tables:
        if locked_table.name in excluded:
            continue
        current = live.table(locked_table.name)
        if current is None:
            findings.append(
                Finding(
                    difference=Difference.TABLE_REMOVED,
                    severity=Severity.FAIL,
                    object=str(locked_table.name),
                    detail="the table is in the lock but not in the live catalog",
                )
            )
        else:
            findings.extend(_compare_table(current, locked_table, level))
    locked_names = set(lock.names())
    findings.extend(
        Finding(
            difference=Difference.NEW_TABLE,
            severity=_severity_new_table(level),
            object=str(table.name),
            detail="the table is not in the lock and matches no exclude pattern",
            resolutions=RESOLUTIONS,
        )
        for table in live.tables
        if table.name not in locked_names and table.name not in excluded
    )
    findings.extend(_compare_requirements(live.requirements, lock.requirements, level))
    judged = [_judged(finding, breaks) for finding in findings]
    objects = _Objects(live, lock, excluded)

    def object_breaks(finding: Finding) -> str | None:
        return objects.breaks(finding) or breaks(finding)

    for finding, ignored in objects.findings(strict):
        result = _judged(finding, object_breaks)
        if not ignored or result.breaks is not None:
            judged.append(result)
    return StrictReport(strict=strict, lock_fingerprint=locked.fingerprint, findings=tuple(judged))
