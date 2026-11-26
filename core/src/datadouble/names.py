# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Typed identifiers.

Catalog objects, images and runs are named by small value types rather than
plain strings, so a column cannot be passed where a table is expected.
"""

import re
from dataclasses import dataclass
from typing import NewType, Self

from pydantic import ConfigDict

SpecName = NewType("SpecName", str)
"""The name of a spec, the first part of every image's identity."""

GroupName = NewType("GroupName", str)
"""The name of an image group (``images:`` in the spec)."""

SourceName = NewType("SourceName", str)
"""The name of one of a spec's sources (``sources:`` in the spec)."""

EngineName = NewType("EngineName", str)
"""The name an engine adapter registers under, such as ``postgres``."""

Generation = NewType("Generation", str)
"""The ID shared by every image from one build; sorts by build time."""

ColumnName = NewType("ColumnName", str)
"""A column name as the catalog spells it."""

FileDigest = NewType("FileDigest", str)
"""The SHA-256 of a file's bytes, written as ``sha256:`` and the hex digest."""

DEFAULT_GROUP = GroupName("all")
"""The single group a spec without an ``images:`` section produces."""

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


def check_name(kind: str, value: str) -> str:
    """Return ``value`` if it is a valid spec, group or channel name.

    These names become path components in stores, so they are limited to
    lowercase letters, digits, ``-`` and ``_``.
    """
    if not _NAME.match(value):
        message = (
            f"{kind} {value!r} must be 1-63 lowercase letters, digits, '-' or '_', "
            "starting with a letter or digit"
        )
        raise ValueError(message)
    return value


@dataclass(frozen=True, order=True, slots=True)
class TableName:
    """A table (or collection, index, object set) and the namespaces it sits in.

    ``namespace`` is the path of namespaces that hold the table, outermost
    first: a schema for SQL Server and Oracle, a database for MySQL, MongoDB
    and Redis, a database and a schema for PostgreSQL, and nothing for engines
    with a single namespace. Its length is fixed per engine, so names compare
    and sort part by part.
    """

    __pydantic_config__ = ConfigDict(strict=True, extra="forbid")

    namespace: tuple[str, ...]
    name: str

    def __post_init__(self) -> None:
        """Refuse empty parts, which would print as stray dots."""
        if not self.name or not all(self.namespace):
            message = f"a table name has an empty part: {self.parts!r}"
            raise ValueError(message)

    def __str__(self) -> str:
        """The parts joined with dots, such as ``shop.public.users``."""
        return ".".join(self.parts)

    @property
    def parts(self) -> tuple[str, ...]:
        """The namespaces and then the name."""
        return (*self.namespace, self.name)

    @property
    def outermost(self) -> str:
        """The outermost namespace (a database, say), or the name when there is none."""
        return self.parts[0]

    @classmethod
    def of(cls, *parts: str) -> Self:
        """Build a table name from its namespaces and name, outermost first."""
        if not parts:
            message = "a table name needs at least its name"
            raise ValueError(message)
        return cls(namespace=parts[:-1], name=parts[-1])


@dataclass(frozen=True, order=True, slots=True)
class ColumnRef:
    """A column of a particular table."""

    __pydantic_config__ = ConfigDict(strict=True, extra="forbid")

    table: TableName
    column: ColumnName

    def __str__(self) -> str:
        """The table's name and then the column's, such as ``public.users.email``."""
        return f"{self.table}.{self.column}"
