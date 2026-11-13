# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Reading a SQL Server catalog into Datadouble's engine-neutral model.

The catalog is one database: the base tables of every user schema, their
columns, primary keys and foreign keys. Table names are ``schema.table``.
Beyond the neutral model, the adapter keeps each column's system type (for
reading it as text and loading it back), the columns loading cannot write
(computed columns and ``rowversion``), and what it does not support, which it
refuses to extract rather than copy wrongly.
"""

from collections import defaultdict
from dataclasses import dataclass, field

from pymssql import Connection

from datadouble.catalog import Catalog, Column, ColumnKind, ForeignKey, Table
from datadouble.names import ColumnName, EngineName, TableName

ENGINE = EngineName("mssql")

_TABLES = """
SELECT s.name, t.name, t.is_memory_optimized, t.temporal_type, t.is_external, t.is_filetable,
       t.is_node | t.is_edge,
       CASE WHEN EXISTS (
           SELECT 1 FROM sys.indexes i JOIN sys.data_spaces d ON d.data_space_id = i.data_space_id
           WHERE i.object_id = t.object_id AND d.type = 'PS') THEN 1 ELSE 0 END,
       CASE WHEN EXISTS (
           SELECT 1 FROM sys.columns c WHERE c.object_id = t.object_id AND c.is_filestream = 1)
           THEN 1 ELSE 0 END,
       CASE WHEN EXISTS (
           SELECT 1 FROM sys.indexes i WHERE i.object_id = t.object_id AND i.type NOT IN (0, 1, 2))
           OR EXISTS (SELECT 1 FROM sys.fulltext_indexes f WHERE f.object_id = t.object_id)
           THEN 1 ELSE 0 END
FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE t.is_ms_shipped = 0
ORDER BY s.name, t.name
"""

_COLUMNS = """
SELECT s.name, t.name, c.name,
       CASE WHEN ty.is_assembly_type = 1 OR ty.system_type_id = ty.user_type_id THEN ty.name
            ELSE TYPE_NAME(ty.system_type_id) END,
       ty.name, CAST(ty.is_user_defined AS INT),
       c.max_length, c.precision, c.scale, CAST(c.is_nullable AS INT), CAST(c.is_computed AS INT)
FROM sys.columns c
JOIN sys.tables t ON t.object_id = c.object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.types ty ON ty.user_type_id = c.user_type_id
WHERE t.is_ms_shipped = 0
ORDER BY s.name, t.name, c.column_id
"""

_PRIMARY_KEYS = """
SELECT s.name, t.name, c.name
FROM sys.key_constraints k
JOIN sys.tables t ON t.object_id = k.parent_object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.index_columns ic ON ic.object_id = k.parent_object_id AND ic.index_id = k.unique_index_id
JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE k.type = 'PK' AND t.is_ms_shipped = 0
ORDER BY s.name, t.name, ic.key_ordinal
"""

_FOREIGN_KEYS = """
SELECT s.name, t.name, f.name, c.name, rs.name, rt.name, rc.name
FROM sys.foreign_keys f
JOIN sys.foreign_key_columns fc ON fc.constraint_object_id = f.object_id
JOIN sys.tables t ON t.object_id = f.parent_object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.columns c ON c.object_id = fc.parent_object_id AND c.column_id = fc.parent_column_id
JOIN sys.tables rt ON rt.object_id = f.referenced_object_id
JOIN sys.schemas rs ON rs.schema_id = rt.schema_id
JOIN sys.columns rc
    ON rc.object_id = fc.referenced_object_id AND rc.column_id = fc.referenced_column_id
WHERE t.is_ms_shipped = 0
ORDER BY s.name, t.name, f.name, fc.constraint_column_id
"""

_KINDS = {
    "tinyint": ColumnKind.INTEGER,
    "smallint": ColumnKind.INTEGER,
    "int": ColumnKind.INTEGER,
    "bigint": ColumnKind.INTEGER,
    "bit": ColumnKind.BOOLEAN,
    "decimal": ColumnKind.DECIMAL,
    "numeric": ColumnKind.DECIMAL,
    "money": ColumnKind.DECIMAL,
    "smallmoney": ColumnKind.DECIMAL,
    "float": ColumnKind.FLOAT,
    "real": ColumnKind.FLOAT,
    "date": ColumnKind.DATE,
    "datetime": ColumnKind.TIMESTAMP,
    "datetime2": ColumnKind.TIMESTAMP,
    "smalldatetime": ColumnKind.TIMESTAMP,
    "datetimeoffset": ColumnKind.TIMESTAMP,
    "time": ColumnKind.TIME,
    "char": ColumnKind.TEXT,
    "varchar": ColumnKind.TEXT,
    "nchar": ColumnKind.TEXT,
    "nvarchar": ColumnKind.TEXT,
    "text": ColumnKind.TEXT,
    "ntext": ColumnKind.TEXT,
    "binary": ColumnKind.BINARY,
    "varbinary": ColumnKind.BINARY,
    "image": ColumnKind.BINARY,
    "uniqueidentifier": ColumnKind.UUID,
    "json": ColumnKind.JSON,
}

SUPPORTED_TYPES = frozenset({*_KINDS, "xml", "geometry", "geography", "hierarchyid", "timestamp"})
"""System types the adapter can read as text and load back exactly."""

_LENGTHS = frozenset({"char", "varchar", "binary", "varbinary"})
_WIDE = frozenset({"nchar", "nvarchar"})
_PRECISION = frozenset({"decimal", "numeric"})
_SCALE = frozenset({"datetime2", "time", "datetimeoffset"})


def kind_of(system_type: str) -> ColumnKind:
    """The engine-neutral kind of a SQL Server system type.

    ``xml``, ``sql_variant``, the spatial types, ``hierarchyid`` and
    ``rowversion`` are ``other``, since a generated value would not fit them.
    """
    return _KINDS.get(system_type.lower(), ColumnKind.OTHER)


def type_text(system_type: str, max_length: int, precision: int, scale: int) -> str:
    """The type as T-SQL writes it, such as ``nvarchar(50)`` or ``decimal(5,2)``."""
    if system_type in _LENGTHS | _WIDE:
        if max_length == -1:
            return f"{system_type}(max)"
        return f"{system_type}({max_length // 2 if system_type in _WIDE else max_length})"
    if system_type in _PRECISION:
        return f"{system_type}({precision},{scale})"
    if system_type in _SCALE:
        return f"{system_type}({scale})"
    return "rowversion" if system_type == "timestamp" else system_type


@dataclass(frozen=True, slots=True)
class Physical:
    """What the adapter needs beyond the engine-neutral catalog."""

    types: dict[TableName, dict[ColumnName, str]] = field(
        default_factory=dict[TableName, dict[ColumnName, str]]
    )
    """Each column's system type, such as ``nvarchar``."""
    unloadable: dict[TableName, frozenset[ColumnName]] = field(
        default_factory=dict[TableName, frozenset[ColumnName]]
    )
    """Computed and ``rowversion`` columns, which the server fills in itself."""
    unsupported: dict[TableName, str] = field(default_factory=dict[TableName, str])
    """Tables this adapter cannot copy, and why."""

    def loaded(self, table: TableName) -> tuple[ColumnName, ...]:
        """The columns extract writes and load reads, in table order."""
        skipped = self.unloadable.get(table, frozenset())
        return tuple(c for c in self.types.get(table, {}) if c not in skipped)


