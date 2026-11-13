# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The scratch side: working databases, anonymization, images and restores.

``load`` builds a group's working database in a new instance: the database,
``before_load`` hooks, schemas and sequences, the group's tables, the rows with
``BULK INSERT`` (which checks no constraints and fires no triggers), fallbacks
for missing parents, then indexes, check constraints, foreign keys, modules and
triggers. Only the group's tables are created; a foreign key, view or function
that needs a table the database does not have is left out. ``transform``
copies the detached files and anonymizes the copy with its triggers disabled,
the rules and hooks in one transaction. ``package`` writes the data and log
files (physical) or the schema and rows in extract's format (logical);
``restore`` attaches or loads them.
"""

import datetime as dt
import shutil
from collections.abc import Generator, Iterable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

import pymssql
from pydantic import BaseModel, ConfigDict
from pymssql import Connection

from datadouble.catalog import Column, ColumnKind
from datadouble.engine import EngineError, RestoredImage, TableCount, Transformation
from datadouble.files import copy_tree
from datadouble.names import ColumnRef, TableName
from datadouble.planner import Fixup, Plan, PlannedGroup, PlannedRule
from datadouble.spec import ExpressionRule, Hook, ImageFormat, NullRule
from datadouble.subset import AllRows, Edge, EdgeMode
from datadouble_mssql.errors import database_error
from datadouble_mssql.introspect import Physical, introspect
from datadouble_mssql.render import (
    OUTER,
    Param,
    Renderer,
    as_of_value,
    columns,
    compose,
    native,
    qualified,
    quote,
)
from datadouble_mssql.script import Phase, Schema, Statement, script
from datadouble_mssql.server import DATA, MssqlServer, Tools, literal
from datadouble_mssql.source import (
    ExtractedTable,
    ExtractIndex,
    as_text,
    extracted_table,
    text_columns,
    write_rows,
)

_INFO = "database.json"
_BATCH = 500
_PLACEHOLDERS: dict[ColumnKind, str] = {
    ColumnKind.TEXT: "''",
    ColumnKind.INTEGER: "0",
    ColumnKind.DECIMAL: "0",
    ColumnKind.FLOAT: "0",
    ColumnKind.BOOLEAN: "0",
    ColumnKind.DATE: "'1970-01-01'",
    ColumnKind.TIMESTAMP: "'1970-01-02T00:00:00'",
    ColumnKind.TIME: "'00:00:00'",
    ColumnKind.JSON: "'{}'",
    ColumnKind.BINARY: "0x",
    ColumnKind.UUID: "'00000000-0000-0000-0000-000000000000'",
}
_FROM_BINARY = frozenset({"binary", "varbinary", "image"})
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
_EXACT = "Latin1_General_100_BIN2"
"""Compares text by code point, so values a case-insensitive database treats as
equal stay distinct."""


class DatabaseInfo(BaseModel):
    """Stored beside a working database and in every image."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    database: str
    server_version: str
    edition: str


def placeholder(table: TableName, column: Column) -> str:
    """A stand-in value for a required column of a stub row."""
    value = _PLACEHOLDERS.get(column.kind)
    if value is None:
        message = (
            f"cannot stub {table}: no placeholder for {column.name} ({column.type}); "
            "make the column nullable, give it a default, or use another fallback"
        )
        raise EngineError(message)
    return value


def format_file(table: ExtractedTable) -> str:
    """A ``BULK INSERT`` format file: each field UTF-16 text with a 4-byte length.

    Fields map to the table's columns by position; columns without a field
    (computed columns and ``rowversion``) are filled in by the server.
    """
    lines = [
        f'{field} SQLNCHAR 4 0 "" {ordinal} c{ordinal} ""'
        for field, ordinal in enumerate(table.ordinals, 1)
    ]
    return "\n".join(["14.0", str(len(lines)), *lines]) + "\n"


