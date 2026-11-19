# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The source side: the org's catalog, key reads and the extract.

Each object is a table in one namespace, its fields the columns and ``Id``
its key. The catalog lists the objects that can be queried, read, created
and replicated, leaving out history, feed, share and change-event objects,
custom metadata types, platform events, and external and big objects; the
connection's ``objects`` patterns narrow it further. Compound fields
(addresses and locations, whose parts are fields too) and base64 fields,
which the Bulk API cannot export, are left out. A reference field that can
point at one object in the catalog is a foreign key to its ``Id``;
polymorphic references (``WhoId``, ``WhatId``, often ``OwnerId``) are not,
and are declared as virtual foreign keys where a subset must follow them.

An org has no snapshots: each read sees the records as they are then, so a
record changed during the extract may be seen before or after the change.
The extract checks that the objects it reads still have the fields the plan
saw, and exports each through the Bulk API as JSON lines.
"""

import datetime as dt
import fnmatch
import json
import time
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict, JsonValue

from datadouble.catalog import (
    Catalog,
    Column,
    ColumnKind,
    ForeignKey,
    Requirement,
    RequirementKind,
    Table,
)
from datadouble.engine import EngineError, ExtractResult, Snapshot, TableCount, Tooling
from datadouble.names import ColumnName, EngineName, TableName
from datadouble.planner import Plan
from datadouble.subset import Condition, KeyTuple, NoRows
from datadouble_salesforce.org import ObjectDescription, ObjectSummary, Org
from datadouble_salesforce.soql import BULK_LIMIT, REST_LIMIT, wheres

ENGINE = EngineName("salesforce")
ID = ColumnName("Id")
RECORDS = "records"
"""The extract's directory of JSON lines, one file per object."""
_SKIPPED_SUFFIXES = ("History", "Feed", "Share", "ChangeEvent", "__mdt", "__e", "__x", "__b")
_SKIPPED_TYPES = {"address", "location", "base64"}
_KINDS = {
    "boolean": ColumnKind.BOOLEAN,
    "int": ColumnKind.INTEGER,
    "long": ColumnKind.INTEGER,
    "double": ColumnKind.FLOAT,
    "percent": ColumnKind.FLOAT,
    "currency": ColumnKind.DECIMAL,
    "date": ColumnKind.DATE,
    "datetime": ColumnKind.TIMESTAMP,
    "time": ColumnKind.TIME,
}
# Record IDs are kind other: not text the PII scan reads.
_TEXT = {
    "string",
    "textarea",
    "email",
    "phone",
    "url",
    "picklist",
    "multipicklist",
    "combobox",
    "encryptedstring",
}
_PACKAGES = (
    "SELECT SubscriberPackage.NamespacePrefix, SubscriberPackage.Name,"
    " SubscriberPackageVersion.MajorVersion, SubscriberPackageVersion.MinorVersion,"
    " SubscriberPackageVersion.PatchVersion FROM InstalledSubscriberPackage"
)


def kind(field_type: str) -> ColumnKind:
    """The column kind of a Salesforce field type."""
    if field_type in _TEXT:
        return ColumnKind.TEXT
    return _KINDS.get(field_type, ColumnKind.OTHER)


def included(summary: ObjectSummary, patterns: tuple[str, ...]) -> bool:
    """Whether an object belongs in the catalog."""
    if not (
        summary.queryable and summary.retrieveable and summary.createable and summary.replicateable
    ):
        return False
    if summary.name.endswith(_SKIPPED_SUFFIXES):
        return False
    return not patterns or any(fnmatch.fnmatchcase(summary.name, p) for p in patterns)


def _table(description: ObjectDescription, names: frozenset[str]) -> Table:
    columns: list[Column] = []
    keys: list[ForeignKey] = []
    for field in description.fields:
        if field.type in _SKIPPED_TYPES:
            continue
        columns.append(
            Column(
                name=ColumnName(field.name),
                type=field.type,
                kind=kind(field.type),
                nullable=field.nillable and field.name != ID,
            )
        )
        targets = field.reference_to
        if field.type == "reference" and len(targets) == 1 and targets[0] in names:
            keys.append(
                ForeignKey(
                    name=f"{description.name}.{field.name}",
                    columns=(ColumnName(field.name),),
                    target=TableName.of("", targets[0]),
                    target_columns=(ID,),
                )
            )
    return Table(
        name=TableName.of("", description.name),
        columns=tuple(columns),
        primary_key=(ID,),
        foreign_keys=tuple(keys),
    )


def _version(record: Mapping[str, JsonValue]) -> str:
    version = record.get("SubscriberPackageVersion")
    parts = version if isinstance(version, dict) else {}
    return ".".join(
        str(parts.get(part, 0)) for part in ("MajorVersion", "MinorVersion", "PatchVersion")
    )


def packages(org: Org) -> tuple[Requirement, ...]:
    """The org's installed packages, by namespace (or name, for those without one)."""
    found: list[Requirement] = []
    for record in org.tooling_query(_PACKAGES):
        package = record.get("SubscriberPackage")
        details = package if isinstance(package, dict) else {}
        name = details.get("NamespacePrefix") or details.get("Name")
        if isinstance(name, str) and name:
            found.append(
                Requirement(kind=RequirementKind.PACKAGE, name=name, version=_version(record))
            )
    return tuple(found)


def introspect(org: Org, patterns: tuple[str, ...]) -> Catalog:
    """The catalog: the included objects, their fields and references, and the packages."""
    names = frozenset(s.name for s in org.objects() if included(s, patterns))
    tables = [_table(org.describe(name), names) for name in sorted(names)]
    return Catalog(engine=ENGINE, tables=tuple(tables), requirements=packages(org))


