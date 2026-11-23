# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The schemas' objects besides tables, with their dependencies.

Objects are views and materialized views, functions, procedures and packages,
triggers, types and sequences, from ``ALL_OBJECTS``; names Oracle generates
(identity columns' sequences, recycle-bin objects) are left out. A package's
or type's specification and body are one object. Definitions are
``ALL_SOURCE`` for PL/SQL, ``ALL_VIEWS`` and ``ALL_MVIEWS`` for views, and
``ALL_SEQUENCES``' settings for sequences (not the next number, which moves).
Dependencies come from ``ALL_DEPENDENCIES``, which Oracle keeps exactly for
every object, PL/SQL included, so none are inferred.
"""

from collections import defaultdict

from datadouble.catalog import (
    Dependency,
    ObjectKind,
    ObjectRef,
    SourceObject,
    definition_fingerprint,
)
from datadouble.names import TableName
from datadouble_oracle.driver import Cursor, Value

_OBJECTS = """
SELECT o.owner, o.object_name, o.object_type
FROM all_objects o
WHERE o.owner IN ({0}) AND o.generated = 'N'
  AND o.object_type IN ('VIEW', 'MATERIALIZED VIEW', 'FUNCTION', 'PROCEDURE', 'PACKAGE',
                        'TRIGGER', 'TYPE', 'SEQUENCE')
  AND o.object_name NOT LIKE 'ISEQ$$%' AND o.object_name NOT LIKE 'BIN$%'
ORDER BY o.owner, o.object_name, o.object_type
"""

_SOURCE = """
SELECT s.owner, s.name, s.type, s.text
FROM all_source s
WHERE s.owner IN ({0})
ORDER BY s.owner, s.name, s.type, s.line
"""

_VIEWS = """
SELECT v.owner, v.view_name, v.text FROM all_views v WHERE v.owner IN ({0})
"""

_MVIEWS = """
SELECT m.owner, m.mview_name, m.query FROM all_mviews m WHERE m.owner IN ({0})
"""

_SEQUENCES = """
SELECT q.sequence_owner, q.sequence_name,
       'sequence minvalue ' || q.min_value || ' maxvalue ' || q.max_value
       || ' increment ' || q.increment_by || ' cycle ' || q.cycle_flag
       || ' order ' || q.order_flag || ' cache ' || q.cache_size
FROM all_sequences q WHERE q.sequence_owner IN ({0})
"""

_DEPENDENCIES = """
SELECT d.owner, d.name, d.type, d.referenced_owner, d.referenced_name, d.referenced_type
FROM all_dependencies d
WHERE d.owner IN ({0}) AND d.referenced_owner IN ({0})
"""

_KINDS = {
    "VIEW": ObjectKind.VIEW,
    "MATERIALIZED VIEW": ObjectKind.MATERIALIZED_VIEW,
    "FUNCTION": ObjectKind.FUNCTION,
    "PROCEDURE": ObjectKind.PROCEDURE,
    "PACKAGE": ObjectKind.PACKAGE,
    "PACKAGE BODY": ObjectKind.PACKAGE,
    "TRIGGER": ObjectKind.TRIGGER,
    "TYPE": ObjectKind.TYPE,
    "TYPE BODY": ObjectKind.TYPE,
    "SEQUENCE": ObjectKind.SEQUENCE,
}
"""Oracle's object types, a body standing for its specification's object."""


type _Refs = dict[tuple[str, str, ObjectKind], ObjectRef]


def read_objects(
    cursor: Cursor, schemas: tuple[str, ...], tables: frozenset[TableName], binds: str
) -> tuple[SourceObject, ...]:
    """The objects of ``schemas``; ``binds`` names the schemas' bind variables."""
    params: dict[str, Value] = {f"s{i}": schema for i, schema in enumerate(schemas)}
    cursor.execute(_OBJECTS.format(binds), params)
    refs: _Refs = {}
    for owner, name, kind in cursor.fetchall():
        ref = ObjectRef(kind=_KINDS[str(kind)], namespace=(str(owner),), name=str(name))
        refs[(str(owner), ref.name, ref.kind)] = ref
    text = _definitions(cursor, refs, binds, params)
    edges = _dependencies(cursor, refs, tables, binds, params)
    return tuple(
        SourceObject(
            ref=ref,
            fingerprint=definition_fingerprint("".join(text[ref])),
            depends_on=tuple(dict.fromkeys(e for e in edges[ref] if e is not None)),
        )
        for ref in refs.values()
    )


def _definitions(
    cursor: Cursor, refs: _Refs, binds: str, params: dict[str, Value]
) -> defaultdict[ObjectRef | None, list[str]]:
    """Each object's definition, in pieces; rows of objects not ours go under None."""
    text: defaultdict[ObjectRef | None, list[str]] = defaultdict(list)
    cursor.execute(_SOURCE.format(binds), params)
    for owner, name, kind, line in cursor.fetchall():
        text[_ref(refs, owner, name, kind)].append(str(line or ""))
    for query, kind in (
        (_VIEWS, "VIEW"),
        (_MVIEWS, "MATERIALIZED VIEW"),
        (_SEQUENCES, "SEQUENCE"),
    ):
        cursor.execute(query.format(binds), params)
        for owner, name, definition in cursor.fetchall():
            text[_ref(refs, owner, name, kind)].append(str(definition or ""))
    return text


def _dependencies(
    cursor: Cursor,
    refs: _Refs,
    tables: frozenset[TableName],
    binds: str,
    params: dict[str, Value],
) -> defaultdict[ObjectRef | None, list[Dependency | None]]:
    """Each object's edges to the schemas' tables and objects; None stands for anything else."""
    edges: defaultdict[ObjectRef | None, list[Dependency | None]] = defaultdict(list)
    cursor.execute(_DEPENDENCIES.format(binds), params)
    for owner, name, kind, target_owner, target_name, target_kind in cursor.fetchall():
        ref = _ref(refs, owner, name, kind)
        table = TableName.of(str(target_owner), str(target_name))
        target = _ref(refs, target_owner, target_name, target_kind)
        is_table = str(target_kind) == "TABLE" and table in tables
        edges[ref].append(
            Dependency(table=table)
            if is_table
            else (None if target is None or target == ref else Dependency(object=target))
        )
    return edges


def _ref(refs: _Refs, owner: object, name: object, kind: object) -> ObjectRef | None:
    """The object an owner, name and Oracle object type name, if it is one of ours."""
    found = _KINDS.get(str(kind))
    return None if found is None else refs.get((str(owner), str(name), found))