def table_problem(flags: tuple[object, ...]) -> str | None:
    """Why a table with these flags (from ``_TABLES``) cannot be copied, if it cannot."""
    names = (
        "is a memory-optimized table",
        "is a system-versioned temporal table or its history",
        "is an external table",
        "is a FileTable",
        "is a graph table",
        "is a partitioned table",
        "has a FILESTREAM column",
        "has a columnstore, XML, spatial or full-text index",
    )
    found = [name for name, flag in zip(names, flags, strict=True) if bool(flag)]
    return f"{found[0]}, which the SQL Server adapter does not support yet" if found else None


def introspect(connection: Connection) -> tuple[Catalog, Physical]:
    """Read the catalog of the connection's database."""
    with connection.cursor() as cursor:
        cursor.execute(_TABLES)
        names: list[TableName] = []
        unsupported: dict[TableName, str] = {}
        for schema, table, *flags in cursor.fetchall():
            name = TableName.of(str(schema), str(table))
            names.append(name)
            if problem := table_problem(tuple(flags)):
                unsupported[name] = problem
        columns: defaultdict[TableName, list[Column]] = defaultdict(list)
        types: defaultdict[TableName, dict[ColumnName, str]] = defaultdict(dict)
        unloadable: defaultdict[TableName, set[ColumnName]] = defaultdict(set)
        cursor.execute(_COLUMNS)
        for row in cursor.fetchall():
            schema, table, column, system_type, declared = (str(v) for v in row[:5])
            user_defined, max_length, precision, scale, nullable, computed = (
                int(str(v)) for v in row[5:]
            )
            name = TableName.of(schema, table)
            columns[name].append(
                Column(
                    name=ColumnName(column),
                    type=type_text(system_type, max_length, precision, scale),
                    kind=kind_of(system_type),
                    nullable=bool(nullable),
                )
            )
            types[name][ColumnName(column)] = system_type
            if computed or system_type == "timestamp":
                unloadable[name].add(ColumnName(column))
            elif user_defined or system_type not in SUPPORTED_TYPES:
                # Alias and CLR types defined in the database, and system
                # types without an exact text form (sql_variant).
                unsupported.setdefault(
                    name, f"has column {column} of type {declared}, which is not supported yet"
                )
        primary: defaultdict[TableName, list[ColumnName]] = defaultdict(list)
        cursor.execute(_PRIMARY_KEYS)
        for key_row in cursor.fetchall():
            owner = TableName.of(str(key_row[0]), str(key_row[1]))
            primary[owner].append(ColumnName(str(key_row[2])))
        foreign: dict[tuple[TableName, str], tuple[TableName, list[ColumnName], list[ColumnName]]]
        foreign = {}
        cursor.execute(_FOREIGN_KEYS)
        for key_row in cursor.fetchall():
            text = [str(value) for value in key_row]
            entry = foreign.setdefault(
                (TableName.of(text[0], text[1]), text[2]), (TableName.of(text[4], text[5]), [], [])
            )
            entry[1].append(ColumnName(text[3]))
            entry[2].append(ColumnName(text[6]))
    catalog = Catalog(
        engine=ENGINE,
        tables=tuple(
            Table(
                name=name,
                columns=tuple(columns[name]),
                primary_key=tuple(primary.get(name, [])),
                foreign_keys=tuple(
                    ForeignKey(
                        name=constraint,
                        columns=tuple(own),
                        target=target,
                        target_columns=tuple(theirs),
                    )
                    for (table, constraint), (target, own, theirs) in sorted(
                        foreign.items(), key=lambda item: (str(item[0][0]), item[0][1])
                    )
                    if table == name
                ),
            )
            for name in names
        ),
    )
    physical = Physical(
        types=dict(types),
        unloadable={t: frozenset(c) for t, c in unloadable.items()},
        unsupported=unsupported,
    )
    return catalog, physical
