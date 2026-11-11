# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Reading a PostgreSQL catalog into Datadouble's engine-neutral model.

Tables are ordinary and partitioned tables outside the system schemas;
partitions are represented by their partitioned parent, and tables owned by
extensions are left out. Types are spelled with ``format_type`` under an
empty search path, so user-defined types come out schema-qualified.
"""

from collections import defaultdict
from dataclasses import dataclass

import psycopg

from datadouble.catalog import Catalog, Column, ColumnKind, ForeignKey, Table
from datadouble.names import ColumnName, EngineName, TableName

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
SELECT con.conrelid, con.contype, con.conname, con.confrelid,
       array_agg(a.attname ORDER BY k.ord), array_agg(fa.attname ORDER BY k.ord)
FROM pg_catalog.pg_constraint con
CROSS JOIN LATERAL unnest(con.conkey, coalesce(con.confkey, con.conkey))
    WITH ORDINALITY AS k(attnum, fattnum, ord)
JOIN pg_catalog.pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
LEFT JOIN pg_catalog.pg_attribute fa ON fa.attrelid = con.confrelid AND fa.attnum = k.fattnum
WHERE con.conrelid = ANY(%s) AND con.contype IN ('p', 'f') AND con.conparentid = 0
GROUP BY con.conrelid, con.contype, con.conname, con.confrelid
ORDER BY con.conrelid, con.conname
"""

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


@dataclass(frozen=True, slots=True)
class Physical:
    """What the adapter needs beyond the engine-neutral catalog."""

    partitioned: frozenset[TableName]
    """Partitioned tables, read without ``ONLY`` so their partitions are included."""
    generated: dict[TableName, frozenset[ColumnName]]
    """Generated columns, which COPY leaves out."""


def introspect(connection: psycopg.Connection[tuple[object, ...]]) -> tuple[Catalog, Physical]:
    """Read the catalog in one transaction with an empty search path."""
    with connection.transaction(), connection.cursor() as cursor:
        # An empty search path makes format_type qualify user-defined types. It
        # is put back afterwards, since this may run inside the snapshot's
        # transaction, where spec SQL relies on the usual search path.
        saved = cursor.execute("SELECT pg_catalog.current_setting('search_path')").fetchone()
        cursor.execute("SELECT pg_catalog.set_config('search_path', '', true)")
        cursor.execute(_TABLES)
        tables = {
            int(str(oid)): (TableName.of(str(nsp), str(rel)), str(kind))
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
        for relid, contype, name, target, names, target_names in cursor.fetchall():
            own = tuple(ColumnName(str(n)) for n in _strings(names))
            if contype == "p":
                primary[int(str(relid))] = own
            elif int(str(target)) in tables:
                foreign[int(str(relid))].append(
                    ForeignKey(
                        name=str(name),
                        columns=own,
                        target=tables[int(str(target))][0],
                        target_columns=tuple(ColumnName(str(n)) for n in _strings(target_names)),
                    )
                )
        cursor.execute(
            "SELECT pg_catalog.set_config('search_path', %s, true)",
            [str(saved[0]) if saved else ""],
        )
    catalog = Catalog(
        engine=ENGINE,
        tables=tuple(
            Table(
                name=name,
                columns=tuple(columns[oid]),
                primary_key=primary.get(oid, ()),
                foreign_keys=tuple(foreign[oid]),
            )
            for oid, (name, _) in tables.items()
        ),
    )
    physical = Physical(
        partitioned=frozenset(name for name, kind in tables.values() if kind == "p"),
        generated={name: frozenset(cols) for name, cols in generated.items()},
    )
    return catalog, physical


def _strings(value: object) -> list[str]:
    items: list[object] = list(value) if isinstance(value, list) else []  # pyright: ignore[reportUnknownArgumentType] - psycopg returns arrays as untyped lists
    return [str(item) for item in items]
