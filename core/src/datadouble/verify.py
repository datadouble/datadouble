# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Verification of a packaged golden image, restored into scratch space.

Every format of every image is restored and checked: row counts against the
working database, references between the image's tables, a PII scan of text
columns, and the spec's assertions. Reports carry counts, never values, so a
report never repeats the personal data it found.
"""

from collections import Counter
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from datadouble.catalog import ColumnKind
from datadouble.engine import RestoredImage, TableCount
from datadouble.names import ColumnRef
from datadouble.pii import allowed, detect, like_pattern
from datadouble.planner import Plan, PlannedGroup
from datadouble.spec import ImageFormat


class CheckKind(StrEnum):
    """The kinds of verification check."""

    RESTORE = "restore"
    ROW_COUNTS = "row-counts"
    REFERENCES = "references"
    PII_SCAN = "pii-scan"
    ASSERTION = "assertion"


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class Check(_Model):
    """One check's outcome."""

    kind: CheckKind
    name: str
    passed: bool
    detail: str = ""


class FormatReport(_Model):
    """The checks run against one restored format."""

    format: ImageFormat
    checks: tuple[Check, ...]

    def passed(self) -> bool:
        """Whether every check passed."""
        return all(check.passed for check in self.checks)


class VerificationReport(_Model):
    """Verification of one image, all formats."""

    group: str
    formats: tuple[FormatReport, ...]

    def passed(self) -> bool:
        """Whether every format passed every check; an empty report never passes."""
        return bool(self.formats) and all(report.passed() for report in self.formats)

    def checks(self) -> tuple[Check, ...]:
        """Every check, across formats."""
        return tuple(check for report in self.formats for check in report.checks)


def restore_failed(image_format: ImageFormat, reason: str) -> FormatReport:
    """The report for a format that could not be restored at all."""
    return FormatReport(
        format=image_format,
        checks=(Check(kind=CheckKind.RESTORE, name="restore", passed=False, detail=reason),),
    )


def check_image(
    plan: Plan,
    group: PlannedGroup,
    image_format: ImageFormat,
    restored: RestoredImage,
    expected: tuple[TableCount, ...],
) -> FormatReport:
    """Run every check against a restored format."""
    checks = [Check(kind=CheckKind.RESTORE, name="restore", passed=True)]
    checks.append(_row_counts(restored, expected))
    checks.extend(_references(plan, group, restored))
    checks.extend(_pii_scan(plan, group, restored))
    checks.extend(
        Check(
            kind=CheckKind.ASSERTION,
            name=assertion.name,
            passed=(failing := restored.failing_rows(assertion.sql)) == 0,
            detail="" if failing == 0 else f"{failing} row(s) returned",
        )
        for assertion in plan.assertions
    )
    return FormatReport(format=image_format, checks=tuple(checks))


def _row_counts(restored: RestoredImage, expected: tuple[TableCount, ...]) -> Check:
    actual = restored.counts(count.table for count in expected)
    wrong = [
        f"{count.table}: {actual.get(count.table, 0)} rows, expected {count.rows}"
        for count in expected
        if actual.get(count.table, 0) != count.rows
    ]
    return Check(
        kind=CheckKind.ROW_COUNTS,
        name="row counts",
        passed=not wrong,
        detail="; ".join(wrong) or f"{len(expected)} tables match",
    )


def _references(plan: Plan, group: PlannedGroup, restored: RestoredImage) -> list[Check]:
    members = set(group.tables)
    checks: list[Check] = []
    for edge in plan.edges:
        if edge.child not in members or edge.parent not in members:
            continue
        orphans = restored.orphans(edge)
        checks.append(
            Check(
                kind=CheckKind.REFERENCES,
                name=edge.name,
                passed=orphans == 0,
                detail="" if orphans == 0 else f"{orphans} row(s) of {edge.child} have no parent",
            )
        )
    return checks


def _pii_scan(plan: Plan, group: PlannedGroup, restored: RestoredImage) -> list[Check]:
    allow = {rule.column: [like_pattern(p) for p in rule.patterns] for rule in plan.pii_allow}
    checks: list[Check] = []
    members = set(group.tables)
    for table in plan.catalog.tables:
        if table.name not in members:
            continue
        for column in table.columns:
            if column.kind is not ColumnKind.TEXT:
                continue
            ref = ColumnRef(table=table.name, column=column.name)
            patterns = allow.get(ref, [])
            found: Counter[str] = Counter()
            for value in restored.text_values(ref):
                if not allowed(value, patterns):
                    found.update(detect(value))
            if found:
                summary = ", ".join(f"{n} value(s) look like {d}" for d, n in sorted(found.items()))
                checks.append(
                    Check(kind=CheckKind.PII_SCAN, name=str(ref), passed=False, detail=summary)
                )
    checks.append(
        Check(
            kind=CheckKind.PII_SCAN,
            name="pii scan",
            passed=not checks,
            detail=f"{len(checks)} column(s) with possible personal data" if checks else "clean",
        )
    )
    return checks
