# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Catalogs and catalog locks.

An engine adapter introspects a source into a :class:`Catalog`: its tables,
columns, primary keys and foreign keys. ``datadouble lock`` writes the catalog
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


class Catalog(_Model):
    """Everything strict mode and the planner need to know about a source."""

    engine: EngineName
    tables: tuple[Table, ...]

    def table(self, name: TableName) -> Table | None:
        """The table called ``name``, if there is one."""
        return next((table for table in self.tables if table.name == name), None)

    def names(self) -> tuple[TableName, ...]:
        """Every table name, in catalog order."""
        return tuple(table.name for table in self.tables)

    def without(self, names: frozenset[TableName]) -> "Catalog":
        """The catalog minus the tables in ``names``."""
        return Catalog(
            engine=self.engine,
            tables=tuple(table for table in self.tables if table.name not in names),
        )

    def fingerprint(self) -> str:
        """A SHA-256 over the catalog's canonical JSON form."""
        return "sha256:" + hashlib.sha256(_canonical(self).encode()).hexdigest()


def normalize(catalog: Catalog) -> Catalog:
    """Sort tables, foreign keys and nothing else, so equal catalogs compare equal.

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
    )


def _canonical(catalog: Catalog) -> str:
    return json.dumps(
        normalize(catalog).model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )


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
    text = json.dumps(lock.model_dump(mode="json"), indent=2, sort_keys=True)
    path.write_text(text + "\n", encoding="utf-8")


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
