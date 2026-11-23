# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Reading a PostgreSQL catalog into Datadouble's engine-neutral model.

Each database is read on its own connection, and its tables are named
``database.schema.table``. Tables are ordinary and partitioned tables outside the system schemas;
partitions are represented by their partitioned root (a reference to a
partition becomes a reference to its root), and tables owned by extensions are
left out. Types are spelled with ``format_type`` under an
empty search path, so user-defined types come out schema-qualified.

Every extension except ``plpgsql``, which every database has, is a
requirement at the version the source runs.

Objects are the functions and procedures, views and materialized views,
triggers, user-defined types (enums, domains, composite and range types) and
sequences outside the system schemas that no extension owns. Their
definitions come from ``pg_get_functiondef``, ``pg_get_viewdef`` and
``pg_get_triggerdef``, and from the catalog for types and sequences. Their
dependencies come from ``pg_depend``, which is exact for views, triggers,
types, sequences and routines with SQL-standard bodies; other routine bodies
(PL/pgSQL and the like) are parsed for names, and those edges are marked
inferred. A table needs the types of its columns (or a typed table's type),
the sequences and functions its defaults, check constraints and indexes
use, the functions and tables its row-level security policies use, and an
inheriting table its parent, from ``pg_depend`` too.
Objects are named ``database.schema.object`` like tables, and a
trigger ``database.schema.table.trigger``'s last part is ``table.trigger``,
since trigger names are only unique per table. Each database is read on its
own, so no edge crosses databases.
"""

from collections import defaultdict
from dataclasses import dataclass

import psycopg
from pydantic import BaseModel, ConfigDict

from datadouble.catalog import (
    Catalog,
    Column,
    ColumnKind,
    Dependency,
    ForeignKey,
    ObjectKind,
    ObjectRef,
    Requirement,
    RequirementKind,
    SourceObject,
    Table,
    definition_fingerprint,
)
from datadouble.names import ColumnName, EngineName, TableName
from datadouble.objects import Candidates, Dialect, infer_dependencies, merge

ENGINE = EngineName("postgres")

_TABLES = """
SELECT c.oid, n.nspname, c.relname, c.relkind
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p')
  AND NOT c.relispartition
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\\_toast%'
  AND n.nspname NOT LIKE 'pg\\_temp%'
  AND NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_depend d
    WHERE d.classid = 'pg_catalog.pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e'
  )
ORDER BY n.nspname, c.relname
"""

_COLUMNS = """
SELECT a.attrelid, a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod),
       a.attnotnull, a.attgenerated <> '', bt.typname, bt.typcategory
FROM pg_catalog.pg_attribute a
JOIN pg_catalog.pg_type t ON t.oid = a.atttypid
JOIN pg_catalog.pg_type bt ON bt.oid = CASE WHEN t.typtype = 'd' THEN t.typbasetype ELSE t.oid END
WHERE a.attrelid = ANY(%s) AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attrelid, a.attnum
"""

_KEYS = """
SELECT con.conrelid, con.contype, con.conname, tn.nspname, tc.relname,
       array_agg(a.attname ORDER BY k.ord), array_agg(fa.attname ORDER BY k.ord)
FROM pg_catalog.pg_constraint con
LEFT JOIN pg_catalog.pg_class tc
  ON tc.oid = coalesce(pg_catalog.pg_partition_root(con.confrelid)::oid, con.confrelid)
LEFT JOIN pg_catalog.pg_namespace tn ON tn.oid = tc.relnamespace
CROSS JOIN LATERAL unnest(con.conkey, coalesce(con.confkey, con.conkey))
    WITH ORDINALITY AS k(attnum, fattnum, ord)
