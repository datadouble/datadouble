# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Catalogs and catalog locks.

An engine adapter introspects a source into a :class:`Catalog`: its tables,
columns, primary keys and foreign keys, and the add-ons the source depends on
(its :class:`Requirement` list). ``datadouble lock`` writes the catalog
to ``catalog.lock`` next to the spec, listing what changed since the lock it
replaces (:func:`changes`), and strict mode compares the live catalog against
it at plan time.

A lock holds names, engine type names, keys and requirement versions, never
data values, so it may be published where data may not (decision 123). An
engine's type name can carry schema literals, such as a MySQL ``enum`` or
``set`` column's labels; they are part of the schema, already in the
committed lock, so publishing the lock exposes nothing more.
"""

import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from datadouble.names import ColumnName, EngineName, TableName


class CatalogError(Exception):
    """A catalog or lock that cannot be used."""


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class ColumnKind(StrEnum):
    """The engine-neutral family of a column's type.

    Anonymization strategies and the PII scan work by kind; the engine's own
    type name is kept alongside it.
    """

    TEXT = "text"
    INTEGER = "integer"
    DECIMAL = "decimal"
    FLOAT = "float"
    BOOLEAN = "boolean"
    DATE = "date"
    TIMESTAMP = "timestamp"
    TIME = "time"
    BINARY = "binary"
    JSON = "json"
    UUID = "uuid"
    ARRAY = "array"
    OTHER = "other"


class Column(_Model):
    """A column, with its type as the engine spells it and its kind."""

    name: ColumnName
    type: str
    kind: ColumnKind
    nullable: bool


class ForeignKey(_Model):
    """A declared foreign key from some of a table's columns to another table."""

    name: str
    columns: tuple[ColumnName, ...]
    target: TableName
    target_columns: tuple[ColumnName, ...]


class Table(_Model):
    """A table and the keys the subset planner follows."""

    name: TableName
    columns: tuple[Column, ...]
    primary_key: tuple[ColumnName, ...] = ()
    foreign_keys: tuple[ForeignKey, ...] = ()

    def column(self, name: ColumnName) -> Column | None:
        """The column called ``name``, if there is one."""
        return next((column for column in self.columns if column.name == name), None)


class RequirementKind(StrEnum):
    """The engine-neutral family of something a source needs beyond the server itself."""

    EXTENSION = "extension"
    PLUGIN = "plugin"
    MODULE = "module"
    PACKAGE = "package"
    OPTION = "option"


class Requirement(_Model):
    """An add-on the source uses, which the working image must provide.

    ``version`` is the version the source runs, when the engine has one; the
    working image must provide exactly that version.
    """

    kind: RequirementKind
    name: str
    version: str | None = None

    def __str__(self) -> str:
        """``extension vector 0.8.2``."""
        return " ".join(part for part in (self.kind.value, self.name, self.version) if part)


class Catalog(_Model):
    """Everything strict mode and the planner need to know about a source."""

    engine: EngineName
    tables: tuple[Table, ...]
    requirements: tuple[Requirement, ...] = ()
    """What the source needs beyond the server: extensions, plugins, modules."""

    def requirement(self, name: str) -> Requirement | None:
        """The requirement called ``name``, if there is one."""
        return next((r for r in self.requirements if r.name == name), None)

    def table(self, name: TableName) -> Table | None:
        """The table called ``name``, if there is one."""
        return next((table for table in self.tables if table.name == name), None)

    def names(self) -> tuple[TableName, ...]:
        """Every table name, in catalog order."""
        return tuple(table.name for table in self.tables)

    def without(self, names: frozenset[TableName]) -> "Catalog":
        """The catalog minus the tables in ``names``."""
        return self.model_copy(
            update={"tables": tuple(table for table in self.tables if table.name not in names)}
        )

    def fingerprint(self) -> str:
        """A SHA-256 over the catalog's canonical JSON form."""
        return "sha256:" + hashlib.sha256(_canonical(self).encode()).hexdigest()


def normalize(catalog: Catalog) -> Catalog:
    """Sort tables, foreign keys and requirements, so equal catalogs compare equal.

    Column order is kept: it is part of a table's definition.
    """
    tables = sorted(catalog.tables, key=lambda table: table.name.parts)
    return Catalog(
        engine=catalog.engine,
        tables=tuple(
            table.model_copy(
                update={"foreign_keys": tuple(sorted(table.foreign_keys, key=lambda fk: fk.name))}
            )
            for table in tables
        ),
        requirements=tuple(sorted(catalog.requirements, key=lambda r: (r.kind, r.name))),
    )


def _canonical(catalog: Catalog) -> str:
    form = normalize(catalog).model_dump(mode="json")
    # A catalog without requirements hashes as it did before they existed, so
    # locks written then stay valid.
    if not catalog.requirements:
        del form["requirements"]
    return json.dumps(form, sort_keys=True, separators=(",", ":"))