def from_text(value: str, system_type: str) -> str:
    """An expression converting text written by ``as_text`` back to ``system_type``."""
    if system_type in _FROM_BINARY:
        return compose("CONVERT(VARBINARY(MAX), {}, 2)", value)
    return value


def _execute(
    connection: Connection, action: str, statement: str, params: dict[str, Param] | None = None
) -> None:
    try:
        with connection.cursor() as cursor:
            cursor.execute(statement, params or None)
    except pymssql.Error as error:
        raise database_error(error, action) from None


def _rows(
    connection: Connection, action: str, statement: str, params: dict[str, Param] | None = None
) -> list[tuple[object, ...]]:
    try:
        with connection.cursor() as cursor:
            cursor.execute(statement, params or None)
            return [tuple(row) for row in cursor.fetchall()]
    except pymssql.Error as error:
        raise database_error(error, action) from None


def _params(statement: str, as_of: str) -> dict[str, Param] | None:
    return {"as_of": as_of} if "%(as_of)s" in statement else None


def _matches(edge: Edge) -> str:
    return " AND ".join(
        compose("p.{} = c.{}", quote(p), quote(c))
        for p, c in zip(edge.parent_columns, edge.columns, strict=True)
    )


class MssqlRestored:
    """A database open for checks: a restored image, or a working database."""

    def __init__(self, connection: Connection, physical: Physical) -> None:
        """Wrap a connection to the database and its catalog."""
        self.connection = connection
        self.physical = physical

    def _scalar(self, action: str, statement: str) -> int:
        rows = _rows(self.connection, action, statement)
        return int(str(rows[0][0])) if rows else 0

    def counts(self, tables: Iterable[TableName]) -> dict[TableName, int]:
        """Rows per table."""
        return {
            table: self._scalar(
                f"counting {table}", compose("SELECT COUNT_BIG(*) FROM {}", qualified(table))
            )
            for table in tables
        }

    def orphans(self, edge: Edge) -> int:
        """Child rows with a non-null reference and no parent."""
        present = " AND ".join(compose("c.{} IS NOT NULL", quote(c)) for c in edge.columns)
        when = f" AND ({edge.when})" if edge.when else ""
        return self._scalar(
            f"checking {edge.name}",
            compose(
                "SELECT COUNT_BIG(*) FROM {} AS c WHERE {}{}"
                " AND NOT EXISTS (SELECT 1 FROM {} AS p WHERE {})",
                qualified(edge.child),
                present,
                when,
                qualified(edge.parent),
                _matches(edge),
            ),
        )

    def text_values(self, column: ColumnRef) -> Iterator[str]:
        """Distinct non-null values of a column, as text, streamed."""
        system_type = self.physical.types.get(column.table, {}).get(column.column, "nvarchar")
        name = columns([column.column], OUTER)
        statement = compose(
            "SELECT DISTINCT {} COLLATE {} FROM {} AS {} WHERE {} IS NOT NULL",
            as_text(name, system_type),
            _EXACT,
            qualified(column.table),
            OUTER,
            name,
        )
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(statement)
                for (value,) in cursor:
                    yield str(value)
        except pymssql.Error as error:
            raise database_error(error, f"scanning {column}") from None

    def failing_rows(self, query: str) -> int:
        """Rows a spec assertion returns, counted as they stream.

        T-SQL cannot count a derived table whose columns have no names, as
        ``SELECT 1 ...`` assertions do, so the query runs as written.
        """
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(query)
                return sum(1 for _ in cursor)
        except pymssql.Error as error:
            raise database_error(error, "running an assertion") from None


def _copy_tree(source: Path, destination: Path) -> None:
    """Copy a directory, sharing blocks where the filesystem allows."""
    problem = copy_tree(source, destination)
    if problem:
        message = f"cannot copy {source.name}: {problem}"
        raise EngineError(message)


