# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Reading a MySQL catalog into Datadouble's engine-neutral model.

The catalog is one or more databases (MySQL's schemas): their base tables,
named ``database.table``, with their columns, primary keys and foreign keys. A
foreign key between two databases read is an ordinary reference; one into a
database not read keeps its real target, so the planner asks for a fallback.
Generated columns are noted, since loading leaves them out.
"""

from collections import defaultdict
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field
from pymysql.connections import Connection

from datadouble.catalog import (
    Catalog,
    Column,
    ColumnKind,
    ForeignKey,
    Requirement,
    RequirementKind,
    Table,
)
from datadouble.names import ColumnName, EngineName, TableName
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
        cursor.execute(_PLUGINS)
        plugins = tuple(
            Plugin(name=_text(name), library=_text(library)) for name, library in cursor.fetchall()
        )
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
                    for (table, constraint), (target, own, theirs) in sorted(foreign.items())
                    if table == name
                ),
            )
            for name in names
        ),
        requirements=tuple(dict.fromkeys(plugin.requirement() for plugin in plugins)),
    )
    physical = Physical(generated={t: frozenset(c) for t, c in generated.items()}, plugins=plugins)
    return catalog, physical
