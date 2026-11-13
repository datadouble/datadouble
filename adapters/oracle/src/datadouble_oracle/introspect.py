# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Reading an Oracle catalog into Datadouble's engine-neutral model.

The catalog is one or more schemas (the connection's ``schemas``, by default
the connecting user's own): their relational tables, columns, primary keys
and foreign keys. Table names are ``OWNER.TABLE``, in Oracle's own case.
Beyond the neutral model, the adapter keeps each column's type family (for
reading it as text and loading it back), the columns loading cannot write
(virtual columns), the identity columns loading must write anyway, and what
it does not support, which it refuses to extract rather than copy wrongly.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import NamedTuple

from datadouble.catalog import Catalog, Column, ColumnKind, ForeignKey, Table
from datadouble.names import ColumnName, EngineName, TableName
from datadouble_oracle.driver import Connection, Value

ENGINE = EngineName("oracle")

_TABLES = """
SELECT t.owner, t.table_name,
       CASE WHEN t.iot_type = 'IOT_OVERFLOW' OR t.nested = 'YES' OR t.secondary = 'Y'
            THEN 1 ELSE 0 END,
       CASE WHEN EXISTS (SELECT 1 FROM all_external_tables e
                         WHERE e.owner = t.owner AND e.table_name = t.table_name)
            THEN 1 ELSE 0 END,
       CASE WHEN t.partitioned = 'YES' THEN 1 ELSE 0 END,
       CASE WHEN EXISTS (SELECT 1 FROM all_mviews m
                         WHERE m.owner = t.owner AND m.container_name = t.table_name)
            THEN 1 ELSE 0 END
FROM all_tables t
WHERE t.owner IN ({}) AND t.temporary = 'N' AND t.dropped = 'NO'
ORDER BY t.owner, t.table_name
"""

_COLUMNS = """
SELECT c.owner, c.table_name, c.column_name, c.data_type, c.data_type_owner,
       c.data_length, c.data_precision, c.data_scale, c.char_length, c.char_used,
       c.nullable, c.virtual_column, c.identity_column
FROM all_tab_cols c
JOIN all_tables t ON t.owner = c.owner AND t.table_name = c.table_name
WHERE c.owner IN ({}) AND c.hidden_column = 'NO'
ORDER BY c.owner, c.table_name, c.column_id
"""

_PRIMARY_KEYS = """
SELECT k.owner, k.table_name, c.column_name
FROM all_constraints k
JOIN all_cons_columns c ON c.owner = k.owner AND c.constraint_name = k.constraint_name
WHERE k.owner IN ({}) AND k.constraint_type = 'P'
ORDER BY k.owner, k.table_name, c.position
"""

_FOREIGN_KEYS = """
SELECT k.owner, k.table_name, k.constraint_name, c.column_name,
       r.owner, r.table_name, rc.column_name
FROM all_constraints k
JOIN all_cons_columns c ON c.owner = k.owner AND c.constraint_name = k.constraint_name
JOIN all_constraints r ON r.owner = k.r_owner AND r.constraint_name = k.r_constraint_name
JOIN all_cons_columns rc ON rc.owner = r.owner AND rc.constraint_name = r.constraint_name
    AND rc.position = c.position
WHERE k.owner IN ({}) AND k.constraint_type = 'R'
ORDER BY k.owner, k.table_name, k.constraint_name, c.position
"""

_TIMESTAMP = re.compile(r"TIMESTAMP\(\d\)")
_INTERVAL = re.compile(r"INTERVAL (DAY|YEAR)\(\d\) TO (SECOND|MONTH)(\(\d\))?")

