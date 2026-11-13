# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Scripting a database's schema from its catalog views.

Microsoft ships no schema tool in its server image, so the adapter writes the
schema itself, as statements tagged with the phase they run in and the objects
they need:

1. ``schema``: schemas and sequences;
2. ``table``: tables with their columns, defaults, identity, primary keys and
   unique constraints, before rows are loaded;
3. ``index``: other indexes, and check constraints, after rows are loaded;
4. ``foreign_key``: foreign keys, checked as the source's were;
5. ``module``: views, functions, procedures and synonyms, in dependency order,
   each with the ``ANSI_NULLS`` and ``QUOTED_IDENTIFIER`` settings it was made with;
6. ``trigger``: triggers, last, so loading never fires them.

Loading runs a statement only when its table and everything it needs exist,
so a view over an excluded table is left out rather than failing. Module
definitions are the source's own text; procedures and triggers resolve names
when they run, so they need only their table. Logins, users, permissions and
extended properties are not copied.
"""

from collections import defaultdict
from enum import StrEnum

from pydantic import BaseModel, ConfigDict
from pymssql import Connection

from datadouble.names import TableName
from datadouble_mssql.introspect import type_text
from datadouble_mssql.render import compose, qualified, quote

_CHARACTER = frozenset({"char", "varchar", "nchar", "nvarchar", "text", "ntext"})


class Phase(StrEnum):
    """When a statement runs, relative to loading rows."""

    SCHEMA = "schema"
    TABLE = "table"
    INDEX = "index"
    FOREIGN_KEY = "foreign_key"
    MODULE = "module"
    TRIGGER = "trigger"


class Statement(BaseModel):
    """One batch of the schema."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    phase: Phase
    name: str
    """The object it creates, for messages and for statements that need it."""
    sql: str
    table: TableName | None = None
    """The table it belongs to; it runs only if the table exists."""
    needs: tuple[str, ...] = ()
    """Tables and modules (by name) that must exist for it to run."""
    schema_name: str = "dbo"
    """The schema unqualified names in a module or trigger resolve to."""
    ansi_nulls: bool = True
    quoted_identifier: bool = True


class Schema(BaseModel):
    """A database's schema and the settings a copy should share."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    collation: str
    compatibility_level: int
    read_committed_snapshot: bool
    statements: tuple[Statement, ...]

    def phase(self, phase: Phase) -> tuple[Statement, ...]:
        """The statements of one phase, in order."""
        return tuple(s for s in self.statements if s.phase is phase)


_DATABASE = """
SELECT collation_name, compatibility_level, CAST(is_read_committed_snapshot_on AS INT)
FROM sys.databases WHERE database_id = DB_ID()
"""

# Schemas 1 to 4 are dbo, guest, INFORMATION_SCHEMA and sys; 16384 and above
# belong to the fixed database roles.
_SCHEMAS = """
SELECT name FROM sys.schemas WHERE schema_id BETWEEN 5 AND 16383 ORDER BY name
"""

# A copy's sequence starts where the source's would go next.
_SEQUENCES = """
SELECT s.name, q.name, TYPE_NAME(q.system_type_id),
       CAST(CASE
           WHEN q.last_used_value IS NULL THEN CAST(q.start_value AS DECIMAL(38, 0))
           WHEN CAST(q.last_used_value AS DECIMAL(38, 0)) + CAST(q.increment AS DECIMAL(38, 0))
                NOT BETWEEN CAST(q.minimum_value AS DECIMAL(38, 0))
                        AND CAST(q.maximum_value AS DECIMAL(38, 0))
               THEN CAST(q.last_used_value AS DECIMAL(38, 0))
           ELSE CAST(q.last_used_value AS DECIMAL(38, 0)) + CAST(q.increment AS DECIMAL(38, 0))
       END AS NVARCHAR(40)),
       CAST(q.increment AS NVARCHAR(40)), CAST(q.minimum_value AS NVARCHAR(40)),
       CAST(q.maximum_value AS NVARCHAR(40)), CAST(q.is_cycling AS INT),
       CAST(q.cache_size AS NVARCHAR(40)), CAST(q.is_cached AS INT)