class _Builder:
    """Creates a database's objects from a schema, phase by phase."""

    def __init__(self, connection: Connection, schema: Schema) -> None:
        self.connection = connection
        self.schema = schema
        self.present: set[str] = set()
        self._users: dict[str, str] = {}

    def run(self, phase: Phase, tables: frozenset[TableName] | None = None) -> None:
        """Run a phase's statements whose table and needs exist.

        With ``tables``, a statement's table must be one of them; without, it
        must have been created already.
        """
        for statement in self.schema.phase(phase):
            if not self._wanted(statement, tables):
                continue
            self._create(statement)
            self.present.add(statement.name)

    def _wanted(self, statement: Statement, tables: frozenset[TableName] | None) -> bool:
        if statement.table is not None:
            if tables is not None and statement.table not in tables:
                return False
            name = f"{statement.table.namespace}.{statement.table.name}"
            if tables is None and name not in self.present:
                return False
        return all(need in self.present for need in statement.needs)

    def _create(self, statement: Statement) -> None:
        settings = compose(
            "SET ANSI_NULLS {}; SET QUOTED_IDENTIFIER {}",
            "ON" if statement.ansi_nulls else "OFF",
            "ON" if statement.quoted_identifier else "OFF",
        )
        modular = statement.phase in {Phase.MODULE, Phase.TRIGGER}
        user = self._user(statement.schema_name) if modular else None
        _execute(self.connection, f"creating {statement.name}", settings)
        if user is not None:
            _execute(
                self.connection, "changing schema", compose("EXECUTE AS USER = {}", literal(user))
            )
        try:
            _execute(self.connection, f"creating {statement.name}", statement.sql)
        finally:
            if user is not None:
                _execute(self.connection, "changing schema", "REVERT")
            _execute(
                self.connection, "restoring settings", "SET ANSI_NULLS ON; SET QUOTED_IDENTIFIER ON"
            )

    def _user(self, schema: str) -> str | None:
        """A user whose default schema is ``schema``, for modules with unqualified names."""
        if schema == "dbo":
            return None
        if schema not in self._users:
            user = f"datadouble-schema-{len(self._users)}"
            _execute(
                self.connection,
                f"preparing schema {schema}",
                compose(
                    "CREATE USER {} WITHOUT LOGIN WITH DEFAULT_SCHEMA = {};"
                    " ALTER ROLE db_owner ADD MEMBER {}",
                    quote(user),
                    quote(schema),
                    quote(user),
                ),
            )
            self._users[schema] = user
        return self._users[schema]

    def finish(self) -> None:
        """Drop the users ``_user`` made."""
        for user in self._users.values():
            _execute(
                self.connection, "removing a schema user", compose("DROP USER {}", quote(user))
            )
        self._users.clear()

    def tables(self, tables: frozenset[TableName]) -> None:
        """Create the schemas, sequences and ``tables``."""
        self.run(Phase.SCHEMA)
        self.run(Phase.TABLE, tables)

    def rest(self) -> None:
        """Everything after the rows: indexes, foreign keys, modules and triggers."""
        for phase in (Phase.INDEX, Phase.FOREIGN_KEY, Phase.MODULE, Phase.TRIGGER):
            self.run(phase)
        self.finish()


def _bulk_load(connection: Connection, extract: Path, table: ExtractedTable, scratch: Path) -> None:
    """Load one table's data file through a format file written to ``scratch``."""
    if table.rows == 0:
        return
    formats = scratch / f"{Path(table.file).stem}.fmt"
    formats.write_text(format_file(table), "ascii")
    _execute(
        connection,
        f"loading {table.table}",
        compose(
            "BULK INSERT {} FROM {} WITH (FORMATFILE = {}, KEEPIDENTITY, KEEPNULLS, TABLOCK)",
            qualified(table.table),
            literal(str(extract / table.file)),
            literal(str(formats)),
        ),
    )
    formats.unlink()