FAMILIES = frozenset(
    {
        "NUMBER",
        "FLOAT",
        "BINARY_FLOAT",
        "BINARY_DOUBLE",
        "VARCHAR2",
        "NVARCHAR2",
        "CHAR",
        "NCHAR",
        "CLOB",
        "NCLOB",
        "DATE",
        "TIMESTAMP",
        "TIMESTAMP WITH TIME ZONE",
        "TIMESTAMP WITH LOCAL TIME ZONE",
        "INTERVAL DAY TO SECOND",
        "INTERVAL YEAR TO MONTH",
        "RAW",
        "BLOB",
        "JSON",
    }
)
"""Type families the adapter reads as text and loads back exactly."""


def family(data_type: str) -> str:
    """A type without its precision.

    ``TIMESTAMP(6) WITH TIME ZONE`` is ``TIMESTAMP WITH TIME ZONE``.
    """
    without = _TIMESTAMP.sub("TIMESTAMP", data_type)
    match = _INTERVAL.fullmatch(without)
    return f"INTERVAL {match.group(1)} TO {match.group(2)}" if match else without


_KINDS = {
    "FLOAT": ColumnKind.FLOAT,
    "BINARY_FLOAT": ColumnKind.FLOAT,
    "BINARY_DOUBLE": ColumnKind.FLOAT,
    "VARCHAR2": ColumnKind.TEXT,
    "NVARCHAR2": ColumnKind.TEXT,
    "CHAR": ColumnKind.TEXT,
    "NCHAR": ColumnKind.TEXT,
    "CLOB": ColumnKind.TEXT,
    "NCLOB": ColumnKind.TEXT,
    "DATE": ColumnKind.TIMESTAMP,
    "TIMESTAMP": ColumnKind.TIMESTAMP,
    "TIMESTAMP WITH TIME ZONE": ColumnKind.TIMESTAMP,
    "TIMESTAMP WITH LOCAL TIME ZONE": ColumnKind.TIMESTAMP,
    "RAW": ColumnKind.BINARY,
    "BLOB": ColumnKind.BINARY,
    "JSON": ColumnKind.JSON,
}


def kind_of(data_type: str, scale: int | None) -> ColumnKind:
    """The engine-neutral kind of an Oracle type.

    Oracle's ``DATE`` holds a time of day, so it is a timestamp; intervals are
    ``other``, since a generated value would not fit them.
    """
    kind = family(data_type)
    if kind == "NUMBER":
        return ColumnKind.INTEGER if scale == 0 else ColumnKind.DECIMAL
    return _KINDS.get(kind, ColumnKind.OTHER)


class Declared(NamedTuple):
    """A column's type as the dictionary describes it."""

    data_type: str
    length: int
    precision: int | None
    scale: int | None
    chars: int
    unit: str
    """``C`` for character length semantics, ``B`` for bytes."""


def type_text(declared: Declared) -> str:
    """The type as Oracle writes it, such as ``VARCHAR2(45 CHAR)`` or ``NUMBER(5,2)``."""
    data_type = declared.data_type
    if data_type == "NUMBER" and declared.precision is not None:
        return f"NUMBER({declared.precision},{declared.scale or 0})"
    if data_type == "NUMBER" and declared.scale == 0:
        return "NUMBER(*,0)"
    if data_type in {"VARCHAR2", "CHAR"}:
        return f"{data_type}({declared.chars} {'CHAR' if declared.unit == 'C' else 'BYTE'})"
    if data_type in {"NVARCHAR2", "NCHAR"}:
        return f"{data_type}({declared.chars})"
    return f"RAW({declared.length})" if data_type == "RAW" else data_type


@dataclass(frozen=True, slots=True)
class Physical:
    """What the adapter needs beyond the engine-neutral catalog."""

    types: dict[TableName, dict[ColumnName, str]] = field(
        default_factory=dict[TableName, dict[ColumnName, str]]
    )
    """Each column's type family, such as ``TIMESTAMP WITH TIME ZONE``."""
    virtual: dict[TableName, frozenset[ColumnName]] = field(
        default_factory=dict[TableName, frozenset[ColumnName]]
    )
    """Virtual columns, which the server computes."""
    identity: dict[TableName, ColumnName] = field(default_factory=dict[TableName, ColumnName])
    """Each table's identity column, which loading writes as extracted."""
    unsupported: dict[TableName, str] = field(default_factory=dict[TableName, str])
    """Tables this adapter cannot copy, and why."""

    def loaded(self, table: TableName) -> tuple[ColumnName, ...]:
        """The columns extract writes and load reads, in table order."""
        skipped = self.virtual.get(table, frozenset())
        return tuple(c for c in self.types.get(table, {}) if c not in skipped)