JOIN pg_catalog.pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
LEFT JOIN pg_catalog.pg_attribute fa ON fa.attrelid = con.confrelid AND fa.attnum = k.fattnum
WHERE con.conrelid = ANY(%s) AND con.contype IN ('p', 'f') AND con.conparentid = 0
GROUP BY con.conrelid, con.contype, con.conname, tn.nspname, tc.relname
ORDER BY con.conrelid, con.conname
"""

_EXTENSIONS = """
SELECT e.extname, e.extversion, n.nspname
FROM pg_catalog.pg_extension e
JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace
WHERE e.extname <> 'plpgsql'
ORDER BY e.oid
"""

_ROUTINES = """
SELECT 'pg_catalog.pg_proc'::regclass::oid, p.oid, n.nspname, p.proname, p.prokind,
       '(' || pg_catalog.pg_get_function_identity_arguments(p.oid) || ')',
       pg_catalog.pg_get_functiondef(p.oid),
       CASE WHEN l.lanname IN ('c', 'internal') THEN '' ELSE p.prosrc END, l.lanname
FROM pg_catalog.pg_proc p
JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
JOIN pg_catalog.pg_language l ON l.oid = p.prolang
WHERE p.prokind IN ('f', 'p')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\\_toast%'
  AND n.nspname NOT LIKE 'pg\\_temp%'
  AND NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_depend e
    WHERE e.classid = 'pg_catalog.pg_proc'::regclass AND e.objid = p.oid AND e.deptype = 'e'
  )
"""

_RELATIONS = """
SELECT 'pg_catalog.pg_class'::regclass::oid, c.oid, n.nspname, c.relname, c.relkind, NULL,
       CASE c.relkind
         WHEN 'S' THEN (
           SELECT 'sequence ' || pg_catalog.format_type(s.seqtypid, NULL)
               || ' increment ' || s.seqincrement || ' minvalue ' || s.seqmin
               || ' maxvalue ' || s.seqmax || ' start ' || s.seqstart
               || ' cache ' || s.seqcache || CASE WHEN s.seqcycle THEN ' cycle' ELSE '' END
           FROM pg_catalog.pg_sequence s WHERE s.seqrelid = c.oid)
         ELSE pg_catalog.pg_get_viewdef(c.oid)
       END,
       '', ''
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('v', 'm', 'S')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\\_toast%'
  AND n.nspname NOT LIKE 'pg\\_temp%'
  AND NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_depend e
    WHERE e.classid = 'pg_catalog.pg_class'::regclass AND e.objid = c.oid AND e.deptype = 'e'
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_depend i
    WHERE i.classid = 'pg_catalog.pg_class'::regclass AND i.objid = c.oid AND i.deptype = 'i'
  )
"""

_TRIGGERS = """
SELECT 'pg_catalog.pg_trigger'::regclass::oid, t.oid, n.nspname,
       c.relname || '.' || t.tgname, 'T', NULL, pg_catalog.pg_get_triggerdef(t.oid), '', ''
FROM pg_catalog.pg_trigger t
JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE NOT t.tgisinternal AND (NOT c.relispartition OR t.tgparentid = 0)
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\\_toast%'
  AND n.nspname NOT LIKE 'pg\\_temp%'
"""

_TYPES = """
SELECT 'pg_catalog.pg_type'::regclass::oid, t.oid, n.nspname, t.typname, 't', NULL,
       CASE t.typtype
         WHEN 'e' THEN 'enum ' || (
           SELECT pg_catalog.string_agg(pg_catalog.quote_literal(e.enumlabel), ', '
                                        ORDER BY e.enumsortorder)
           FROM pg_catalog.pg_enum e WHERE e.enumtypid = t.oid)
         WHEN 'd' THEN 'domain ' || pg_catalog.format_type(t.typbasetype, t.typtypmod)
           || coalesce(' default ' || t.typdefault, '')
           || CASE WHEN t.typnotnull THEN ' not null' ELSE '' END
           || coalesce((
             SELECT ' ' || pg_catalog.string_agg(pg_catalog.pg_get_constraintdef(k.oid), ' '
                                                ORDER BY k.conname)
             FROM pg_catalog.pg_constraint k WHERE k.contypid = t.oid), '')
         WHEN 'c' THEN 'composite ' || (
           SELECT pg_catalog.string_agg(
             pg_catalog.quote_ident(a.attname) || ' '
               || pg_catalog.format_type(a.atttypid, a.atttypmod), ', ' ORDER BY a.attnum)
           FROM pg_catalog.pg_attribute a
           WHERE a.attrelid = t.typrelid AND a.attnum > 0 AND NOT a.attisdropped)
         ELSE 'range ' || (
           SELECT pg_catalog.format_type(r.rngsubtype, NULL)
           FROM pg_catalog.pg_range r WHERE r.rngtypid = t.oid)
       END,
       '', ''