def _reseed(connection: Connection) -> None:
    """Move identity counters past the loaded rows, as the source's are."""
    tables = _rows(
        connection,
        "listing identity columns",
        "SELECT s.name, t.name FROM sys.identity_columns i"
        " JOIN sys.tables t ON t.object_id = i.object_id"
        " JOIN sys.schemas s ON s.schema_id = t.schema_id WHERE t.is_ms_shipped = 0",
    )
    for schema, table in tables:
        name = qualified(TableName.of(str(schema), str(table)))
        _execute(
            connection,
            f"reseeding {schema}.{table}",
            compose("DBCC CHECKIDENT ({}) WITH NO_INFOMSGS", literal(name)),
        )


def _configure(connection: Connection, database: str, schema: Schema) -> None:
    """Give a new database the source's compatibility level and isolation settings."""
    name = quote(database)
    settings = [compose("COMPATIBILITY_LEVEL = {}", str(schema.compatibility_level))]
    if schema.snapshot_isolation:
        settings.append("ALLOW_SNAPSHOT_ISOLATION ON")
    if schema.read_committed_snapshot:
        settings.append("READ_COMMITTED_SNAPSHOT ON")
    for setting in settings:
        _execute(
            connection,
            f"configuring {database}",
            compose("ALTER DATABASE {} SET {}", name, setting),
        )


class _Anonymizer:
    """Applies a transformation's column rules to one database."""

    def __init__(
        self, connection: Connection, physical: Physical, transformation: Transformation
    ) -> None:
        self.connection = connection
        self.physical = physical
        self.transformation = transformation

    def table(self, position: int, table: TableName, rules: list[PlannedRule]) -> None:
        """Apply every rule for one table in a single UPDATE.

        Expressions and their ``where`` clauses are the spec's SQL, as written.
        """
        types = self.physical.types.get(table, {})
        joins: list[str] = []
        assignments: list[str] = []
        for number, rule in enumerate(rules):
            column = compose("t.{}", quote(rule.column.column))
            system_type = types.get(rule.column.column, "nvarchar")
            match rule.rule:
                case NullRule():
                    assignments.append(compose("{} = NULL", column))
                case ExpressionRule(expression=expression, where=where):
                    value = f"({expression})"
                    if where:
                        value = compose("CASE WHEN ({}) THEN {} ELSE {} END", where, value, column)
                    assignments.append(compose("{} = {}", column, value))
                case _:
                    mapped = quote(f"m{number}")
                    temporary = quote(f"#dd_map_{position}_{number}")
                    self._replacements(temporary, rule, system_type)
                    text = compose("{} COLLATE {}", as_text(column, system_type), _EXACT)
                    joins.append(
                        compose(
                            "LEFT JOIN {} AS {} ON {}.dd_hash = CAST(HASHBYTES('SHA2_256', {})"
                            " AS BINARY(32)) AND {}.dd_old = {}",
                            temporary,
                            mapped,
                            mapped,
                            text,
                            mapped,
                            text,
                        )
                    )
                    assignments.append(
                        compose(
                            "{} = CASE WHEN {} IS NULL THEN NULL ELSE {} END",
                            column,
                            column,
                            from_text(compose("{}.dd_new", mapped), system_type),
                        )
                    )
        update = compose(
            "UPDATE t SET {} FROM {} AS t {}",
            ", ".join(assignments),
            qualified(table),
            " ".join(joins),
        )
        _execute(self.connection, f"anonymizing {table}", update)

    def _replacements(self, temporary: str, rule: PlannedRule, system_type: str) -> None:
        """Fill a temporary table mapping each original value to its replacement."""
        name = columns([rule.column.column], OUTER)
        distinct = compose(
            "SELECT DISTINCT {} COLLATE {} FROM {} AS {} WHERE {} IS NOT NULL",
            as_text(name, system_type),
            _EXACT,
            qualified(rule.column.table),
            OUTER,
            name,
        )
        read = _rows(self.connection, f"reading {rule.column}", distinct)
        mapping = self.transformation.mapper(rule, [str(row[0]) for row in read])
        _execute(
            self.connection,
            "preparing replacements",
            compose(
                "CREATE TABLE {} (dd_old NVARCHAR(MAX) COLLATE {} NOT NULL,"
                " dd_new NVARCHAR(MAX),"
                " dd_hash AS CAST(HASHBYTES('SHA2_256', dd_old) AS BINARY(32)) PERSISTED);"
                " CREATE CLUSTERED INDEX dd_by_hash ON {} (dd_hash)",
                temporary,
                _EXACT,
                temporary,
            ),
        )
        pairs = list(mapping.items())
        for start in range(0, len(pairs), _BATCH):
            params: dict[str, Param] = {}
            rows: list[str] = []
            for number, (old, new) in enumerate(pairs[start : start + _BATCH]):
                params[f"o{number}"], params[f"n{number}"] = old, new
                rows.append(f"(%(o{number})s, %(n{number})s)")
            _execute(
                self.connection,
                "preparing replacements",
                compose("INSERT INTO {} (dd_old, dd_new) VALUES {}", temporary, ", ".join(rows)),
                params,
            )


