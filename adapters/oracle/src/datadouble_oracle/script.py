# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""A schema's objects as statements, from Oracle's own ``DBMS_METADATA``.

``DBMS_METADATA`` writes each object's DDL, without storage, tablespace or
foreign-key clauses; foreign keys are composed from the dictionary, so each
can wait for its parent. Statements are tagged with the phase they run in and
the objects they need, as the SQL Server adapter's are:

1. ``schema``: object types and sequences;
2. ``table``: tables with their primary, unique and check constraints, before
   rows are loaded;
3. ``index``: other indexes, after rows are loaded;
4. ``foreign_key``: foreign keys, enabled and validated as the source's are;
5. ``module``: views (in dependency order, each after what it reads),
   functions, procedures, packages, type bodies and synonyms;
6. ``trigger``: triggers, last, so loading never fires them.

Loading runs a statement only when its table and everything it needs exist,
so a view over an excluded table is left out rather than failing. PL/SQL
compiles even when what it names is missing, and the schema is recompiled at
the end. Users, grants and roles are not copied: each schema becomes a user
without a password in the copy.
"""

from collections import defaultdict
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from datadouble.names import TableName
from datadouble_oracle.driver import Connection, Value, read
from datadouble_oracle.introspect import binds, keys
from datadouble_oracle.render import compose, qualified, quote


class Phase(StrEnum):
    """When a statement runs, relative to loading rows."""

    SCHEMA = "schema"
    TABLE = "table"
    INDEX = "index"
    FOREIGN_KEY = "foreign_key"
    MODULE = "module"
    TRIGGER = "trigger"


class Statement(BaseModel):
    """One statement of the schema."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    phase: Phase
    owner: str
    """The schema it belongs to; it runs with that as the current schema."""
    name: str
    """The object it creates, as ``OWNER.NAME``, for messages and for statements that need it."""
    sql: str
    table: TableName | None = None
    """The table it belongs to; it runs only if the table exists."""
    needs: tuple[str, ...] = ()
    """Tables and views (as ``OWNER.NAME``) that must exist for it to run."""


class Identity(BaseModel):
    """An identity column, which loading fills with the extracted values."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    table: TableName
    column: str
    generation: str
    """``ALWAYS`` or ``BY DEFAULT``, as the source declares it."""


class Schema(BaseModel):
    """The schemas' objects and what a copy needs to recreate them."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    schemas: tuple[str, ...]
    statements: tuple[Statement, ...]
    identities: tuple[Identity, ...]

    def phase(self, phase: Phase) -> tuple[Statement, ...]:
        """The statements of one phase, in order."""
        return tuple(s for s in self.statements if s.phase is phase)


_TRANSFORMS = """
BEGIN
    DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'SEGMENT_ATTRIBUTES', FALSE);
    DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'STORAGE', FALSE);
    DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'TABLESPACE', FALSE);
    DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'REF_CONSTRAINTS', FALSE);
    DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'SQLTERMINATOR', FALSE);
    DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'PRETTY', TRUE);
END;
"""

_RESET = """
BEGIN
    DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'DEFAULT');
END;
"""

_DDL = "SELECT DBMS_METADATA.GET_DDL(:kind, :name, :owner) FROM dual"

# Object types first, then everything else in the order a copy creates it.
_OBJECTS = """
SELECT o.owner, o.object_name, o.object_type
FROM all_objects o
WHERE o.owner IN ({}) AND o.generated = 'N' AND o.object_name NOT LIKE 'BIN$%'
  AND o.object_type IN ('TYPE', 'SEQUENCE', 'TABLE', 'VIEW', 'FUNCTION', 'PROCEDURE',
                        'PACKAGE', 'PACKAGE BODY', 'TYPE BODY', 'SYNONYM', 'TRIGGER')
  AND NOT EXISTS (SELECT 1 FROM all_tables t WHERE t.owner = o.owner
                  AND t.table_name = o.object_name AND (t.temporary = 'Y' OR t.nested = 'YES'))
ORDER BY o.owner, o.object_id
"""

