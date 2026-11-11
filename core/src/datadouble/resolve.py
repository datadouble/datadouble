# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Resolving the names a spec uses against a catalog.

A spec names tables as ``table`` or ``namespace.table``, columns as
``[namespace.]table.column``, and references as ``child.column -> parent``.
Names are matched case-insensitively unless the spec sets
``case_sensitive: true``, because engines fold identifier case differently.
"""

import fnmatch
import re
from dataclasses import dataclass

from datadouble.catalog import Catalog, Column, Table
from datadouble.names import ColumnName, ColumnRef, TableName


class ResolveError(Exception):
    """A name in the spec that does not match the catalog."""


@dataclass(frozen=True, slots=True)
class Reference:
    """A parsed ``child[.columns] -> parent[.columns]`` reference."""

    child: Table
    columns: tuple[ColumnName, ...] | None
    parent: Table
    parent_columns: tuple[ColumnName, ...] | None


_ARROW = re.compile(r"\s*->\s*")


class Resolver:
    """Looks up spec names in one catalog."""

    def __init__(self, catalog: Catalog, *, case_sensitive: bool) -> None:
        """Index ``catalog`` for lookups."""
        self.catalog = catalog
        self.case_sensitive = case_sensitive

    def _fold(self, text: str) -> str:
        return text if self.case_sensitive else text.casefold()

    def matches(self, pattern: str, name: TableName) -> bool:
        """Whether a shell-style glob matches a table.

        A pattern with a dot matches ``namespace.table``; one without matches
        the table name in any namespace.
        """
        subject = str(name) if "." in pattern else name.name
        return fnmatch.fnmatchcase(self._fold(subject), self._fold(pattern))

    def glob(self, pattern: str) -> tuple[TableName, ...]:
        """Every table a glob matches, in catalog order."""
        return tuple(name for name in self.catalog.names() if self.matches(pattern, name))

    def _find(self, text: str) -> Table | None:
        wanted = self._fold(text)
        found = [
            table
            for table in self.catalog.tables
            if self._fold(str(table.name)) == wanted or self._fold(table.name.name) == wanted
        ]
        exact = [table for table in found if self._fold(str(table.name)) == wanted]
        if exact:
            return exact[0]
        if len(found) > 1:
            names = ", ".join(str(table.name) for table in found)
            message = f"table {text!r} is ambiguous: it matches {names}; qualify it"
            raise ResolveError(message)
        return found[0] if found else None

    def table(self, text: str) -> Table:
        """The table a spec name refers to."""
        table = self._find(text.strip())
        if table is None:
            message = f"table {text!r} is not in the catalog"
            raise ResolveError(message)
        return table

    def column_of(self, table: Table, text: str) -> Column:
        """The column of ``table`` a spec name refers to."""
        wanted = self._fold(text.strip())
        for column in table.columns:
            if self._fold(column.name) == wanted:
                return column
        message = f"column {text!r} is not in table {table.name}"
        raise ResolveError(message)

    def column(self, text: str) -> ColumnRef:
        """The column a ``[namespace.]table.column`` name refers to."""
        table_text, dot, column_text = text.strip().rpartition(".")
        if not dot or not table_text:
            message = f"{text!r} is not a column name; write table.column"
            raise ResolveError(message)
        table = self.table(table_text)
        return ColumnRef(table=table.name, column=self.column_of(table, column_text).name)

    def _side(self, text: str) -> tuple[Table, tuple[ColumnName, ...] | None]:
        """A table, or a table with columns: ``t``, ``t.c`` or ``t.(c1, c2)``."""
        text = text.strip()
        table_text, opened, listed = text.rpartition(".(")
        if opened and table_text and listed.endswith(")") and "(" not in listed[:-1]:
            table = self.table(table_text)
            names = tuple(self.column_of(table, part).name for part in listed[:-1].split(","))
            return table, names
        whole = self._find(text)
        if whole is not None:
            return whole, None
        table_text, dot, column_text = text.rpartition(".")
        if dot and table_text:
            table = self.table(table_text)
            return table, (self.column_of(table, column_text).name,)
        message = f"{text!r} is not a table or column in the catalog"
        raise ResolveError(message)

    def reference(self, text: str) -> Reference:
        """Parse ``child[.columns] -> parent[.columns]``."""
        match _ARROW.split(text.strip()):
            case [left, right] if left and right:
                child, columns = self._side(left)
                parent, parent_columns = self._side(right)
                return Reference(child, columns, parent, parent_columns)
            case _:
                message = f"{text!r} is not a reference; write child.column -> parent"
                raise ResolveError(message)

    def downward(self, text: str) -> Reference:
        """Parse a ``parent -> child[.columns]`` entry from ``follow.down``."""
        match _ARROW.split(text.strip()):
            case [left, right] if left and right:
                parent, parent_columns = self._side(left)
                child, columns = self._side(right)
                return Reference(child, columns, parent, parent_columns)
            case _:
                message = f"{text!r} is not a downward edge; write parent -> child"
                raise ResolveError(message)
