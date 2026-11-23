# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Reading a MySQL catalog into Datadouble's engine-neutral model.

The catalog is one or more databases (MySQL's schemas): their base tables,
named ``database.table``, with their columns, primary keys and foreign keys. A
foreign key between two databases read is an ordinary reference; one into a
database not read keeps its real target, so the planner asks for a fallback.
Generated columns are noted, since loading leaves them out.

Objects are the databases' views, stored functions and procedures, triggers
and events, named ``database.object``. A view's tables, views and functions
come from ``information_schema.VIEW_TABLE_USAGE`` and ``VIEW_ROUTINE_USAGE``,
which are exact and may cross databases; a trigger's table is exact too.
Views that read a database not read (directly or through other views) are
left out, as the copies leave them out. MySQL records nothing else, so
routine, trigger and event bodies are parsed for the names they use, in any
database read, and those edges are marked inferred. Reading a routine's body
needs the ``SHOW_ROUTINE`` privilege (or owning it); without it the body
reads as empty and its fingerprint cannot see changes. The adapter needs
MySQL 8.0.13 or later, the first to list views' tables.
"""

from collections import defaultdict
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field
from pymysql.connections import Connection
from pymysql.cursors import Cursor

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
from datadouble.engine import EngineError
from datadouble.names import ColumnName, EngineName, TableName
from datadouble.objects import Candidates, Dialect, infer_dependencies, merge
from datadouble_mysql.render import compose

ENGINE = EngineName("mysql")

SYSTEM_DATABASES = frozenset({"mysql", "information_schema", "performance_schema", "sys"})
"""MySQL's own databases, which are never read and exist in every server."""

_TABLES = """
SELECT TABLE_SCHEMA, TABLE_NAME FROM information_schema.TABLES
WHERE TABLE_SCHEMA IN ({}) AND TABLE_TYPE = 'BASE TABLE'
ORDER BY TABLE_SCHEMA, TABLE_NAME
"""

_COLUMNS = """
SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, DATA_TYPE, IS_NULLABLE, EXTRA
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA IN ({})
ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
"""

# Only plugins the data needs: storage engines and full-text parsers. Others
# (authentication, password policy, audit, replication) change how a server
# behaves, not what it can load.
_PLUGINS = """
SELECT PLUGIN_NAME, PLUGIN_LIBRARY FROM INFORMATION_SCHEMA.PLUGINS
WHERE PLUGIN_LIBRARY IS NOT NULL AND PLUGIN_STATUS = 'ACTIVE'
  AND PLUGIN_TYPE IN ('STORAGE ENGINE', 'FTPARSER')
ORDER BY PLUGIN_LIBRARY, PLUGIN_NAME
"""

_KEYS = """
SELECT k.TABLE_SCHEMA, k.TABLE_NAME, k.CONSTRAINT_NAME, k.COLUMN_NAME,
       k.REFERENCED_TABLE_SCHEMA, k.REFERENCED_TABLE_NAME, k.REFERENCED_COLUMN_NAME
FROM information_schema.KEY_COLUMN_USAGE k
JOIN information_schema.TABLE_CONSTRAINTS c
  ON c.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA AND c.TABLE_NAME = k.TABLE_NAME
 AND c.CONSTRAINT_NAME = k.CONSTRAINT_NAME
WHERE k.TABLE_SCHEMA IN ({}) AND c.CONSTRAINT_TYPE IN ('PRIMARY KEY', 'FOREIGN KEY')
ORDER BY k.TABLE_SCHEMA, k.TABLE_NAME, k.CONSTRAINT_NAME, k.ORDINAL_POSITION
"""

_VIEWS = """
SELECT TABLE_SCHEMA, TABLE_NAME, VIEW_DEFINITION, CHECK_OPTION, IS_UPDATABLE, SECURITY_TYPE
FROM information_schema.VIEWS
WHERE TABLE_SCHEMA IN ({})
ORDER BY TABLE_SCHEMA, TABLE_NAME
"""

_VIEW_TABLES = """
SELECT VIEW_SCHEMA, VIEW_NAME, TABLE_SCHEMA, TABLE_NAME FROM information_schema.VIEW_TABLE_USAGE
WHERE VIEW_SCHEMA IN ({})
"""

_VIEW_ROUTINES = """
SELECT TABLE_SCHEMA, TABLE_NAME, SPECIFIC_SCHEMA, SPECIFIC_NAME
FROM information_schema.VIEW_ROUTINE_USAGE
WHERE TABLE_SCHEMA IN ({})
"""

_ROUTINES = """
SELECT r.ROUTINE_SCHEMA, r.ROUTINE_NAME, r.ROUTINE_TYPE, coalesce(r.DTD_IDENTIFIER, ''),
       coalesce(r.ROUTINE_DEFINITION, ''), r.IS_DETERMINISTIC, r.SQL_DATA_ACCESS,
       r.SECURITY_TYPE,
       coalesce((
         SELECT GROUP_CONCAT(concat_ws(' ', p.PARAMETER_MODE, p.PARAMETER_NAME, p.DTD_IDENTIFIER)
                             ORDER BY p.ORDINAL_POSITION SEPARATOR ', ')
         FROM information_schema.PARAMETERS p
         WHERE p.SPECIFIC_SCHEMA = r.ROUTINE_SCHEMA AND p.SPECIFIC_NAME = r.SPECIFIC_NAME
           AND p.ROUTINE_TYPE = r.ROUTINE_TYPE AND p.ORDINAL_POSITION > 0), '')
FROM information_schema.ROUTINES r
WHERE r.ROUTINE_SCHEMA IN ({})
ORDER BY r.ROUTINE_SCHEMA, r.ROUTINE_TYPE, r.ROUTINE_NAME
"""

_TRIGGERS = """
SELECT TRIGGER_SCHEMA, TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION,
       ACTION_STATEMENT
FROM information_schema.TRIGGERS
WHERE TRIGGER_SCHEMA IN ({})
ORDER BY TRIGGER_SCHEMA, TRIGGER_NAME
"""

_EVENTS = """
SELECT EVENT_SCHEMA, EVENT_NAME, EVENT_DEFINITION, EVENT_TYPE, coalesce(EXECUTE_AT, ''),
       coalesce(INTERVAL_VALUE, ''), coalesce(INTERVAL_FIELD, ''), coalesce(STARTS, ''),
       coalesce(ENDS, ''), ON_COMPLETION
FROM information_schema.EVENTS
WHERE EVENT_SCHEMA IN ({})
ORDER BY EVENT_SCHEMA, EVENT_NAME
"""

_DIALECT = Dialect(
    quotes="`", backslash_escapes=True, hash_comments=True, double_quoted_strings=True
)
"""MySQL's default SQL mode: ``"..."`` is a string, not an identifier."""

_HAS_VIEW_USAGE = """
SELECT VERSION(), COUNT(*) FROM information_schema.TABLES
WHERE TABLE_SCHEMA = 'information_schema' AND TABLE_NAME = 'VIEW_TABLE_USAGE'
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
    plugins: tuple["Plugin", ...] = ()
    """Plugins installed from a library, which working servers install too."""


class Plugin(BaseModel):
    """A plugin installed from a library (``INSTALL PLUGIN name SONAME 'library'``)."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z0-9_]+$")
    library: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")

    def requirement(self) -> Requirement:
        """The plugin's library as a requirement; its version is the server's."""
        return Requirement(kind=RequirementKind.PLUGIN, name=self.library)