_INDEXES = """
SELECT i.owner, i.index_name, i.table_owner, i.table_name
FROM all_indexes i
WHERE i.table_owner IN ({}) AND i.generated = 'N' AND i.index_type NOT LIKE 'IOT%'
  AND i.index_type <> 'LOB'
  AND NOT EXISTS (SELECT 1 FROM all_constraints c
                  WHERE c.owner = i.table_owner AND c.index_name = i.index_name)
ORDER BY i.owner, i.index_name
"""

_DEPENDENCIES = """
SELECT owner, name, referenced_owner, referenced_name
FROM all_dependencies
WHERE owner IN ({}) AND type = 'VIEW' AND referenced_type IN ('TABLE', 'VIEW')
"""

_TRIGGERS = (
    "SELECT owner, trigger_name, table_owner, table_name FROM all_triggers WHERE owner IN ({})"
)

_IDENTITIES = """
SELECT owner, table_name, column_name, generation_type
FROM all_tab_identity_cols WHERE owner IN ({})
ORDER BY owner, table_name
"""

_KINDS = {
    "TYPE": Phase.SCHEMA,
    "SEQUENCE": Phase.SCHEMA,
    "TABLE": Phase.TABLE,
    "VIEW": Phase.MODULE,
    "FUNCTION": Phase.MODULE,
    "PROCEDURE": Phase.MODULE,
    "PACKAGE": Phase.MODULE,
    "PACKAGE BODY": Phase.MODULE,
    "TYPE BODY": Phase.MODULE,
    "SYNONYM": Phase.MODULE,
    "TRIGGER": Phase.TRIGGER,
}
_METADATA_KINDS = {
    "PACKAGE": "PACKAGE_SPEC",
    "PACKAGE BODY": "PACKAGE_BODY",
    "TYPE": "TYPE_SPEC",
    "TYPE BODY": "TYPE_BODY",
}


def object_name(owner: str, name: str) -> str:
    """How statements refer to an object: ``OWNER.NAME``."""
    return f"{owner}.{name}"


def split_trigger(ddl: str) -> tuple[str, bool]:
    """A trigger's ``CREATE``, and whether the source has it disabled.

    ``DBMS_METADATA`` follows the ``CREATE`` with ``ALTER TRIGGER ... ENABLE``
    or ``DISABLE``; a disabled trigger is disabled again after it is created.
    """
    head, separator, tail = ddl.rpartition("\nALTER TRIGGER ")
    if not separator:
        return ddl.strip(), False
    return head.strip(), tail.rstrip().endswith("DISABLE")


def _rows(connection: Connection, query: str, schemas: tuple[str, ...]) -> list[tuple[object, ...]]:
    params: dict[str, Value] = {f"s{i}": schema for i, schema in enumerate(schemas)}
    with connection.cursor() as cursor:
        cursor.execute(query.format(binds(len(schemas))), params)
        return [tuple(row) for row in cursor.fetchall()]


def _ddl(connection: Connection, kind: str, name: str, owner: str) -> str:
    with connection.cursor() as cursor:
        cursor.execute(_DDL, {"kind": kind.replace(" ", "_"), "name": name, "owner": owner})
        ((ddl,),) = cursor.fetchall()
    return str(read(ddl)).strip()


def _views_in_order(
    views: list[tuple[str, str]], needs: dict[str, set[str]]
) -> list[tuple[str, str]]:
    """Views after the views they read; a cycle is broken at the first."""
    pending = list(views)
    ordered: list[tuple[str, str]] = []
    while pending:
        waiting = {object_name(*view) for view in pending}
        ready = [
            view
            for view in pending
            if not (needs[object_name(*view)] - {object_name(*view)}) & waiting
        ] or pending[:1]
        ordered.extend(ready)
        pending = [view for view in pending if view not in ready]
    return ordered