FROM pg_catalog.pg_type t
JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
WHERE (t.typtype IN ('e', 'd', 'r')
       OR (t.typtype = 'c' AND (
         SELECT c.relkind FROM pg_catalog.pg_class c WHERE c.oid = t.typrelid) = 'c'))
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\\_toast%'
  AND n.nspname NOT LIKE 'pg\\_temp%'
  AND NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_depend e
    WHERE e.classid = 'pg_catalog.pg_type'::regclass AND e.objid = t.oid AND e.deptype = 'e'
  )
"""

_CATALOG_OIDS = "SELECT 'pg_catalog.pg_class'::regclass::oid, 'pg_catalog.pg_type'::regclass::oid"

_PARTITIONS = """
SELECT c.oid, pg_catalog.pg_partition_root(c.oid)::oid
FROM pg_catalog.pg_class c
WHERE c.relispartition
"""
"""Each partition and its partitioned root, which stands for it."""

_TYPE_ALIASES = """
SELECT t.oid, t.typrelid, t.typelem, t.typcategory = 'A'
FROM pg_catalog.pg_type t
WHERE t.typrelid <> 0 OR t.typcategory = 'A'
ORDER BY t.typcategory = 'A', t.oid
"""
"""Row types stand for their relation, and array types for their element; row types
come first, so an array of a row type finds the relation."""

_DEPENDS = """
SELECT DISTINCT
  CASE WHEN d.classid = 'pg_catalog.pg_rewrite'::regclass
       THEN 'pg_catalog.pg_class'::regclass ELSE d.classid END::oid,
  CASE WHEN d.classid = 'pg_catalog.pg_rewrite'::regclass THEN r.ev_class ELSE d.objid END,
  d.refclassid::oid, d.refobjid
FROM pg_catalog.pg_depend d
LEFT JOIN pg_catalog.pg_rewrite r
  ON d.classid = 'pg_catalog.pg_rewrite'::regclass AND r.oid = d.objid
WHERE d.deptype IN ('n', 'a')
  AND d.classid IN ('pg_catalog.pg_proc'::regclass, 'pg_catalog.pg_class'::regclass,
                    'pg_catalog.pg_trigger'::regclass, 'pg_catalog.pg_type'::regclass,
                    'pg_catalog.pg_rewrite'::regclass)
  AND d.refclassid IN ('pg_catalog.pg_proc'::regclass, 'pg_catalog.pg_class'::regclass,
                       'pg_catalog.pg_type'::regclass)
"""
"""Every normal and automatic dependency, a view's rule standing for the view."""

