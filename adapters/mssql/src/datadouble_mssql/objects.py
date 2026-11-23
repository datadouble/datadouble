# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""A SQL Server database's objects besides tables, with their dependencies.

Objects are views, procedures, functions, DML triggers and sequences from
``sys.objects``, and user-defined alias and table types from ``sys.types``.
Definitions are the modules' text (``sys.sql_modules``), and the catalog's
settings for sequences and types. Dependencies come from
``sys.sql_expression_dependencies``, which SQL Server records for every
module; a trigger's table is its parent. A reference SQL Server could not
bind when the module was created (deferred name resolution) is matched by
its schema and name, and that edge is marked inferred; an exact edge to the
same target wins. Three-part names in the current database count.

Tables need objects too: the functions and sequences their defaults, check
constraints and computed columns use (recorded in the same view, under the
constraint or the table), and the alias types of their columns.
"""

from collections import defaultdict
from collections.abc import Sequence
from typing import Protocol

from datadouble.catalog import (
    Dependency,
    ObjectKind,
    ObjectRef,
    SourceObject,
    definition_fingerprint,
)
from datadouble.names import TableName
from datadouble.objects import merge


class Cursor(Protocol):
    """What reading the objects needs of a cursor."""

    def execute(self, operation: str, /) -> object:
        """Run a query."""
        ...

    def fetchall(self) -> Sequence[tuple[object, ...]]:
        """The query's rows."""
        ...


_TABLE_IDS = """
SELECT t.object_id, s.name, t.name
FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE t.is_ms_shipped = 0
"""

_MODULES = """
SELECT o.object_id, s.name, o.name, RTRIM(o.type), COALESCE(m.definition, N''),
       o.parent_object_id
FROM sys.objects o
JOIN sys.schemas s ON s.schema_id = o.schema_id
LEFT JOIN sys.sql_modules m ON m.object_id = o.object_id
WHERE o.is_ms_shipped = 0 AND o.type IN ('V', 'P', 'FN', 'IF', 'TF', 'TR')
ORDER BY s.name, o.name
"""

_SEQUENCES = """
SELECT q.object_id, s.name, q.name,
       CONCAT('sequence ', TYPE_NAME(q.user_type_id),
              ' start ', CAST(q.start_value AS NVARCHAR(64)),
              ' increment ', CAST(q.increment AS NVARCHAR(64)),
              ' minvalue ', CAST(q.minimum_value AS NVARCHAR(64)),
              ' maxvalue ', CAST(q.maximum_value AS NVARCHAR(64)),
              ' cycle ', CAST(q.is_cycling AS INT), ' cache ', COALESCE(q.cache_size, 0))
FROM sys.sequences q JOIN sys.schemas s ON s.schema_id = q.schema_id
WHERE q.is_ms_shipped = 0
ORDER BY s.name, q.name
"""

_TYPES = """
SELECT t.user_type_id, s.name, t.name,
       CASE WHEN t.is_table_type = 1 THEN CONCAT('table (', (
           SELECT STRING_AGG(CONCAT(c.name, ' ', TYPE_NAME(c.user_type_id), '(', c.max_length, ',',
                                    c.precision, ',', c.scale, ') ', c.is_nullable), ', ')
                  WITHIN GROUP (ORDER BY c.column_id)
           FROM sys.table_types tt JOIN sys.columns c ON c.object_id = tt.type_table_object_id
           WHERE tt.user_type_id = t.user_type_id), ')')
       ELSE CONCAT(TYPE_NAME(t.system_type_id), '(', t.max_length, ',', t.precision, ',',
                   t.scale, ') ', t.is_nullable)
       END
FROM sys.types t JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE t.is_user_defined = 1 AND t.is_assembly_type = 0
ORDER BY s.name, t.name
"""

_DEPENDENCIES = """
SELECT d.referencing_id, d.referenced_class, d.referenced_id,
       COALESCE(d.referenced_schema_name, N''), d.referenced_entity_name
FROM sys.sql_expression_dependencies d
WHERE d.referencing_class = 1
  AND (d.referenced_database_name IS NULL OR d.referenced_database_name = DB_NAME())
  AND d.referenced_server_name IS NULL AND d.referenced_class IN (1, 6)
"""

_CONSTRAINTS = """
SELECT o.object_id, o.parent_object_id
FROM sys.objects o
WHERE o.is_ms_shipped = 0 AND o.type IN ('D', 'C')
"""
"""Default and check constraints, whose dependencies are their table's."""

_COLUMN_TYPES = """
SELECT DISTINCT c.object_id, c.user_type_id
FROM sys.columns c
JOIN sys.tables t ON t.object_id = c.object_id
JOIN sys.types y ON y.user_type_id = c.user_type_id
WHERE t.is_ms_shipped = 0 AND y.is_user_defined = 1
"""

_KINDS = {
    "V": ObjectKind.VIEW,
    "P": ObjectKind.PROCEDURE,
    "FN": ObjectKind.FUNCTION,
    "IF": ObjectKind.FUNCTION,
    "TF": ObjectKind.FUNCTION,
    "TR": ObjectKind.TRIGGER,
}

_OBJECT = 1
_TYPE = 6
"""``referenced_class`` values: an object (or a column of one), and a type."""

type _Node = TableName | ObjectRef