class CatalogLock(_Model):
    """The catalog as it was when the subset model was built."""

    format: Literal[1] = 1
    fingerprint: str
    catalog: Catalog

    @classmethod
    def of(cls, catalog: Catalog) -> "CatalogLock":
        """Lock ``catalog``."""
        normal = normalize(catalog)
        return cls(fingerprint=normal.fingerprint(), catalog=normal)


def write_lock(lock: CatalogLock, path: Path) -> None:
    """Write a lock file: indented JSON with sorted keys, so diffs are readable."""
    form = lock.model_dump(mode="json")
    if not lock.catalog.requirements:
        del form["catalog"]["requirements"]
    text = json.dumps(form, indent=2, sort_keys=True)
    try:
        path.write_text(text + "\n", encoding="utf-8")
    except OSError as error:
        message = f"cannot write the catalog lock {path}: {error.strerror}"
        raise CatalogError(message) from None


def read_lock(path: Path) -> CatalogLock:
    """Read and check a lock file."""
    try:
        lock = CatalogLock.model_validate_json(path.read_bytes())
    except FileNotFoundError:
        message = f"{path} does not exist; write one with `datadouble lock`"
        raise CatalogError(message) from None
    except ValidationError as error:
        message = f"{path} is not a valid catalog lock: {error.error_count()} problem(s)"
        raise CatalogError(message) from error
    if lock.catalog.fingerprint() != lock.fingerprint:
        message = f"{path} has been edited by hand: its fingerprint does not match its contents"
        raise CatalogError(message)
    return lock


class Change(StrEnum):
    """How an object differs between two locks."""

    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"


class ObjectKind(StrEnum):
    """The kinds of object a lock records."""

    TABLE = "table"
    COLUMN = "column"
    PRIMARY_KEY = "primary-key"
    FOREIGN_KEY = "foreign-key"
    REQUIREMENT = "requirement"
    CATALOG = "catalog"
    """The catalog as a whole: its engine, or a difference nothing else names."""


class LockChange(_Model):
    """One difference between an earlier lock and a new one: names and types, never values."""

    change: Change
    kind: ObjectKind
    object: str
    """``public.customer``, ``public.customer.email``, ``public.orders fk_customer``..."""
    detail: str = ""
    """The type, key or version, and what it was for a change."""

    def __str__(self) -> str:
        """``added column public.customer.phone: text null``."""
        text = f"{self.change} {self.kind} {self.object}"
        return f"{text}: {self.detail}" if self.detail else text


def _column(column: Column) -> str:
    return f"{column.type} ({column.kind}) {'null' if column.nullable else 'not null'}"


def _key(columns: tuple[ColumnName, ...]) -> str:
    return f"({', '.join(columns)})"


def _reference(key: ForeignKey) -> str:
    return f"{_key(key.columns)} -> {key.target}{_key(key.target_columns)}"


def _was(old: str, new: str) -> str:
    return f"{new}, was {old}"


def _added_table(table: Table) -> list[LockChange]:
    """A new table, each of its columns and its keys: every new column is someone's to check."""
    found = [LockChange(change=Change.ADDED, kind=ObjectKind.TABLE, object=str(table.name))]
    found.extend(
        LockChange(
            change=Change.ADDED,
            kind=ObjectKind.COLUMN,
            object=f"{table.name}.{column.name}",
            detail=_column(column),
        )
        for column in table.columns
    )
    if table.primary_key:
        found.append(
            LockChange(
                change=Change.ADDED,
                kind=ObjectKind.PRIMARY_KEY,
                object=str(table.name),
                detail=_key(table.primary_key),
            )
        )
    found.extend(
        LockChange(
            change=Change.ADDED,
            kind=ObjectKind.FOREIGN_KEY,
            object=f"{table.name} {key.name}",
            detail=_reference(key),
        )
        for key in sorted(table.foreign_keys, key=lambda key: key.name)
    )
    return found


def _order(old: Table, new: Table) -> list[LockChange]:
    """The order of the columns both locks have, which is part of a table's definition."""
    kept = {column.name for column in old.columns} & {column.name for column in new.columns}
    before = tuple(column.name for column in old.columns if column.name in kept)
    after = tuple(column.name for column in new.columns if column.name in kept)
    if before == after:
        return []
    return [
        LockChange(
            change=Change.CHANGED,
            kind=ObjectKind.TABLE,
            object=str(new.name),
            detail=f"column order {_was(_key(before), _key(after))}",
        )
    ]