def _text(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def introspect(connection: Connection, databases: tuple[str, ...]) -> tuple[Catalog, Physical]:
    """Read the catalog of ``databases``."""
    listed = ", ".join(["%s"] * len(databases))
    with connection.cursor() as cursor:
        cursor.execute(compose(_TABLES, listed), databases)
        names = [TableName.of(_text(db), _text(name)) for db, name in cursor.fetchall()]
        columns: defaultdict[TableName, list[Column]] = defaultdict(list)
        generated: defaultdict[TableName, set[ColumnName]] = defaultdict(set)
        cursor.execute(compose(_COLUMNS, listed), databases)
        for db, table, name, column_type, data_type, nullable, extra in cursor.fetchall():
            column = Column(
                name=ColumnName(_text(name)),
                type=_text(column_type),
                kind=kind_of(_text(data_type)),
                nullable=_text(nullable) == "YES",
            )
            owner = TableName.of(_text(db), _text(table))
            columns[owner].append(column)
            # EXTRA says VIRTUAL GENERATED or STORED GENERATED for generated
            # columns, and DEFAULT_GENERATED for ones with a default expression.
            if _text(extra).upper().split(" ")[0] in {"VIRTUAL", "STORED"}:
                generated[owner].add(column.name)
        primary: defaultdict[TableName, list[ColumnName]] = defaultdict(list)
        foreign: dict[
            tuple[TableName, str], tuple[TableName, list[ColumnName], list[ColumnName]]
        ] = {}
        cursor.execute(compose(_KEYS, listed), databases)
        for db, table, constraint, column_name, schema, target, target_column in cursor.fetchall():
            owner = TableName.of(_text(db), _text(table))
            if target is None:
                primary[owner].append(ColumnName(_text(column_name)))
                continue
            key = (owner, _text(constraint))
            entry = foreign.setdefault(key, (TableName.of(_text(schema), _text(target)), [], []))
            entry[1].append(ColumnName(_text(column_name)))
            entry[2].append(ColumnName(_text(target_column)))
        found = _objects(cursor, databases, frozenset(names))
        cursor.execute(_PLUGINS)
        plugins = tuple(
            Plugin(name=_text(name), library=_text(library)) for name, library in cursor.fetchall()
        )
    tables = tuple(
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
                for (table, constraint), (target, own, theirs) in sorted(foreign.items())
                if table == name
            ),
        )
        for name in names
    )
    catalog = Catalog(
        engine=ENGINE,
        tables=tables,
        requirements=tuple(dict.fromkeys(plugin.requirement() for plugin in plugins)),
        objects=_with_inferred(found, tables),
    )
    physical = Physical(generated={t: frozenset(c) for t, c in generated.items()}, plugins=plugins)
    return catalog, physical