def _edge(target: _Node, *, inferred: bool = False) -> Dependency:
    if isinstance(target, TableName):
        return Dependency(table=target, inferred=inferred)
    return Dependency(object=target, inferred=inferred)


class _Reader:
    """The objects as they are read: definitions, and the edges found so far."""

    def __init__(self, cursor: Cursor) -> None:
        self.cursor = cursor
        self.ids: dict[tuple[int, int], _Node] = {}
        self.definitions: dict[ObjectRef, str] = {}
        self.edges: defaultdict[_Node, list[Dependency]] = defaultdict(list)
        self.constraints: dict[int, int] = {}
        """Each default and check constraint's table."""

    def tables(self) -> None:
        self.cursor.execute(_TABLE_IDS)
        for object_id, schema, name in self.cursor.fetchall():
            self.ids[(_OBJECT, int(str(object_id)))] = TableName.of(str(schema), str(name))

    def modules(self) -> None:
        """Views, routines and triggers, each trigger depending on its table."""
        parents: dict[ObjectRef, int] = {}
        self.cursor.execute(_MODULES)
        for object_id, schema, name, kind, definition, parent in self.cursor.fetchall():
            ref = ObjectRef(kind=_KINDS[str(kind)], namespace=(str(schema),), name=str(name))
            self.ids[(_OBJECT, int(str(object_id)))] = ref
            self.definitions[ref] = str(definition)
            if ref.kind is ObjectKind.TRIGGER:
                parents[ref] = int(str(parent))
        for ref, parent in parents.items():
            self.edges[ref].append(_edge(self.ids[(_OBJECT, parent)]))

    def others(self, query: str, kind: ObjectKind, klass: int) -> None:
        """Sequences or types, whose definitions the query spells out."""
        self.cursor.execute(query)
        for object_id, schema, name, definition in self.cursor.fetchall():
            ref = ObjectRef(kind=kind, namespace=(str(schema),), name=str(name))
            self.ids[(klass, int(str(object_id)))] = ref
            self.definitions[ref] = str(definition)

    def merged(self, node: _Node) -> tuple[Dependency, ...]:
        """A node's exact edges, then the inferred ones no exact edge covers."""
        found = self.edges[node]
        return merge([e for e in found if not e.inferred], [e for e in found if e.inferred])

    def column_types(self) -> None:
        """Each table's columns of alias and table types."""
        self.cursor.execute(_COLUMN_TYPES)
        for object_id, type_id in self.cursor.fetchall():
            table = self.ids.get((_OBJECT, int(str(object_id))))
            kind = self.ids.get((_TYPE, int(str(type_id))))
            if isinstance(table, TableName) and isinstance(kind, ObjectRef):
                self.edges[table].append(_edge(kind))

    def dependencies(self) -> None:
        """Bound references by ID; unbound ones by schema and name, as inferred edges."""
        self.cursor.execute(_CONSTRAINTS)
        self.constraints = {
            int(str(object_id)): int(str(parent)) for object_id, parent in self.cursor.fetchall()
        }
        by_name = {_name_key(node): node for node in self.ids.values()}
        self.cursor.execute(_DEPENDENCIES)
        for row in self.cursor.fetchall():
            found = self.edge(row, by_name)
            if found is not None:
                self.edges[found[0]].append(found[1])

    def edge(
        self, row: tuple[object, ...], by_name: dict[tuple[str, str], _Node]
    ) -> tuple[_Node, Dependency] | None:
        """The edge one row records, if it is one: from an object, or from a table to an object.

        A constraint's references are its table's.
        """
        referencing, klass, referenced, schema, name = row
        own = int(str(referencing))
        owner = self.ids.get((_OBJECT, self.constraints.get(own, own)))
        unbound = referenced is None
        target = (
            by_name.get(((str(schema) or _namespace(owner)).lower(), str(name).lower()))
            if unbound
            else self.ids.get((int(str(klass)), int(str(referenced))))
        )
        if owner is None or target is None or target == owner:
            return None
        if isinstance(owner, TableName) and not isinstance(target, ObjectRef):
            return None
        return (owner, _edge(target, inferred=unbound))


def read_objects(
    cursor: Cursor,
) -> tuple[tuple[SourceObject, ...], dict[TableName, tuple[Dependency, ...]]]:
    """The database's objects besides tables, with their dependencies, and what tables need."""
    reader = _Reader(cursor)
    reader.tables()
    reader.modules()
    reader.others(_SEQUENCES, ObjectKind.SEQUENCE, _OBJECT)
    reader.others(_TYPES, ObjectKind.TYPE, _TYPE)
    reader.dependencies()
    reader.column_types()
    objects = tuple(
        SourceObject(
            ref=ref,
            fingerprint=definition_fingerprint(definition),
            depends_on=reader.merged(ref),
        )
        for ref, definition in reader.definitions.items()
    )
    needs = {
        node: reader.merged(node) for node in list(reader.edges) if isinstance(node, TableName)
    }
    return objects, needs


def _namespace(node: _Node | None) -> str:
    """The schema a node is in, or ``dbo`` for none."""
    return "dbo" if node is None else node.namespace[-1]


def _name_key(node: _Node) -> tuple[str, str]:
    return (node.namespace[-1].lower(), node.name.lower())
