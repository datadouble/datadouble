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
from datadouble_oracle.driver import Connection, Cursor, Value

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

# Keys come from two single-view queries joined here: joins between the
# constraint views can get plans that run for many minutes, most of all in a
# freshly plugged-in database with no dictionary statistics. The keys foreign
# keys reference come too, in whichever schema they are.
_REFERENCED = """
(owner, constraint_name) IN (
    SELECT r_owner, r_constraint_name FROM all_constraints
    WHERE owner IN ({0}) AND constraint_type = 'R')
"""

_CONSTRAINTS = (
    """
SELECT owner, constraint_name, table_name, constraint_type, r_owner, r_constraint_name,
       delete_rule, status, validated, deferrable, deferred
FROM all_constraints
WHERE (owner IN ({0}) AND constraint_type IN ('P', 'R')) OR """
    + _REFERENCED
)

_KEY_COLUMNS = (
    """
SELECT owner, constraint_name, column_name
FROM all_cons_columns
WHERE position IS NOT NULL AND (owner IN ({0}) OR """
    + _REFERENCED
    + """)
ORDER BY owner, constraint_name, position
"""
)


@dataclass(frozen=True, slots=True)
class Reference:
    """A foreign key, with the settings a script recreates it with."""

    table: TableName
    name: str
    columns: tuple[ColumnName, ...]
    target: TableName
    target_columns: tuple[ColumnName, ...]
    delete_rule: str
    enabled: bool
    validated: bool
    deferrable: bool
    deferred: bool


def keys(
    cursor: Cursor, schemas: tuple[str, ...]
) -> tuple[dict[TableName, tuple[ColumnName, ...]], list[Reference]]:
    """The schemas' primary keys, by table, and foreign keys, in order."""
    names = binds(len(schemas))
    params: dict[str, Value] = {f"s{i}": schema for i, schema in enumerate(schemas)}
    cursor.execute(_CONSTRAINTS.format(names), params)
    rows = {(str(row[0]), str(row[1])): row for row in cursor.fetchall()}
    columns: defaultdict[tuple[str, str], list[ColumnName]] = defaultdict(list)
    cursor.execute(_KEY_COLUMNS.format(names), params)
    for owner, name, column in cursor.fetchall():
        columns[(str(owner), str(name))].append(ColumnName(str(column)))
    own = sorted(key for key, row in rows.items() if key[0] in schemas and row[3] in {"P", "R"})
    primary = {
        TableName.of(key[0], str(rows[key][2])): tuple(columns[key])
        for key in own
        if rows[key][3] == "P"
    }
    references = [_reference(key, rows, columns) for key in own if rows[key][3] == "R"]
    references.sort(key=lambda r: (str(r.table), r.name))
    return primary, references


def _reference(
    key: tuple[str, str],
    rows: dict[tuple[str, str], tuple[object, ...]],
    columns: dict[tuple[str, str], list[ColumnName]],
) -> Reference:
    row = rows[key]
    target = (str(row[4]), str(row[5]))
    return Reference(
        table=TableName.of(key[0], str(row[2])),
        name=key[1],
        columns=tuple(columns[key]),
        target=TableName.of(target[0], str(rows[target][2])),
        target_columns=tuple(columns[target]),
        delete_rule=str(row[6]),
        enabled=row[7] == "ENABLED",
        validated=row[8] == "VALIDATED",
        deferrable=row[9] == "DEFERRABLE",
        deferred=row[10] == "DEFERRED",
    )


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
        primary, references = keys(cursor, schemas)
    catalog = Catalog(
        engine=ENGINE,
        tables=tuple(
            Table(
                name=name,
                columns=tuple(columns[name]),
                primary_key=primary.get(name, ()),
                foreign_keys=tuple(
                    ForeignKey(
                        name=r.name,
                        columns=r.columns,
                        target=r.target,
                        target_columns=r.target_columns,
                    )
                    for r in references
                    if r.table == name
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