FROM sys.sequences q JOIN sys.schemas s ON s.schema_id = q.schema_id
ORDER BY s.name, q.name
"""

_COLUMNS = """
SELECT s.name, t.name, c.name, ty.name, c.max_length, c.precision, c.scale,
       CAST(c.is_nullable AS INT), c.collation_name, cc.definition, CAST(cc.is_persisted AS INT),
       CAST(ic.seed_value AS NVARCHAR(40)), CAST(ic.increment_value AS NVARCHAR(40)),
       d.name, d.definition, CAST(c.is_sparse AS INT), CAST(c.is_rowguidcol AS INT)
FROM sys.columns c
JOIN sys.tables t ON t.object_id = c.object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.types ty ON ty.user_type_id = c.user_type_id
LEFT JOIN sys.computed_columns cc ON cc.object_id = c.object_id AND cc.column_id = c.column_id
LEFT JOIN sys.identity_columns ic ON ic.object_id = c.object_id AND ic.column_id = c.column_id
LEFT JOIN sys.default_constraints d ON d.parent_object_id = c.object_id
    AND d.parent_column_id = c.column_id
WHERE t.is_ms_shipped = 0
ORDER BY s.name, t.name, c.column_id
"""

_INDEXES = """
SELECT s.name, t.name, i.name, CAST(i.type AS INT), CAST(i.is_unique AS INT),
       CAST(i.is_primary_key AS INT), CAST(i.is_unique_constraint AS INT), i.filter_definition,
       CAST(i.ignore_dup_key AS INT), CAST(i.is_disabled AS INT), c.name,
       CAST(ic.is_descending_key AS INT), CAST(ic.is_included_column AS INT)
FROM sys.indexes i
JOIN sys.tables t ON t.object_id = i.object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE t.is_ms_shipped = 0 AND i.type IN (1, 2)
ORDER BY s.name, t.name, i.index_id, ic.is_included_column, ic.key_ordinal, ic.index_column_id
"""

_CHECKS = """
SELECT s.name, t.name, k.name, k.definition, CAST(k.is_disabled AS INT),
       CAST(k.is_not_trusted AS INT), CAST(k.is_not_for_replication AS INT)
FROM sys.check_constraints k
JOIN sys.tables t ON t.object_id = k.parent_object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE t.is_ms_shipped = 0
ORDER BY s.name, t.name, k.name
"""

_FOREIGN_KEYS = """
SELECT s.name, t.name, f.name, c.name, rs.name, rt.name, rc.name,
       f.delete_referential_action_desc, f.update_referential_action_desc,
       CAST(f.is_disabled AS INT), CAST(f.is_not_trusted AS INT),
       CAST(f.is_not_for_replication AS INT)
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

_MODULES = """
SELECT o.object_id, s.name, o.name, RTRIM(o.type), m.definition,
       CAST(m.uses_ansi_nulls AS INT), CAST(m.uses_quoted_identifier AS INT),
       ps.name, p.name
FROM sys.sql_modules m
JOIN sys.objects o ON o.object_id = m.object_id
JOIN sys.schemas s ON s.schema_id = o.schema_id
LEFT JOIN sys.objects p ON p.object_id = o.parent_object_id AND o.type = 'TR'
LEFT JOIN sys.schemas ps ON ps.schema_id = p.schema_id
WHERE o.is_ms_shipped = 0 AND o.type IN ('V', 'P', 'FN', 'IF', 'TF', 'TR')
ORDER BY o.object_id
"""

_SYNONYMS = """
SELECT s.name, y.name, y.base_object_name
FROM sys.synonyms y JOIN sys.schemas s ON s.schema_id = y.schema_id
ORDER BY s.name, y.name
"""