class StoredColumn(BaseModel):
    """A column in an extract or image: its name, Salesforce type and kind."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    name: ColumnName
    type: str
    kind: ColumnKind


class ObjectRecords(BaseModel):
    """One object in an extract or image."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    table: TableName
    columns: tuple[StoredColumn, ...]
    records: int


class Objects(BaseModel):
    """What an extract or image holds: ``objects.json``."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    api_version: str
    objects: tuple[ObjectRecords, ...]


def typed(value: str, column: StoredColumn) -> JsonValue:
    """An exported text value as JSON, by the column's kind; empty text is null."""
    if value == "":
        return None
    try:
        match column.kind:
            case ColumnKind.BOOLEAN:
                return {"true": True, "false": False}[value.lower()]
            case ColumnKind.INTEGER:
                return int(value)
            case ColumnKind.FLOAT:
                return float(value)
            case _:
                return value
    except (KeyError, ValueError):
        message = f"an exported value of {column.name} is not a {column.type}"
        raise EngineError(message) from None


def _key(value: JsonValue) -> KeyTuple:
    return (value if isinstance(value, str | int | float | bool) else None,)


class SalesforceSnapshot:
    """Reads of the org; without snapshots, each sees the records as they are then."""

    def __init__(self, source: "SalesforceSource", as_of: dt.datetime) -> None:
        """Read through ``source``."""
        self.source = source
        self.as_of = as_of
        self.opened = time.monotonic()

    def project(
        self, table: TableName, condition: Condition, columns: tuple[ColumnName, ...]
    ) -> Iterable[KeyTuple]:
        """Distinct values of ``columns`` for the chosen records."""
        if isinstance(condition, NoRows):
            return set()
        found: set[KeyTuple] = set()
        listed = ", ".join(columns)
        for clause in wheres(condition, self.as_of, REST_LIMIT):
            soql = f"SELECT {listed} FROM {table.name}" + (f" WHERE {clause}" if clause else "")
            for record in self.source.org.query(soql):
                values = {key.lower(): value for key, value in record.items()}
                found.add(
                    tuple(part for column in columns for part in _key(values.get(column.lower())))
                )
        return found

    def extract(
        self,
        plan: Plan,
        selections: Mapping[TableName, Condition],
        directory: Path,
        tooling: Tooling,  # noqa: ARG002 - extract reads through the API, without tools
    ) -> ExtractResult:
        """Export the chosen records of each object as JSON lines."""
        org = self.source.org
        (directory / RECORDS).mkdir(parents=True, exist_ok=True)
        written: list[ObjectRecords] = []
        for table in plan.extracted():
            planned = plan.catalog.table(table)
            current = _table(org.describe(table.name), frozenset())
            if planned is None or current.columns != planned.columns:
                message = (
                    f"{table.name}'s fields changed between planning and the extract;"
                    " plan again (start a new generation)"
                )
                raise EngineError(message)
            columns = tuple(
                StoredColumn(name=c.name, type=c.type, kind=c.kind) for c in planned.columns
            )
            count = self._export(table, columns, selections[table], directory)
            written.append(ObjectRecords(table=table, columns=columns, records=count))
        objects = Objects(api_version=self.source.api_version, objects=tuple(written))
        (directory / "objects.json").write_text(objects.model_dump_json(indent=2), "utf-8")
        return ExtractResult(
            server_version=self.source.api_version,
            snapshot="",
            snapshot_seconds=round(time.monotonic() - self.opened, 3),
            counts=tuple(TableCount(table=o.table, rows=o.records) for o in written),
        )

    def _export(
        self,
        table: TableName,
        columns: tuple[StoredColumn, ...],
        condition: Condition,
        directory: Path,
    ) -> int:
        listed = ", ".join(c.name for c in columns)
        clauses = [] if isinstance(condition, NoRows) else wheres(condition, self.as_of, BULK_LIMIT)
        # Split conditions can choose a record more than once.
        seen: set[str] = set()
        count = 0
        with (directory / RECORDS / f"{table.name}.jsonl").open("w", encoding="utf-8") as out:
            for clause in clauses:
                soql = f"SELECT {listed} FROM {table.name}" + (f" WHERE {clause}" if clause else "")
                for row in self.source.org.export(soql):
                    record = {c.name: typed(row.get(c.name, ""), c) for c in columns}
                    if len(clauses) > 1:
                        if str(record[ID]) in seen:
                            continue
                        seen.add(str(record[ID]))
                    out.write(json.dumps(record, separators=(",", ":")) + "\n")
                    count += 1
        return count


class SalesforceSource:
    """An open connection to an org."""

    def __init__(self, org: Org, api_version: str, patterns: tuple[str, ...]) -> None:
        """Read ``org`` through API ``api_version``; ``patterns`` choose the objects."""
        self.org = org
        self.api_version = api_version
        self.patterns = patterns

    def server_version(self) -> str:
        """The API version the adapter uses, once the org is known to offer it."""
        offered = self.org.versions()
        if self.api_version not in offered:
            latest = max(offered, key=lambda v: tuple(map(int, v.split("."))), default="none")
            message = (
                f"the org does not offer API version {self.api_version}; its latest is {latest}"
            )
            raise EngineError(message)
        return self.api_version

    def catalog(self) -> Catalog:
        """Introspect the org."""
        return introspect(self.org, self.patterns)

    @contextmanager
    def snapshot(self, as_of: dt.datetime) -> Generator[Snapshot]:
        """Reads of the org; it has no snapshots, so each sees the records as they are then."""
        yield SalesforceSnapshot(self, as_of)