_TABLE_DEPENDS = """
SELECT DISTINCT n.relid, d.refclassid::oid, d.refobjid
FROM pg_catalog.pg_depend d
CROSS JOIN LATERAL (
  SELECT CASE d.classid
    WHEN 'pg_catalog.pg_class'::regclass THEN CASE
      WHEN d.objsubid > 0 THEN d.objid
      -- An index's expressions and predicate count as its table's; a typed
      -- table needs its type, and an inheriting table its parent.
      ELSE coalesce(
        (SELECT i.indrelid FROM pg_catalog.pg_index i WHERE i.indexrelid = d.objid),
        (SELECT c.oid FROM pg_catalog.pg_class c
         WHERE c.oid = d.objid AND c.relkind IN ('r', 'p')))
    END
    WHEN 'pg_catalog.pg_policy'::regclass THEN (
      SELECT p.polrelid FROM pg_catalog.pg_policy p WHERE p.oid = d.objid)
    WHEN 'pg_catalog.pg_attrdef'::regclass THEN (
      SELECT a.adrelid FROM pg_catalog.pg_attrdef a WHERE a.oid = d.objid)
    WHEN 'pg_catalog.pg_constraint'::regclass THEN (
      SELECT k.conrelid FROM pg_catalog.pg_constraint k
      WHERE k.oid = d.objid AND k.contype = 'c')
  END AS relid
) n
WHERE d.deptype = 'n' AND n.relid IS NOT NULL
  AND d.classid IN ('pg_catalog.pg_class'::regclass, 'pg_catalog.pg_attrdef'::regclass,
                    'pg_catalog.pg_constraint'::regclass, 'pg_catalog.pg_policy'::regclass)
  AND d.refclassid IN ('pg_catalog.pg_proc'::regclass, 'pg_catalog.pg_class'::regclass,
                       'pg_catalog.pg_type'::regclass)
"""
"""What tables need: a column's type, a typed table's type, an inheriting
table's parent, the sequences and functions its default uses, the functions
its check constraints, index expressions and predicates call, and the
functions and tables its row-level security policies use."""

_OBJECT_KINDS = {
    "f": ObjectKind.FUNCTION,
    "p": ObjectKind.PROCEDURE,
    "v": ObjectKind.VIEW,
    "m": ObjectKind.MATERIALIZED_VIEW,
    "S": ObjectKind.SEQUENCE,
    "T": ObjectKind.TRIGGER,
    "t": ObjectKind.TYPE,
}

_DIALECT = Dialect(quotes='"', dollar_quotes=True)
_PYTHON = Dialect(quotes='"', dollar_quotes=True, hash_comments=True)
"""PL/Python bodies, whose comments start with ``#``."""

_KINDS = {
    "int2": ColumnKind.INTEGER,
    "int4": ColumnKind.INTEGER,
    "int8": ColumnKind.INTEGER,
    "numeric": ColumnKind.DECIMAL,
    "float4": ColumnKind.FLOAT,
    "float8": ColumnKind.FLOAT,
    "bool": ColumnKind.BOOLEAN,
    "date": ColumnKind.DATE,
    "timestamp": ColumnKind.TIMESTAMP,
    "timestamptz": ColumnKind.TIMESTAMP,
    "time": ColumnKind.TIME,
    "timetz": ColumnKind.TIME,
    "bytea": ColumnKind.BINARY,
    "json": ColumnKind.JSON,
    "jsonb": ColumnKind.JSON,
    "uuid": ColumnKind.UUID,
    "text": ColumnKind.TEXT,
    "varchar": ColumnKind.TEXT,
    "bpchar": ColumnKind.TEXT,
    "name": ColumnKind.TEXT,
    "citext": ColumnKind.TEXT,
}


def kind_of(base_type: str, category: str) -> ColumnKind:
    """The engine-neutral kind of a base type."""
    if category == "A":
        return ColumnKind.ARRAY
    return _KINDS.get(base_type, ColumnKind.OTHER)


class Extension(BaseModel):
    """An installed extension: its version and the schema its objects are in."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    name: str
    version: str
    schema_: str

    def requirement(self) -> Requirement:
        """The extension as a requirement of the source."""
        return Requirement(kind=RequirementKind.EXTENSION, name=self.name, version=self.version)


@dataclass(frozen=True, slots=True)
class Physical:
    """What the adapter needs beyond the engine-neutral catalog."""

    partitioned: frozenset[TableName]
    """Partitioned tables, read without ``ONLY`` so their partitions are included."""
    generated: dict[TableName, frozenset[ColumnName]]
    """Generated columns, which COPY leaves out."""
    extensions: dict[str, tuple[Extension, ...]]
    """Each database's installed extensions, in the order they were created."""

    def merged(self, other: "Physical") -> "Physical":
        """This and another database's details together."""
        return Physical(
            partitioned=self.partitioned | other.partitioned,
            generated={**self.generated, **other.generated},
            extensions={**self.extensions, **other.extensions},
        )