type _Cursor = Cursor


class _Found:
    """Objects as read, before bodies are parsed: definition, body and exact edges."""

    def __init__(self) -> None:
        self.definitions: dict[ObjectRef, str] = {}
        self.bodies: dict[ObjectRef, str] = {}
        self.exact: defaultdict[ObjectRef, list[Dependency]] = defaultdict(list)
        self.views: dict[tuple[str, str], ObjectRef] = {}
        self.functions: dict[tuple[str, str], ObjectRef] = {}

    def add(self, ref: ObjectRef, definition: str, body: str = "") -> None:
        self.definitions[ref] = definition
        self.bodies[ref] = body

    def drop(self, refs: list[ObjectRef]) -> None:
        """Leave ``refs`` out, with their edges."""
        for ref in refs:
            del self.definitions[ref], self.bodies[ref]
            self.exact.pop(ref, None)


def check_version(cursor: _Cursor) -> None:
    """Fail unless the server lists views' tables (MySQL 8.0.13 or later)."""
    cursor.execute(_HAS_VIEW_USAGE)
    require_view_usage(cursor.fetchone())


def require_view_usage(row: tuple[object, ...] | None) -> None:
    """Fail, naming the version, unless ``row`` says ``VIEW_TABLE_USAGE`` exists."""
    version, usage = ("unknown", 0) if row is None else (_text(row[0]), int(_text(row[1])))
    if not usage:
        message = (
            f"the MySQL adapter needs MySQL 8.0.13 or later, which lists views' tables "
            f"(information_schema.VIEW_TABLE_USAGE); the source runs {version}"
        )
        raise EngineError(message)


def _ref(kind: ObjectKind, database: object, name: object) -> ObjectRef:
    return ObjectRef(kind=kind, namespace=(_text(database),), name=_text(name))


