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

EngineName = NewType("EngineName", str)
"""The name an engine adapter registers under, such as ``postgres``."""

Generation = NewType("Generation", str)
"""The ID shared by every image from one build; sorts by build time."""

ColumnName = NewType("ColumnName", str)
"""A column name as the catalog spells it."""

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
    """A table (or collection, index, object set) in a namespace.

    ``namespace`` is the engine's namespace: a schema for relational engines,
    a database for MongoDB, and so on. It is empty for engines with a single
    namespace.
    """

    __pydantic_config__ = ConfigDict(strict=True, extra="forbid")

    namespace: str
    name: str

    def __str__(self) -> str:
        """``namespace.name``, or ``name`` when there is no namespace."""
        return f"{self.namespace}.{self.name}" if self.namespace else self.name

    @classmethod
    def of(cls, namespace: str, name: str) -> Self:
        """Build a table name."""
        return cls(namespace=namespace, name=name)


@dataclass(frozen=True, order=True, slots=True)
class ColumnRef:
    """A column of a particular table."""

    __pydantic_config__ = ConfigDict(strict=True, extra="forbid")

    table: TableName
    column: ColumnName

    def __str__(self) -> str:
        """``schema.table.column``."""
        return f"{self.table}.{self.column}"