_DEPENDENCIES = """
SELECT DISTINCT d.referencing_id, rs.name, r.name
FROM sys.sql_expression_dependencies d
JOIN sys.objects r ON r.object_id = d.referenced_id
JOIN sys.schemas rs ON rs.schema_id = r.schema_id
WHERE d.referenced_id IS NOT NULL AND d.referencing_id <> d.referenced_id
"""


def _rows(connection: Connection, query: str) -> list[tuple[object, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(query)
        return [tuple(row) for row in cursor.fetchall()]


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _flag(value: object) -> bool:
    return bool(int(str(value))) if value is not None else False


def object_name(schema: str, name: str) -> str:
    """The name statements use to refer to an object: ``schema.name``."""
    return f"{schema}.{name}"


def _sequences(connection: Connection) -> list[Statement]:
    statements: list[Statement] = []
    for row in _rows(connection, _SEQUENCES):
        schema, name, kind, start, increment, minimum, maximum = (_text(v) for v in row[:7])
        cycling, cache, cached = _flag(row[7]), row[8], _flag(row[9])
        caching = "NO CACHE" if not cached else (f"CACHE {cache}" if cache else "CACHE")
        statement = compose(
            "CREATE SEQUENCE {}.{} AS {} START WITH {} INCREMENT BY {}"
            " MINVALUE {} MAXVALUE {} {} {}",
            quote(schema),
            quote(name),
            kind,
            start,
            increment,
            minimum,
            maximum,
            "CYCLE" if cycling else "NO CYCLE",
            caching,
        )
        statements.append(
            Statement(phase=Phase.SCHEMA, name=object_name(schema, name), sql=statement)
        )
    return statements


def _column(row: tuple[object, ...]) -> str:
    name, type_name = _text(row[2]), _text(row[3])
    max_length, precision, scale = (int(str(v)) for v in row[4:7])
    nullable, collation = _flag(row[7]), row[8]
    computed, persisted = row[9], _flag(row[10])
    seed, increment, default_name, default = row[11], row[12], row[13], row[14]
    sparse, rowguid = _flag(row[15]), _flag(row[16])
    if computed is not None:
        return compose(
            "{} AS {}{}", quote(name), _text(computed), " PERSISTED" if persisted else ""
        )
    parts = [quote(name), type_text(type_name, max_length, precision, scale)]
    if collation is not None and type_name in _CHARACTER:
        parts.append(f"COLLATE {collation}")
    if sparse:
        parts.append("SPARSE")
    if seed is not None:
        parts.append(f"IDENTITY({seed}, {increment})")
    if rowguid:
        parts.append("ROWGUIDCOL")
    parts.append("NULL" if nullable else "NOT NULL")
    if default is not None:
        parts.append(
            compose("CONSTRAINT {} DEFAULT {}", quote(_text(default_name)), _text(default))
        )
    return " ".join(parts)


def _key_list(keys: list[tuple[str, bool]]) -> str:
    return ", ".join(quote(name) + (" DESC" if descending else " ASC") for name, descending in keys)


type _Index = tuple[tuple[object, ...], list[tuple[str, bool]], list[str]]


def _indexes(connection: Connection) -> dict[tuple[str, str, str], _Index]:
    """Each rowstore index: its row, key columns (with direction) and included columns."""
    indexes: dict[tuple[str, str, str], _Index] = {}
    for row in _rows(connection, _INDEXES):
        entry = indexes.setdefault((_text(row[0]), _text(row[1]), _text(row[2])), (row, [], []))
        if _flag(row[12]):
            entry[2].append(_text(row[10]))
        else:
            entry[1].append((_text(row[10]), _flag(row[11])))
    return indexes


def _index(target: TableName, name: str, index: _Index) -> str:
    """``CREATE INDEX`` for an index that is not a constraint."""
    row, keys, included = index
    statement = compose(
        "CREATE {}{} INDEX {} ON {} ({})",
        "UNIQUE " if _flag(row[4]) else "",
        "CLUSTERED" if int(str(row[3])) == 1 else "NONCLUSTERED",
        quote(name),
        qualified(target),
        _key_list(keys),
    )
    if included:
        statement += compose(" INCLUDE ({})", ", ".join(quote(c) for c in included))
    if row[7] is not None:
        statement += compose(" WHERE {}", _text(row[7]))
    if _flag(row[8]):
        statement += " WITH (IGNORE_DUP_KEY = ON)"
    if _flag(row[9]):
        statement += compose("; ALTER INDEX {} ON {} DISABLE", quote(name), qualified(target))
    return statement


def _checks(connection: Connection) -> list[Statement]:
    """Check constraints, trusted or not as the source's are."""
    statements: list[Statement] = []
    for row in _rows(connection, _CHECKS):
        schema, table, name, definition = (_text(v) for v in row[:4])
        disabled, untrusted, replication = (_flag(v) for v in row[4:7])
        target = TableName.of(schema, table)
        statement = compose(
            "ALTER TABLE {} WITH {} ADD CONSTRAINT {} CHECK {}{}",
            qualified(target),
            "NOCHECK" if untrusted or disabled else "CHECK",
            quote(name),
            " NOT FOR REPLICATION" if replication else "",
            definition,
        )
        if disabled:
            statement += compose(
                "; ALTER TABLE {} NOCHECK CONSTRAINT {}", qualified(target), quote(name)
            )
        statements.append(
            Statement(
                phase=Phase.INDEX, name=f"{schema}.{table}.{name}", sql=statement, table=target
            )
        )
    return statements


def _tables(connection: Connection) -> tuple[list[Statement], list[Statement]]:
    """``CREATE TABLE`` statements, and the index statements that follow loading."""
    columns: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    for row in _rows(connection, _COLUMNS):
        columns[(_text(row[0]), _text(row[1]))].append(_column(row))
    constraints: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    after: list[Statement] = []
    for (schema, table, name), index in _indexes(connection).items():
        row, keys, _ = index
        target = TableName.of(schema, table)
        if _flag(row[5]) or _flag(row[6]):
            constraints[(schema, table)].append(
                compose(
                    "CONSTRAINT {} {} {} ({})",
                    quote(name),
                    "PRIMARY KEY" if _flag(row[5]) else "UNIQUE",
                    "CLUSTERED" if int(str(row[3])) == 1 else "NONCLUSTERED",
                    _key_list(keys),
                )
            )
            continue
        after.append(
            Statement(
                phase=Phase.INDEX,
                name=f"{schema}.{table}.{name}",
                sql=_index(target, name, index),
                table=target,
            )
        )
    tables = [
        Statement(
            phase=Phase.TABLE,
            name=object_name(schema, table),
            sql=compose(
                "CREATE TABLE {} ({})",
                qualified(TableName.of(schema, table)),
                ", ".join([*definitions, *constraints[(schema, table)]]),
            ),
            table=TableName.of(schema, table),
        )
        for (schema, table), definitions in columns.items()
    ]
    return tables, [*after, *_checks(connection)]


def _foreign_keys(connection: Connection) -> list[Statement]:
    keys: dict[tuple[str, str, str], tuple[tuple[object, ...], list[str], list[str]]] = {}
    for row in _rows(connection, _FOREIGN_KEYS):
        entry = keys.setdefault((_text(row[0]), _text(row[1]), _text(row[2])), (row, [], []))
        entry[1].append(_text(row[3]))
        entry[2].append(_text(row[6]))
    statements: list[Statement] = []
    for (schema, table, name), (row, own, theirs) in keys.items():
        child = TableName.of(schema, table)
        parent = TableName.of(_text(row[4]), _text(row[5]))
        disabled, untrusted, replication = (_flag(v) for v in row[9:12])
        statement = compose(
            "ALTER TABLE {} WITH {} ADD CONSTRAINT {} FOREIGN KEY ({}) REFERENCES {} ({})"
            " ON DELETE {} ON UPDATE {}{}",
            qualified(child),
            "NOCHECK" if untrusted or disabled else "CHECK",
            quote(name),
            ", ".join(quote(c) for c in own),
            qualified(parent),
            ", ".join(quote(c) for c in theirs),
            _text(row[7]).replace("_", " "),
            _text(row[8]).replace("_", " "),
            " NOT FOR REPLICATION" if replication else "",
        )
        if disabled:
            statement += compose(
                "; ALTER TABLE {} NOCHECK CONSTRAINT {}", qualified(child), quote(name)
            )
        statements.append(
            Statement(
                phase=Phase.FOREIGN_KEY,
                name=f"{schema}.{table}.{name}",
                sql=statement,
                table=child,
                needs=(object_name(parent.namespace, parent.name),),
            )
        )
    return statements


def _modules(connection: Connection) -> list[Statement]:
    """Views, functions, procedures and triggers, each after what it reads."""
    needs: defaultdict[int, set[str]] = defaultdict(set)
    for referencing, schema, name in _rows(connection, _DEPENDENCIES):
        needs[int(str(referencing))].add(object_name(_text(schema), _text(name)))
    modules = _rows(connection, _MODULES)
    names = {int(str(row[0])): object_name(_text(row[1]), _text(row[2])) for row in modules}
    pending = {int(str(row[0])): row for row in modules}
    ordered: list[tuple[object, ...]] = []
    while pending:
        waiting = {names[k] for k in pending}
        # A cycle (possible through procedures) is broken at its oldest object.
        ready = [
            key for key in sorted(pending) if not (needs[key] - {names[key]}) & waiting
        ] or sorted(pending)[:1]
        ordered.extend(pending.pop(key) for key in ready)
    statements: list[Statement] = []
    for row in ordered:
        key, schema, name, kind = int(str(row[0])), _text(row[1]), _text(row[2]), _text(row[3])
        definition = _text(row[4])
        if kind == "TR":
            parent = TableName.of(_text(row[7]), _text(row[8]))
            statements.append(
                Statement(
                    phase=Phase.TRIGGER,
                    name=object_name(schema, name),
                    sql=definition,
                    table=parent,
                    schema_name=schema,
                    ansi_nulls=_flag(row[5]),
                    quoted_identifier=_flag(row[6]),
                )
            )
            continue
        # Procedures resolve names when they run; views and functions when
        # they are created, so those need what they read.
        required = () if kind == "P" else tuple(sorted(needs[key] - {names[key]}))
        statements.append(
            Statement(
                phase=Phase.MODULE,
                name=object_name(schema, name),
                sql=definition,
                needs=required,
                schema_name=schema,
                ansi_nulls=_flag(row[5]),
                quoted_identifier=_flag(row[6]),
            )
        )
    for schema, name, base in _rows(connection, _SYNONYMS):
        statements.append(
            Statement(
                phase=Phase.MODULE,
                name=object_name(_text(schema), _text(name)),
                sql=compose(
                    "CREATE SYNONYM {}.{} FOR {}",
                    quote(_text(schema)),
                    quote(_text(name)),
                    _text(base),
                ),
            )
        )
    return statements


def script(connection: Connection) -> Schema:
    """The schema of the connection's database."""
    database = _rows(connection, _DATABASE)[0]
    schemas = [
        Statement(
            phase=Phase.SCHEMA,
            name=_text(name),
            sql=compose("CREATE SCHEMA {}", quote(_text(name))),
        )
        for (name,) in _rows(connection, _SCHEMAS)
        if _text(name) != "dbo"
    ]
    tables, after = _tables(connection)
    return Schema(
        collation=_text(database[0]),
        compatibility_level=int(str(database[1])),
        read_committed_snapshot=_flag(database[2]),
        statements=(
            *schemas,
            *_sequences(connection),
            *tables,
            *after,
            *_foreign_keys(connection),
            *_modules(connection),
        ),
    )