def introspect(
    connection: psycopg.Connection[tuple[object, ...]], database: str
) -> tuple[Catalog, Physical]:
    """Read one database's catalog in one transaction with an empty search path."""
    with connection.transaction(), connection.cursor() as cursor:
        # An empty search path makes format_type qualify user-defined types. It
        # is put back afterwards, since this may run inside the snapshot's
        # transaction, where spec SQL relies on the usual search path.
        saved = cursor.execute("SELECT pg_catalog.current_setting('search_path')").fetchone()
        cursor.execute("SELECT pg_catalog.set_config('search_path', '', true)")
        cursor.execute(_TABLES)
        tables = {
            int(str(oid)): (TableName.of(database, str(nsp), str(rel)), str(kind))
            for oid, nsp, rel, kind in cursor.fetchall()
        }
        oids = list(tables)
        columns: defaultdict[int, list[Column]] = defaultdict(list)
        generated: defaultdict[TableName, set[ColumnName]] = defaultdict(set)
        cursor.execute(_COLUMNS, [oids])
        for relid, name, type_name, not_null, is_generated, base, category in cursor.fetchall():
            column = Column(
                name=ColumnName(str(name)),
                type=str(type_name),
                kind=kind_of(str(base), str(category)),
                nullable=not not_null,
            )
            columns[int(str(relid))].append(column)
            if is_generated:
                generated[tables[int(str(relid))][0]].add(column.name)
        primary: dict[int, tuple[ColumnName, ...]] = {}
        foreign: defaultdict[int, list[ForeignKey]] = defaultdict(list)
        cursor.execute(_KEYS, [oids])
        # A reference to a table outside the catalog (one an extension owns,
        # say) keeps its real target, so the planner asks for a fallback.
        for relid, contype, name, namespace, target, names, target_names in cursor.fetchall():
            own = tuple(ColumnName(str(n)) for n in _strings(names))
            if contype == "p":
                primary[int(str(relid))] = own
            else:
                foreign[int(str(relid))].append(
                    ForeignKey(
                        name=str(name),
                        columns=own,
                        target=TableName.of(database, str(namespace), str(target)),
                        target_columns=tuple(ColumnName(str(n)) for n in _strings(target_names)),
                    )
                )
        table_list = [
            Table(
                name=name,
                columns=tuple(columns[oid]),
                primary_key=primary.get(oid, ()),
                foreign_keys=tuple(foreign[oid]),
            )
            for oid, (name, _) in tables.items()
        ]
        objects, needs = _objects(
            cursor, database, {oid: name for oid, (name, _) in tables.items()}, table_list
        )
        table_list = [
            table.model_copy(update={"depends_on": tuple(dict.fromkeys(needs[table.name]))})
            for table in table_list
        ]
        cursor.execute(_EXTENSIONS)
        extensions = tuple(
            Extension(name=str(name), version=str(version), schema_=str(schema))
            for name, version, schema in cursor.fetchall()
        )
        cursor.execute(
            "SELECT pg_catalog.set_config('search_path', %s, true)",
            [str(saved[0]) if saved else ""],
        )
    catalog = Catalog(
        engine=ENGINE,
        tables=tuple(table_list),
        requirements=tuple(extension.requirement() for extension in extensions),
        objects=objects,
    )
    physical = Physical(
        partitioned=frozenset(name for name, kind in tables.values() if kind == "p"),
        generated={name: frozenset(cols) for name, cols in generated.items()},
        extensions={database: extensions},
    )
    return catalog, physical


type _Node = TableName | ObjectRef
type _Key = tuple[int, int]
"""A catalog row: the OID of the system catalog it is in, and its own OID."""