def _columns(old: Table, new: Table) -> list[LockChange]:
    found: list[LockChange] = []
    before = {column.name: column for column in old.columns}
    after = {column.name: column for column in new.columns}
    for column in old.columns:
        name = f"{new.name}.{column.name}"
        current = after.get(column.name)
        if current is None:
            found.append(LockChange(change=Change.REMOVED, kind=ObjectKind.COLUMN, object=name))
        elif current != column:
            detail = _was(_column(column), _column(current))
            found.append(
                LockChange(
                    change=Change.CHANGED, kind=ObjectKind.COLUMN, object=name, detail=detail
                )
            )
    found.extend(
        LockChange(
            change=Change.ADDED,
            kind=ObjectKind.COLUMN,
            object=f"{new.name}.{column.name}",
            detail=_column(column),
        )
        for column in new.columns
        if column.name not in before
    )
    return found


def _keys(old: Table, new: Table) -> list[LockChange]:
    found: list[LockChange] = []
    if old.primary_key != new.primary_key:
        found.append(
            LockChange(
                change=Change.CHANGED,
                kind=ObjectKind.PRIMARY_KEY,
                object=str(new.name),
                detail=_was(_key(old.primary_key), _key(new.primary_key)),
            )
        )
    before = {key.name: key for key in old.foreign_keys}
    after = {key.name: key for key in new.foreign_keys}
    for name in sorted(before.keys() | after.keys()):
        old_key, new_key = before.get(name), after.get(name)
        what = f"{new.name} {name}"
        if new_key is None:
            found.append(
                LockChange(change=Change.REMOVED, kind=ObjectKind.FOREIGN_KEY, object=what)
            )
        elif old_key is None:
            found.append(
                LockChange(
                    change=Change.ADDED,
                    kind=ObjectKind.FOREIGN_KEY,
                    object=what,
                    detail=_reference(new_key),
                )
            )
        elif old_key != new_key:
            found.append(
                LockChange(
                    change=Change.CHANGED,
                    kind=ObjectKind.FOREIGN_KEY,
                    object=what,
                    detail=_was(_reference(old_key), _reference(new_key)),
                )
            )
    return found


def _requirements(old: Catalog, new: Catalog) -> list[LockChange]:
    found: list[LockChange] = []
    before = {(r.kind, r.name): r for r in old.requirements}
    after = {(r.kind, r.name): r for r in new.requirements}
    for key in sorted(before.keys() | after.keys()):
        earlier, current = before.get(key), after.get(key)
        what = f"{key[0]} {key[1]}"
        if current is None:
            found.append(
                LockChange(change=Change.REMOVED, kind=ObjectKind.REQUIREMENT, object=what)
            )
        elif earlier is None:
            found.append(
                LockChange(
                    change=Change.ADDED,
                    kind=ObjectKind.REQUIREMENT,
                    object=what,
                    detail=current.version or "",
                )
            )
        elif earlier.version != current.version:
            found.append(
                LockChange(
                    change=Change.CHANGED,
                    kind=ObjectKind.REQUIREMENT,
                    object=what,
                    detail=_was(earlier.version or "no version", current.version or "no version"),
                )
            )
    return found


def changes(old: Catalog, new: Catalog) -> tuple[LockChange, ...]:
    """What ``new`` adds, removes and changes against ``old``, table by table."""
    found: list[LockChange] = []
    old_tables = {table.name: table for table in old.tables}
    new_tables = {table.name: table for table in new.tables}
    for name in sorted(old_tables.keys() | new_tables.keys()):
        before, after = old_tables.get(name), new_tables.get(name)
        if after is None:
            found.append(LockChange(change=Change.REMOVED, kind=ObjectKind.TABLE, object=str(name)))
        elif before is None:
            found.extend(_added_table(after))
        else:
            found.extend(_columns(before, after))
            found.extend(_order(before, after))
            found.extend(_keys(before, after))
    found.extend(_requirements(old, new))
    if old.engine != new.engine:
        found.append(
            LockChange(
                change=Change.CHANGED,
                kind=ObjectKind.CATALOG,
                object="engine",
                detail=_was(old.engine, new.engine),
            )
        )
    if not found and old.fingerprint() != new.fingerprint():
        found.append(
            LockChange(
                change=Change.CHANGED,
                kind=ObjectKind.CATALOG,
                object="fingerprint",
                detail=(
                    "the locks differ, though in no table, column, key or requirement;"
                    " compare the two files"
                ),
            )
        )
    return tuple(found)


def describe_changes(
    changes: tuple[LockChange, ...] | None, unreadable: str | None = None
) -> tuple[str, ...]:
    """The changes as lines for people: the CLI summary and the lock job's ``changes.txt``.

    ``unreadable`` is why an earlier lock could not be read, when that is why
    there are no changes to list.
    """
    if unreadable is not None:
        problem = (
            f"the earlier lock could not be read ({unreadable}): check every table and column"
            " in the new one for personal data"
        )
        return (problem,)
    if changes is None:
        return (
            "no earlier lock to compare with: check every table and column in it for personal data",
        )
    if not changes:
        return ("no changes from the earlier lock",)
    heading = (
        f"{len(changes)} change(s) from the earlier lock; check new and changed columns"
        " for personal data:"
    )
    return (heading, *(f"  {change}" for change in changes))