class MssqlWorking:
    """Working databases and images under one scratch directory."""

    def __init__(self, tools: Tools, scratch: Path) -> None:
        """Use ``tools`` for every instance under ``scratch``."""
        self.tools = tools
        self.scratch = scratch
        self._version: str | None = None

    def server_version(self) -> str:
        """The version of the image's server, which images are built for."""
        if self._version is None:
            directory = self.scratch / ".version"
            directory.mkdir(parents=True, exist_ok=True)
            try:
                with MssqlServer(self.tools, directory, self.scratch).running() as server:
                    self._version = server.version()
            finally:
                shutil.rmtree(directory, ignore_errors=True)
        return self._version

    def _check_version(self, server: MssqlServer, source_version: str) -> str:
        version = server.version()
        if version.partition("-")[0] != source_version.partition("-")[0]:
            message = (
                f"the working database image {self.tools.image} runs SQL Server {version}, but"
                f" the source runs {source_version}; set source.image to Microsoft's image of"
                " the source's version"
            )
            raise EngineError(message)
        self._version = version
        return version

    # load

    def load(
        self,
        plan: Plan,
        group: PlannedGroup,
        extract: Path,
        directory: Path,
        hooks: tuple[Hook, ...],
    ) -> tuple[TableCount, ...]:
        """Build the group's working database."""
        index = ExtractIndex.model_validate_json((extract / "index.json").read_bytes())
        schema = Schema.model_validate_json((extract / "schema.json").read_bytes())
        members = frozenset(group.tables)
        server = MssqlServer(self.tools, directory, directory, (extract,))
        with server.running():
            version = self._check_version(server, index.server_version)
            server.create(index.database, schema.collation)
            with server.connect(index.database) as connection:
                _configure(connection, index.database, schema)
                for hook in hooks:
                    _execute(connection, "a before_load hook", hook.sql)
                builder = _Builder(connection, schema)
                builder.tables(members)
                for table in index.tables:
                    if table.table in members:
                        _bulk_load(connection, extract, table, directory)
                as_of = as_of_value(plan.as_of)
                for fixup in group.fixups:
                    self._fixup(connection, plan, index, fixup, as_of)
                builder.rest()
                _reseed(connection)
                counts = MssqlRestored(connection, Physical()).counts(group.tables)
            server.detach(index.database)
        info = DatabaseInfo(
            database=index.database, server_version=version, edition=self.tools.edition
        )
        (directory / _INFO).write_text(info.model_dump_json(indent=2), "utf-8")
        return tuple(TableCount(table=t, rows=n) for t, n in counts.items())

    @staticmethod
    def _fixup(
        connection: Connection, plan: Plan, index: ExtractIndex, fixup: Fixup, as_of: str
    ) -> None:
        """Null or stub references whose parent rows are missing."""
        edge = fixup.edge
        conditions = [compose("c.{} IS NOT NULL", quote(c)) for c in edge.columns]
        if edge.when:
            conditions.append(f"({native(edge.when)})")
        if fixup.parent_present:
            conditions.append(
                compose(
                    "NOT EXISTS (SELECT 1 FROM {} AS p WHERE {})",
                    qualified(edge.parent),
                    _matches(edge),
                )
            )
        where = " AND ".join(conditions)
        if fixup.mode is EdgeMode.NULL:
            statement = compose(
                "UPDATE c SET {} FROM {} AS c WHERE {}",
                ", ".join(compose("c.{} = NULL", quote(c)) for c in edge.columns),
                qualified(edge.child),
                where,
            )
        else:
            parent = plan.catalog.table(edge.parent)
            extracted = index.table(edge.parent)
            loaded = extracted.columns if extracted else ()
            fillers = [
                (column.name, placeholder(edge.parent, column))
                for column in (parent.columns if parent else ())
                if column.name not in edge.parent_columns
                and not column.nullable
                and column.name in loaded
            ]
            statement = compose(
                "INSERT INTO {} ({}) SELECT DISTINCT {} FROM {} AS c WHERE {}",
                qualified(edge.parent),
                columns([*edge.parent_columns, *(name for name, _ in fillers)]),
                ", ".join(
                    [*(compose("c.{}", quote(c)) for c in edge.columns), *(v for _, v in fillers)]
                ),
                qualified(edge.child),
                where,
            )
            identity = _rows(
                connection,
                f"reading {edge.parent}",
                "SELECT COUNT(*) FROM sys.identity_columns WHERE object_id = OBJECT_ID(%(t)s)",
                {"t": qualified(edge.parent)},
            )
            if identity and int(str(identity[0][0])):
                statement = compose(
                    "SET IDENTITY_INSERT {} ON; {}; SET IDENTITY_INSERT {} OFF",
                    qualified(edge.parent),
                    statement,
                    qualified(edge.parent),
                )
        _execute(
            connection,
            f"applying the fallback for {edge.name}",
            statement,
            _params(statement, as_of),
        )

    # transform

    def transform(
        self,
        group: PlannedGroup,
        source: Path,
        directory: Path,
        transformation: Transformation,
    ) -> tuple[TableCount, ...]:
        """Copy the working database and anonymize the copy."""
        directory.parent.mkdir(parents=True, exist_ok=True)
        _copy_tree(source, directory)
        info = DatabaseInfo.model_validate_json((directory / _INFO).read_bytes())
        server = MssqlServer(self.tools, directory, directory)
        with server.running():
            server.attach(info.database)
            with server.connect(info.database) as connection:
                physical = introspect(connection)[1]
                triggered = self._triggered_tables(connection)
                for table in triggered:
                    _execute(
                        connection,
                        f"disabling the triggers of {table}",
                        compose("ALTER TABLE {} DISABLE TRIGGER ALL", qualified(table)),
                    )
                _execute(connection, "starting the transformation", "BEGIN TRANSACTION")
                by_table: dict[TableName, list[PlannedRule]] = {}
                for rule in transformation.rules:
                    by_table.setdefault(rule.column.table, []).append(rule)
                anonymizer = _Anonymizer(connection, physical, transformation)
                for position, (table, rules) in enumerate(by_table.items()):
                    anonymizer.table(position, table, rules)
                for hook in transformation.hooks:
                    _execute(connection, "an after_transform hook", hook.sql)
                _execute(connection, "committing the transformation", "COMMIT TRANSACTION")
                for table in triggered:
                    _execute(
                        connection,
                        f"enabling the triggers of {table}",
                        compose("ALTER TABLE {} ENABLE TRIGGER ALL", qualified(table)),
                    )
                counts = MssqlRestored(connection, physical).counts(group.tables)
            server.detach(info.database)
        return tuple(TableCount(table=t, rows=n) for t, n in counts.items())

    @staticmethod
    def _triggered_tables(connection: Connection) -> list[TableName]:
        rows = _rows(
            connection,
            "listing triggers",
            "SELECT DISTINCT s.name, t.name FROM sys.triggers r"
            " JOIN sys.tables t ON t.object_id = r.parent_id"
            " JOIN sys.schemas s ON s.schema_id = t.schema_id"
            " WHERE r.is_disabled = 0 ORDER BY 1, 2",
        )
        return [TableName.of(str(schema), str(table)) for schema, table in rows]

    # package and restore

    def package(self, source: Path, image_format: ImageFormat, directory: Path) -> None:
        """Write one format of the image into ``directory``."""
        info = DatabaseInfo.model_validate_json((source / _INFO).read_bytes())
        shutil.copyfile(source / _INFO, directory / _INFO)
        if image_format is ImageFormat.PHYSICAL:
            _copy_tree(source / DATA, directory / DATA)
            return
        scratch = directory.parent / f".{directory.name}-instance"
        scratch.mkdir(parents=True, exist_ok=True)
        _copy_tree(source / DATA, scratch / DATA)
        server = MssqlServer(self.tools, scratch, directory)
        try:
            with server.running():
                server.attach(info.database)
                with server.connect(info.database) as connection:
                    self._write_logical(connection, info, directory)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    @staticmethod
    def _write_logical(connection: Connection, info: DatabaseInfo, directory: Path) -> None:
        """The schema and every table's rows, in extract's format."""
        try:
            catalog, physical = introspect(connection)
            schema = script(connection)
        except pymssql.Error as error:
            raise database_error(error, "scripting the image") from None
        (directory / "schema.json").write_text(schema.model_dump_json(indent=2), "utf-8")
        tables: list[ExtractedTable] = []
        for position, table in enumerate(t.name for t in catalog.tables):
            query = Renderer(as_of=_EPOCH).select(table, text_columns(physical, table), AllRows())
            file = f"{position:05d}.dat"
            try:
                with connection.cursor() as cursor, (directory / file).open("wb") as out:
                    cursor.execute(query.text)
                    rows = write_rows(cursor, out)
            except pymssql.Error as error:
                raise database_error(error, f"writing {table}") from None
            tables.append(extracted_table(physical, table, file, rows))
        (directory / "index.json").write_text(
            ExtractIndex(
                database=info.database, server_version=info.server_version, tables=tuple(tables)
            ).model_dump_json(indent=2),
            "utf-8",
        )

    @contextmanager
    def restore(
        self, image: Path, image_format: ImageFormat, directory: Path
    ) -> Generator[RestoredImage]:
        """Bring an image up in a scratch instance for checks."""
        info = DatabaseInfo.model_validate_json((image / _INFO).read_bytes())
        directory.mkdir(parents=True, exist_ok=True)
        server = MssqlServer(self.tools, directory, directory, (image,))
        if image_format is ImageFormat.PHYSICAL:
            _copy_tree(image / DATA, directory / DATA)
        with server.running():
            if image_format is ImageFormat.PHYSICAL:
                server.attach(info.database)
            else:
                self._load_logical(server, info, image, directory)
            with server.connect(info.database) as connection:
                yield MssqlRestored(connection, introspect(connection)[1])

    @staticmethod
    def _load_logical(server: MssqlServer, info: DatabaseInfo, image: Path, scratch: Path) -> None:
        index = ExtractIndex.model_validate_json((image / "index.json").read_bytes())
        schema = Schema.model_validate_json((image / "schema.json").read_bytes())
        server.create(info.database, schema.collation)
        with server.connect(info.database) as connection:
            _configure(connection, info.database, schema)
            builder = _Builder(connection, schema)
            builder.tables(frozenset(t.table for t in index.tables))
            for table in index.tables:
                _bulk_load(connection, image, table, scratch)
            builder.rest()
            _reseed(connection)
        with suppress(OSError):
            for leftover in scratch.glob("*.fmt"):
                leftover.unlink()