def _objects(
    cursor: psycopg.Cursor[tuple[object, ...]],
    database: str,
    tables: dict[int, TableName],
    table_list: list[Table],
) -> tuple[tuple[SourceObject, ...], defaultdict[TableName, list[Dependency]]]:
    """The source's objects, with their exact and inferred dependencies, and what tables need."""
    found = cursor.execute(_CATALOG_OIDS).fetchone() or (0, 0)
    pg_class, pg_type = int(str(found[0])), int(str(found[1]))
    nodes: dict[_Key, _Node] = {(pg_class, oid): name for oid, name in tables.items()}
    cursor.execute(_PARTITIONS)
    roots = {int(str(oid)): int(str(root)) for oid, root in cursor.fetchall()}
    nodes |= {(pg_class, oid): tables[root] for oid, root in roots.items() if root in tables}
    definitions: dict[ObjectRef, tuple[str, str, str]] = {}
    for query in (_ROUTINES, _RELATIONS, _TRIGGERS, _TYPES):
        cursor.execute(query)
        for catalog, oid, namespace, name, kind, signature, *text in cursor.fetchall():
            ref = ObjectRef(
                kind=_OBJECT_KINDS[str(kind)],
                namespace=(database, str(namespace)),
                name=str(name),
                signature=None if signature is None else str(signature),
            )
            nodes[(int(str(catalog)), int(str(oid)))] = ref
            definition, body, language = (str(part or "") for part in text)
            definitions[ref] = (definition, body, language)
    _aliases(cursor, nodes, pg_class, pg_type)
    exact: defaultdict[ObjectRef, list[Dependency]] = defaultdict(list)
    cursor.execute(_DEPENDS)
    for classid, objid, refclassid, refobjid in cursor.fetchall():
        owner = nodes.get((int(str(classid)), int(str(objid))))
        target = nodes.get((int(str(refclassid)), int(str(refobjid))))
        if isinstance(owner, ObjectRef) and target is not None and target != owner:
            edge = (
                Dependency(table=target)
                if isinstance(target, TableName)
                else Dependency(object=target)
            )
            exact[owner].append(edge)
    needs: defaultdict[TableName, list[Dependency]] = defaultdict(list)
    cursor.execute(_TABLE_DEPENDS)
    for relid, refclassid, refobjid in cursor.fetchall():
        owner = nodes.get((pg_class, int(str(relid))))
        target = nodes.get((int(str(refclassid)), int(str(refobjid))))
        if isinstance(owner, TableName) and isinstance(target, ObjectRef):
            needs[owner].append(Dependency(object=target))
        elif isinstance(owner, TableName) and isinstance(target, TableName) and target != owner:
            needs[owner].append(Dependency(table=target))
    plain = [SourceObject(ref=ref, fingerprint="") for ref in definitions]
    candidates = Candidates.of(table_list, plain)
    objects = tuple(
        SourceObject(
            ref=ref,
            fingerprint=definition_fingerprint(definition),
            depends_on=merge(
                exact[ref],
                infer_dependencies(
                    body,
                    ref,
                    candidates,
                    _PYTHON if language.startswith("plpython") else _DIALECT,
                ),
            ),
        )
        for ref, (definition, body, language) in definitions.items()
    )
    return objects, needs


def _aliases(
    cursor: psycopg.Cursor[tuple[object, ...]],
    nodes: dict[_Key, _Node],
    pg_class: int,
    pg_type: int,
) -> None:
    """Map row types to their relations, arrays to their elements, composites' rows to the type.

    A standalone composite type's dependencies are recorded on its ``pg_class``
    row, which then stands for the type.
    """
    cursor.execute(_TYPE_ALIASES)
    for oid, relid, element, is_array in cursor.fetchall():
        own, relation = (pg_type, int(str(oid))), (pg_class, int(str(relid)))
        if is_array:
            element_node = nodes.get((pg_type, int(str(element))))
            if element_node is not None:
                nodes.setdefault(own, element_node)
        elif own in nodes:
            nodes[relation] = nodes[own]
        elif relation in nodes:
            nodes[own] = nodes[relation]


def _strings(value: object) -> list[str]:
    items: list[object] = list(value) if isinstance(value, list) else []  # pyright: ignore[reportUnknownArgumentType] - psycopg returns arrays as untyped lists
    return [str(item) for item in items]
