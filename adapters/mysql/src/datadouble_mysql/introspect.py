# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Reading a MySQL catalog into Datadouble's engine-neutral model.

The catalog is one database (MySQL's schema): its base tables, their columns,
primary keys and foreign keys. A foreign key into another database keeps its
real target, so the planner asks for a fallback. Generated columns are noted,
since loading leaves them out.
"""

from collections import defaultdict
from dataclasses import dataclass

from pymysql.connections import Connection

from datadouble.catalog import Catalog, Column, ColumnKind, ForeignKey, Table
from datadouble.names import ColumnName, EngineName, TableName

ENGINE = EngineName("mysql")

_TABLES = """
SELECT TABLE_NAME FROM information_schema.TABLES
WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
ORDER BY TABLE_NAME
"""

_COLUMNS = """
SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, DATA_TYPE, IS_NULLABLE, EXTRA
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = %s
ORDER BY TABLE_NAME, ORDINAL_POSITION
"""

_KEYS = """
SELECT k.TABLE_NAME, k.CONSTRAINT_NAME, k.COLUMN_NAME,
       k.REFERENCED_TABLE_SCHEMA, k.REFERENCED_TABLE_NAME, k.REFERENCED_COLUMN_NAME
FROM information_schema.KEY_COLUMN_USAGE k
JOIN information_schema.TABLE_CONSTRAINTS c
  ON c.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA AND c.TABLE_NAME = k.TABLE_NAME
 AND c.CONSTRAINT_NAME = k.CONSTRAINT_NAME
WHERE k.TABLE_SCHEMA = %s AND c.CONSTRAINT_TYPE IN ('PRIMARY KEY', 'FOREIGN KEY')
ORDER BY k.TABLE_NAME, k.CONSTRAINT_NAME, k.ORDINAL_POSITION
"""

_KINDS = {
    "tinyint": ColumnKind.INTEGER,
    "smallint": ColumnKind.INTEGER,
    "mediumint": ColumnKind.INTEGER,
    "int": ColumnKind.INTEGER,
    "bigint": ColumnKind.INTEGER,
    "year": ColumnKind.INTEGER,
    "decimal": ColumnKind.DECIMAL,
    "float": ColumnKind.FLOAT,
    "double": ColumnKind.FLOAT,
    "date": ColumnKind.DATE,
    "datetime": ColumnKind.TIMESTAMP,
    "timestamp": ColumnKind.TIMESTAMP,
    "time": ColumnKind.TIME,
    "char": ColumnKind.TEXT,
    "varchar": ColumnKind.TEXT,
    "tinytext": ColumnKind.TEXT,
    "text": ColumnKind.TEXT,
    "mediumtext": ColumnKind.TEXT,
    "longtext": ColumnKind.TEXT,
    "json": ColumnKind.JSON,
    "binary": ColumnKind.BINARY,
    "varbinary": ColumnKind.BINARY,
    "tinyblob": ColumnKind.BINARY,
    "blob": ColumnKind.BINARY,
    "mediumblob": ColumnKind.BINARY,
    "longblob": ColumnKind.BINARY,
}


def kind_of(data_type: str) -> ColumnKind:
    """The engine-neutral kind of a MySQL data type.

    ``enum``, ``set``, ``bit`` and the spatial types are ``other``, since a
    generated value would not fit them.
    """
    return _KINDS.get(data_type.lower(), ColumnKind.OTHER)


@dataclass(frozen=True, slots=True)
class Physical:
    """What the adapter needs beyond the engine-neutral catalog."""

    generated: dict[TableName, frozenset[ColumnName]]
    """Generated columns, which loading leaves out."""


def _text(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def introspect(connection: Connection, database: str) -> tuple[Catalog, Physical]:
    """Read the catalog of ``database``."""
    with connection.cursor() as cursor:
        cursor.execute(_TABLES, (database,))
        names = [_text(row[0]) for row in cursor.fetchall()]
        columns: defaultdict[str, list[Column]] = defaultdict(list)
        generated: defaultdict[TableName, set[ColumnName]] = defaultdict(set)
        cursor.execute(_COLUMNS, (database,))
        for table, name, column_type, data_type, nullable, extra in cursor.fetchall():
            column = Column(
                name=ColumnName(_text(name)),
                type=_text(column_type),
                kind=kind_of(_text(data_type)),
                nullable=_text(nullable) == "YES",
            )
            columns[_text(table)].append(column)
            # EXTRA says VIRTUAL GENERATED or STORED GENERATED for generated
            # columns, and DEFAULT_GENERATED for ones with a default expression.
            if _text(extra).upper().split(" ")[0] in {"VIRTUAL", "STORED"}:
                generated[TableName.of(database, _text(table))].add(column.name)
        primary: defaultdict[str, list[ColumnName]] = defaultdict(list)
        foreign: dict[tuple[str, str], tuple[TableName, list[ColumnName], list[ColumnName]]] = {}
        cursor.execute(_KEYS, (database,))
        for table, constraint, column_name, schema, target, target_column in cursor.fetchall():
            if target is None:
                primary[_text(table)].append(ColumnName(_text(column_name)))
                continue
            key = (_text(table), _text(constraint))
            entry = foreign.setdefault(key, (TableName.of(_text(schema), _text(target)), [], []))
            entry[1].append(ColumnName(_text(column_name)))
            entry[2].append(ColumnName(_text(target_column)))
    catalog = Catalog(
        engine=ENGINE,
        tables=tuple(
            Table(
                name=TableName.of(database, name),
                columns=tuple(columns[name]),
                primary_key=tuple(primary.get(name, [])),
                foreign_keys=tuple(
                    ForeignKey(
                        name=constraint,
                        columns=tuple(own),
                        target=target,
                        target_columns=tuple(theirs),
                    )
                    for (table, constraint), (target, own, theirs) in sorted(foreign.items())
                    if table == name
                ),
            )
            for name in names
        ),
    )
    return catalog, Physical(generated={t: frozenset(c) for t, c in generated.items()})
