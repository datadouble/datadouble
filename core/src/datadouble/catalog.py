# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Catalogs and catalog locks.

An engine adapter introspects a source into a :class:`Catalog`: its tables,
columns, primary keys and foreign keys, and the add-ons the source depends on
(its :class:`Requirement` list). ``datadouble lock`` writes the catalog
to ``catalog.lock`` next to the spec, and strict mode compares the live catalog
against it at plan time.
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
    tables = sorted(catalog.tables, key=lambda table: (table.name.namespace, table.name.name))
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
