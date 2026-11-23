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
"""

from collections.abc import Callable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from datadouble.catalog import Catalog, CatalogLock, Requirement, Table
from datadouble.names import TableName
from datadouble.resolve import Resolver
from datadouble.spec import Strict, StrictLevel


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


class Finding(_Model):
    """One difference between the live catalog and the lock."""

    difference: Difference
    severity: Severity
    object: str
    detail: str
    resolutions: tuple[str, ...] = ()
    breaks: str | None = None
    """What the difference breaks, when it does; such a difference always fails."""

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
    return StrictReport(
        strict=strict,
        lock_fingerprint=locked.fingerprint,
        findings=tuple(_judged(finding, breaks) for finding in findings),
    )
