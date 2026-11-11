# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Excludes and strict mode.

Excludes are resolved first, against the live catalog. Strict mode then
compares what is left with the catalog lock:

=========================================  =======  ========  ===========
Difference from the lock                   off      tables    columns
=========================================  =======  ========  ===========
Known table or column removed or changed   fail     fail      fail
New table matching an exclude pattern      ignored  ignored   ignored
New table, not excluded                    warn     fail      fail
New column in a known table                warn     warn      fail
=========================================  =======  ========  ===========
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from datadouble.catalog import Catalog, Table
from datadouble.names import TableName
from datadouble.resolve import Resolver
from datadouble.spec import StrictLevel


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


class Finding(_Model):
    """One difference between the live catalog and the lock."""

    difference: Difference
    severity: Severity
    object: str
    detail: str
    resolutions: tuple[str, ...] = ()


class StrictReport(_Model):
    """Strict mode's verdict on a plan."""

    level: StrictLevel
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
                    severity=Severity.FAIL,
                    object=name,
                    detail=(
                        f"locked as {column.type} ({'null' if column.nullable else 'not null'}),"
                        f" now {found.type} ({'null' if found.nullable else 'not null'})"
                    ),
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


def check_strict(
    live: Catalog,
    lock: Catalog,
    lock_fingerprint: str,
    excluded: frozenset[TableName],
    level: StrictLevel,
) -> StrictReport:
    """Compare the live catalog, minus excluded tables, with the lock."""
    findings: list[Finding] = []
    for locked in lock.tables:
        if locked.name in excluded:
            continue
        current = live.table(locked.name)
        if current is None:
            findings.append(
                Finding(
                    difference=Difference.TABLE_REMOVED,
                    severity=Severity.FAIL,
                    object=str(locked.name),
                    detail="the table is in the lock but not in the live catalog",
                )
            )
        else:
            findings.extend(_compare_table(current, locked, level))
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
    return StrictReport(level=level, lock_fingerprint=lock_fingerprint, findings=tuple(findings))