def table_problem(flags: tuple[object, ...]) -> str | None:
    """Why a table with these flags (from ``_TABLES``) cannot be copied, if it cannot."""
    names = (
        "is a nested table, an overflow segment or a secondary object",
        "is an external table",
        "is a partitioned table",
        "holds a materialized view",
    )
    found = [name for name, flag in zip(names, flags, strict=True) if int(str(flag))]
    return f"{found[0]}, which the Oracle adapter does not support yet" if found else None


def binds(count: int) -> str:
    """``:s0, :s1, ...`` for ``count`` schema names."""
    return ", ".join(f":s{i}" for i in range(count))


def introspect(connection: Connection, schemas: tuple[str, ...]) -> tuple[Catalog, Physical]:
    """Read the catalog of ``schemas``."""
    names = binds(len(schemas))
    params: dict[str, Value] = {f"s{i}": schema for i, schema in enumerate(schemas)}
    with connection.cursor() as cursor:
        cursor.execute(_TABLES.format(names), params)
        tables: list[TableName] = []
        unsupported: dict[TableName, str] = {}
        for owner, table, *flags in cursor.fetchall():
            name = TableName.of(str(owner), str(table))
            tables.append(name)
            if problem := table_problem(tuple(flags)):
                unsupported[name] = problem
        columns: defaultdict[TableName, list[Column]] = defaultdict(list)
        types: defaultdict[TableName, dict[ColumnName, str]] = defaultdict(dict)
        virtual: defaultdict[TableName, set[ColumnName]] = defaultdict(set)
        identity: dict[TableName, ColumnName] = {}
        cursor.execute(_COLUMNS.format(names), params)
        for row in cursor.fetchall():
            owner, table, column, data_type = (str(v) for v in row[:4])
            type_owner = row[4]
            length, chars = int(str(row[5])), int(str(row[8] or 0))
            precision = None if row[6] is None else int(str(row[6]))
            scale = None if row[7] is None else int(str(row[7]))
            name, column_name = TableName.of(owner, table), ColumnName(column)
            columns[name].append(
                Column(
                    name=column_name,
                    type=type_text(
                        Declared(data_type, length, precision, scale, chars, str(row[9]))
                    ),
                    kind=kind_of(data_type, scale),
                    nullable=row[10] == "Y",
                )
            )
            types[name][column_name] = family(data_type)
            if row[11] == "YES":
                virtual[name].add(column_name)
            elif type_owner is not None or family(data_type) not in FAMILIES:
                unsupported.setdefault(
                    name, f"has column {column} of type {data_type}, which is not supported yet"
                )
            if row[12] == "YES":
                identity[name] = column_name
        primary: defaultdict[TableName, list[ColumnName]] = defaultdict(list)
        cursor.execute(_PRIMARY_KEYS.format(names), params)
        for key in cursor.fetchall():
            primary[TableName.of(str(key[0]), str(key[1]))].append(ColumnName(str(key[2])))
        foreign: dict[tuple[TableName, str], tuple[TableName, list[ColumnName], list[ColumnName]]]
        foreign = {}
        cursor.execute(_FOREIGN_KEYS.format(names), params)
        for row in cursor.fetchall():
            text = [str(value) for value in row]
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
            for name in tables
        ),
    )
    physical = Physical(
        types=dict(types),
        virtual={t: frozenset(c) for t, c in virtual.items()},
        identity=identity,
        unsupported=unsupported,
    )
    return catalog, physical