def _objects(cursor: _Cursor, databases: tuple[str, ...], tables: frozenset[TableName]) -> _Found:
    """The databases' views, routines, triggers and events, with their exact edges."""
    check_version(cursor)
    listed = ", ".join(["%s"] * len(databases))
    found = _Found()
    cursor.execute(compose(_VIEWS, listed), databases)
    for database, name, *rest in cursor.fetchall():
        ref = _ref(ObjectKind.VIEW, database, name)
        found.views[(_text(database), _text(name))] = ref
        found.add(ref, " ".join(map(_text, rest)))
    cursor.execute(compose(_ROUTINES, listed), databases)
    for database, name, kind, returns, body, *rest in cursor.fetchall():
        function = _text(kind) == "FUNCTION"
        ref = _ref(ObjectKind.FUNCTION if function else ObjectKind.PROCEDURE, database, name)
        if function:
            found.functions[(_text(database), _text(name))] = ref
        parameters = _text(rest[-1])
        text = [_text(kind), f"({parameters})", _text(returns), *map(_text, rest[:-1])]
        found.add(ref, " ".join([*text, _text(body)]), _text(body))
    _view_edges(cursor, listed, databases, found, tables)
    cursor.execute(compose(_TRIGGERS, listed), databases)
    for database, name, table, *rest in cursor.fetchall():
        ref = _ref(ObjectKind.TRIGGER, database, name)
        found.add(ref, " ".join([_text(table), *map(_text, rest)]), _text(rest[-1]))
        found.exact[ref].append(Dependency(table=TableName.of(_text(database), _text(table))))
    cursor.execute(compose(_EVENTS, listed), databases)
    for database, name, body, *rest in cursor.fetchall():
        ref = _ref(ObjectKind.EVENT, database, name)
        found.add(ref, " ".join([*map(_text, rest), _text(body)]), _text(body))
    found.drop(_over_unread(found, frozenset(databases)))
    return found


def _view_edges(
    cursor: _Cursor,
    listed: str,
    databases: tuple[str, ...],
    found: _Found,
    tables: frozenset[TableName],
) -> None:
    """Record each view's tables, views and functions, as MySQL lists them.

    A name in a database not read is a table there, or a function there.
    """
    nodes = {(t.outermost, t.name): Dependency(table=t) for t in tables}
    nodes |= {key: Dependency(object=ref) for key, ref in found.views.items()}
    cursor.execute(compose(_VIEW_TABLES, listed), databases)
    for view_db, view, database, table in cursor.fetchall():
        key = (_text(database), _text(table))
        edge = nodes.get(key) or Dependency(table=TableName.of(*key))
        found.exact[found.views[(_text(view_db), _text(view))]].append(edge)
    cursor.execute(compose(_VIEW_ROUTINES, listed), databases)
    for view_db, view, database, routine in cursor.fetchall():
        key = (_text(database), _text(routine))
        ref = found.functions.get(key) or _ref(ObjectKind.FUNCTION, *key)
        found.exact[found.views[(_text(view_db), _text(view))]].append(Dependency(object=ref))


def _over_unread(found: _Found, databases: frozenset[str]) -> list[ObjectRef]:
    """Views that read a database not read, directly or through other views."""
    left_out: list[ObjectRef] = []
    pending = list(found.views.values())
    grew = True
    while grew:
        reading = [
            ref
            for ref in pending
            if any(
                _target_database(edge) not in databases or edge.object in left_out
                for edge in found.exact[ref]
            )
        ]
        left_out += reading
        pending = [ref for ref in pending if ref not in reading]
        grew = bool(reading)
    return left_out


def _target_database(edge: Dependency) -> str:
    target = edge.table if edge.object is None else edge.object
    return "" if target is None else target.namespace[0]


def _with_inferred(found: _Found, tables: tuple[Table, ...]) -> tuple[SourceObject, ...]:
    candidates = Candidates.of(
        tables, [SourceObject(ref=ref, fingerprint="") for ref in found.definitions]
    )
    return tuple(
        SourceObject(
            ref=ref,
            fingerprint=definition_fingerprint(definition),
            depends_on=merge(
                found.exact[ref],
                infer_dependencies(found.bodies[ref], ref, candidates, _DIALECT),
            ),
        )
        for ref, definition in found.definitions.items()
    )