def _foreign_keys(connection: Connection, schemas: tuple[str, ...]) -> list[Statement]:
    with connection.cursor() as cursor:
        references = keys(cursor, schemas)[1]
    delete = {"CASCADE": " ON DELETE CASCADE", "SET NULL": " ON DELETE SET NULL"}
    return [
        Statement(
            phase=Phase.FOREIGN_KEY,
            owner=r.table.namespace,
            name=object_name(r.table.namespace, r.name),
            sql=compose(
                "ALTER TABLE {} ADD CONSTRAINT {} FOREIGN KEY ({}) REFERENCES {} ({}){}{} {} {}",
                qualified(r.table),
                quote(r.name),
                ", ".join(quote(c) for c in r.columns),
                qualified(r.target),
                ", ".join(quote(c) for c in r.target_columns),
                delete.get(r.delete_rule, ""),
                f" DEFERRABLE INITIALLY {'DEFERRED' if r.deferred else 'IMMEDIATE'}"
                if r.deferrable
                else "",
                "ENABLE" if r.enabled else "DISABLE",
                "VALIDATE" if r.validated else "NOVALIDATE",
            ),
            table=r.table,
            needs=(object_name(r.target.namespace, r.target.name),),
        )
        for r in references
    ]


def script(connection: Connection, schemas: tuple[str, ...]) -> Schema:
    """The objects of ``schemas``, through ``DBMS_METADATA``."""
    with connection.cursor() as cursor:
        cursor.execute(_TRANSFORMS)
    try:
        return _script(connection, schemas)
    finally:
        with connection.cursor() as cursor:
            cursor.execute(_RESET)


def _script(connection: Connection, schemas: tuple[str, ...]) -> Schema:
    needs: defaultdict[str, set[str]] = defaultdict(set)
    for owner, name, referenced_owner, referenced in _rows(connection, _DEPENDENCIES, schemas):
        needs[object_name(str(owner), str(name))].add(
            object_name(str(referenced_owner), str(referenced))
        )
    triggers = {
        object_name(str(row[0]), str(row[1])): TableName.of(str(row[2]), str(row[3]))
        for row in _rows(connection, _TRIGGERS, schemas)
    }
    objects = [(str(r[0]), str(r[1]), str(r[2])) for r in _rows(connection, _OBJECTS, schemas)]
    views = _views_in_order([(o, n) for o, n, k in objects if k == "VIEW"], needs)
    ordered = [
        *(item for kind in ("TYPE", "SEQUENCE", "TABLE") for item in objects if item[2] == kind),
        *((owner, name, "VIEW") for owner, name in views),
        *(item for item in objects if item[2] not in {"TYPE", "SEQUENCE", "TABLE", "VIEW"}),
    ]
    statements: list[Statement] = []
    for owner, name, kind in ordered:
        ddl = _ddl(connection, _METADATA_KINDS.get(kind, kind), name, owner)
        full = object_name(owner, name)
        table = TableName.of(owner, name) if kind == "TABLE" else triggers.get(full)
        disabled = False
        if kind == "TRIGGER":
            ddl, disabled = split_trigger(ddl)
        statements.append(
            Statement(
                phase=_KINDS[kind],
                owner=owner,
                name=full,
                sql=ddl,
                table=table,
                needs=tuple(sorted(needs[full] - {full})) if kind == "VIEW" else (),
            )
        )
        if disabled:
            statements.append(
                Statement(
                    phase=Phase.TRIGGER,
                    owner=owner,
                    name=f"{full} disabled",
                    sql=compose("ALTER TRIGGER {}.{} DISABLE", quote(owner), quote(name)),
                    table=table,
                    needs=(full,),
                )
            )
    for row in _rows(connection, _INDEXES, schemas):
        index_owner, index_name, table_owner, table_name = (str(v) for v in row)
        target = TableName.of(table_owner, table_name)
        statements.append(
            Statement(
                phase=Phase.INDEX,
                owner=index_owner,
                name=object_name(index_owner, index_name),
                sql=_ddl(connection, "INDEX", index_name, index_owner),
                table=target,
            )
        )
    statements.extend(_foreign_keys(connection, schemas))
    identities = tuple(
        Identity(table=TableName.of(str(r[0]), str(r[1])), column=str(r[2]), generation=str(r[3]))
        for r in _rows(connection, _IDENTITIES, schemas)
    )
    return Schema(schemas=schemas, statements=tuple(statements), identities=identities)
