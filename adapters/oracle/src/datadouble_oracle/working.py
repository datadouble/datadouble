# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The scratch side: working databases, anonymization, images and restores.

``load`` builds a group's working database as a new pluggable database: a
user without a password for each schema, ``before_load`` hooks, object types
and sequences, the group's tables, the rows (identity columns taking the
extracted values), fallbacks for missing parents, then indexes, foreign keys,
views and PL/SQL, and triggers last, so loading fires none; the schemas are
recompiled at the end. Only the group's tables are created; a foreign key or
view that needs a table the database does not have is left out.
``transform`` copies the unplugged files, plugs the copy in and anonymizes it
with its triggers disabled, the rules and hooks in one transaction.
``package`` writes the datafiles and manifest (physical) or the schema and
rows in extract's format (logical); ``restore`` plugs or loads them.
"""

import datetime as dt
import shutil
import struct
from collections.abc import Generator, Iterable, Iterator
from contextlib import contextmanager
from itertools import batched
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from datadouble.catalog import Column, ColumnKind
from datadouble.engine import EngineError, RestoredImage, TableCount, Transformation
from datadouble.files import copy_tree
from datadouble.names import ColumnRef, TableName
from datadouble.planner import Fixup, Plan, PlannedGroup, PlannedRule
from datadouble.spec import ExpressionRule, Hook, ImageFormat, NullRule
from datadouble.subset import AllRows, Edge, EdgeMode
from datadouble_oracle.driver import Connection, Error, Value
from datadouble_oracle.errors import database_error
from datadouble_oracle.introspect import Physical, binds, introspect
from datadouble_oracle.render import (
    OUTER,
    Param,
    Renderer,
    as_of_value,
    columns,
    compose,
    qualified,
    quote,
    uses_as_of,
)
from datadouble_oracle.script import Phase, Schema, Statement
from datadouble_oracle.server import DATA, OracleServer, Tools
from datadouble_oracle.source import (
    ExtractedTable,
    ExtractIndex,
    as_text,
    extracted_table,
    from_text,
    text_columns,
    write_rows,
)

_INFO = "database.json"
_BATCH = 1000
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
_PLACEHOLDERS: dict[ColumnKind, str] = {
    ColumnKind.TEXT: "' '",
    ColumnKind.INTEGER: "0",
    ColumnKind.DECIMAL: "0",
    ColumnKind.FLOAT: "0",
    ColumnKind.TIMESTAMP: "DATE '1970-01-02'",
    ColumnKind.JSON: "'{}'",
    ColumnKind.BINARY: "HEXTORAW('00')",
}


class DatabaseInfo(BaseModel):
    """Stored beside a working database and in every image."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    schemas: tuple[str, ...]
    server_version: str
    unplugged_from: str
    """Where the datafiles were when the database was unplugged, as the manifest names them."""


def placeholder(table: TableName, column: Column) -> str:
    """A stand-in value for a required column of a stub row.

    Oracle stores an empty string as NULL, so text gets a space.
    """
    value = _PLACEHOLDERS.get(column.kind)
    if value is None:
        message = (
            f"cannot stub {table}: no placeholder for {column.name} ({column.type}); "
            "make the column nullable, give it a default, or use another fallback"
        )
        raise EngineError(message)
    return value


def read_rows(path: Path, width: int) -> Iterator[list[str | None]]:
    """The rows of a data file written by ``write_rows``."""
    with path.open("rb") as data:
        while True:
            row: list[str | None] = []
            for _ in range(width):
                prefix = data.read(4)
                if not prefix:
                    return
                (length,) = struct.unpack("<i", prefix)
                row.append(None if length < 0 else data.read(length).decode("utf-8"))
            yield row


def _execute(
    connection: Connection, action: str, statement: str, params: dict[str, Param] | None = None
) -> None:
    try:
        with connection.cursor() as cursor:
            cursor.execute(statement, params or {})
    except Error as error:
        raise database_error(error, action) from None


def _rows(
    connection: Connection, action: str, statement: str, params: dict[str, Param] | None = None
) -> list[tuple[object, ...]]:
    try:
        with connection.cursor() as cursor:
            cursor.execute(statement, params or {})
            return [tuple(row) for row in cursor.fetchall()]
    except Error as error:
        raise database_error(error, action) from None


def _matches(edge: Edge) -> str:
    return " AND ".join(
        compose("p.{} = c.{}", quote(p), quote(c))
        for p, c in zip(edge.parent_columns, edge.columns, strict=True)
    )


class OracleRestored:
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
                f"counting {table}", compose("SELECT COUNT(*) FROM {}", qualified(table))
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
                "SELECT COUNT(*) FROM {} c WHERE {}{} AND NOT EXISTS (SELECT 1 FROM {} p WHERE {})",
                qualified(edge.child),
                present,
                when,
                qualified(edge.parent),
                _matches(edge),
            ),
        )

    def text_values(self, column: ColumnRef) -> Iterator[str]:
        """Distinct non-null values of a column, as text, streamed."""
        family = self.physical.types.get(column.table, {}).get(column.column, "VARCHAR2")
        name = columns([column.column], OUTER)
        text = as_text(name, family)
        if family in {"CLOB", "NCLOB", "JSON"}:
            # LOBs cannot be compared in DISTINCT; their first 4000 characters can.
            text = compose("DBMS_LOB.SUBSTR({}, 4000, 1)", text)
        statement = compose(
            "SELECT DISTINCT {} FROM {} {} WHERE {} IS NOT NULL",
            text,
            qualified(column.table),
            OUTER,
            name,
        )
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(statement)
                for (value,) in cursor:
                    yield str(value)
        except Error as error:
            raise database_error(error, f"scanning {column}") from None

    def failing_rows(self, query: str) -> int:
        """Rows a spec assertion returns, counted as they stream."""
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(query)
                return sum(1 for _ in cursor)
        except Error as error:
            raise database_error(error, "running an assertion") from None


def use_schemas(connection: Connection, schemas: tuple[str, ...]) -> Connection:
    """``connection``, resolving unqualified names in the first schema, as spec SQL expects."""
    _execute(
        connection,
        "changing schema",
        compose("ALTER SESSION SET CURRENT_SCHEMA = {}", quote(schemas[0])),
    )
    return connection


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

    def users(self) -> None:
        """A user without a password for each schema, owning its objects."""
        for owner in self.schema.schemas:
            _execute(
                self.connection,
                f"creating schema {owner}",
                compose(
                    "CREATE USER {} NO AUTHENTICATION QUOTA UNLIMITED ON {}",
                    quote(owner),
                    quote("DATADOUBLE_DATA"),
                ),
            )

    def run(self, phase: Phase, tables: frozenset[TableName] | None = None) -> None:
        """Run a phase's statements whose table and needs exist.

        With ``tables``, a statement's table must be one of them; without, it
        must have been created already.
        """
        for statement in self.schema.phase(phase):
            if self._wanted(statement, tables):
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
        _execute(
            self.connection,
            "changing schema",
            compose("ALTER SESSION SET CURRENT_SCHEMA = {}", quote(statement.owner)),
        )
        _execute(self.connection, f"creating {statement.name}", statement.sql)

    def tables(self, tables: frozenset[TableName]) -> None:
        """Create the object types, sequences and ``tables``."""
        self.run(Phase.SCHEMA)
        self.run(Phase.TABLE, tables)

    def rest(self) -> None:
        """Everything after the rows: indexes, foreign keys, views, PL/SQL and triggers."""
        for phase in (Phase.INDEX, Phase.FOREIGN_KEY, Phase.MODULE, Phase.TRIGGER):
            self.run(phase)
        for owner in self.schema.schemas:
            _execute(
                self.connection,
                f"compiling {owner}",
                "BEGIN DBMS_UTILITY.COMPILE_SCHEMA(:owner, FALSE); END;",
                {"owner": owner},
            )


def _load_table(connection: Connection, extract: Path, table: ExtractedTable) -> None:
    """Insert one table's rows from its data file, in batches."""
    if table.rows == 0:
        return
    binds = [from_text(f":{i + 1}", family) for i, family in enumerate(table.types)]
    statement = compose(
        "INSERT INTO {} ({}) VALUES ({})",
        qualified(table.table),
        columns(table.columns),
        ", ".join(binds),
    )
    blobs = {i for i, family in enumerate(table.types) if family == "BLOB"}
    try:
        with connection.cursor() as cursor:
            rows = read_rows(extract / table.file, len(table.columns))
            for batch in batched(rows, _BATCH, strict=False):
                values: list[list[Value]] = [
                    [
                        bytes.fromhex(value) if i in blobs and value is not None else value
                        for i, value in enumerate(row)
                    ]
                    for row in batch
                ]
                cursor.executemany(statement, values)
    except Error as error:
        raise database_error(error, f"loading {table.table}") from None


def _identities(
    connection: Connection, schema: Schema, members: frozenset[TableName], *, loading: bool
) -> None:
    """Let identity columns take loaded values, or restore them to start after the rows."""
    for identity in schema.identities:
        if identity.table not in members:
            continue
        generation = "BY DEFAULT" if loading else identity.generation
        restart = "" if loading else " (START WITH LIMIT VALUE)"
        _execute(
            connection,
            f"configuring the identity of {identity.table}",
            compose(
                "ALTER TABLE {} MODIFY ({} GENERATED {} AS IDENTITY{})",
                qualified(identity.table),
                quote(identity.column),
                generation,
                restart,
            ),
        )


class OracleWorking:
    """Working databases and images under one scratch directory."""

    def __init__(self, tools: Tools, scratch: Path) -> None:
        """Use ``tools`` for every container under ``scratch``."""
        self.tools = tools
        self.scratch = scratch
        self._version: str | None = None

    def server_version(self) -> str:
        """The version of the image's server, which images are built for."""
        if self._version is None:
            directory = self.scratch / ".version"
            directory.mkdir(parents=True, exist_ok=True)
            try:
                with OracleServer(self.tools, directory, self.scratch).running() as server:
                    self._version = server.version()
            finally:
                shutil.rmtree(directory, ignore_errors=True)
        return self._version

    def _check_version(self, server: OracleServer, source_version: str) -> str:
        version = server.version()
        if version != source_version:
            message = (
                f"the working database image {self.tools.image} runs Oracle {version}, but the"
                f" source runs {source_version}; set source.image to an Oracle Database Free"
                " image of the source's version"
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
        server = OracleServer(self.tools, directory, directory, (extract,))
        with server.running():
            version = self._check_version(server, index.server_version)
            server.create()
            with server.connect() as connection:
                builder = _Builder(connection, schema)
                builder.users()
                use_schemas(connection, index.schemas)
                for hook in hooks:
                    _execute(connection, "a before_load hook", hook.sql)
                builder.tables(members)
                _identities(connection, schema, members, loading=True)
                for table in index.tables:
                    if table.table in members:
                        _load_table(connection, extract, table)
                for fixup in group.fixups:
                    self._fixup(connection, plan, index, fixup)
                connection.commit()
                _identities(connection, schema, members, loading=False)
                builder.rest()
                use_schemas(connection, index.schemas)
                counts = OracleRestored(connection, Physical()).counts(group.tables)
            unplugged = server.unplug()
        info = DatabaseInfo(schemas=index.schemas, server_version=version, unplugged_from=unplugged)
        (directory / _INFO).write_text(info.model_dump_json(indent=2), "utf-8")
        # The logical image is this schema and the rows; transform copies it along.
        shutil.copyfile(extract / "schema.json", directory / "schema.json")
        return tuple(TableCount(table=t, rows=n) for t, n in counts.items())

    @staticmethod
    def _fixup(connection: Connection, plan: Plan, index: ExtractIndex, fixup: Fixup) -> None:
        """Null or stub references whose parent rows are missing."""
        edge = fixup.edge
        conditions = [compose("c.{} IS NOT NULL", quote(c)) for c in edge.columns]
        if edge.when:
            conditions.append(f"({edge.when})")
        if fixup.parent_present:
            conditions.append(
                compose(
                    "NOT EXISTS (SELECT 1 FROM {} p WHERE {})",
                    qualified(edge.parent),
                    _matches(edge),
                )
            )
        where = " AND ".join(conditions)
        if fixup.mode is EdgeMode.NULL:
            statement = compose(
                "UPDATE {} c SET {} WHERE {}",
                qualified(edge.child),
                ", ".join(compose("c.{} = NULL", quote(c)) for c in edge.columns),
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
                "INSERT INTO {} ({}) SELECT DISTINCT {} FROM {} c WHERE {}",
                qualified(edge.parent),
                columns([*edge.parent_columns, *(name for name, _ in fillers)]),
                ", ".join(
                    [*(compose("c.{}", quote(c)) for c in edge.columns), *(v for _, v in fillers)]
                ),
                qualified(edge.child),
                where,
            )
        params: dict[str, Param] = (
            {"as_of": as_of_value(plan.as_of)} if uses_as_of(statement) else {}
        )
        _execute(connection, f"applying the fallback for {edge.name}", statement, params)

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
        server = OracleServer(self.tools, directory, directory)
        with server.running():
            server.plug(info.unplugged_from)
            with server.connect() as connection:
                use_schemas(connection, info.schemas)
                physical = introspect(connection, info.schemas)[1]
                # Only the enabled triggers, by name: a table's ENABLE ALL
                # TRIGGERS would also enable the ones the source has disabled.
                enabled = self._enabled_triggers(connection, info.schemas)
                for owner, trigger in enabled:
                    _execute(
                        connection,
                        f"disabling the trigger {owner}.{trigger}",
                        compose("ALTER TRIGGER {}.{} DISABLE", quote(owner), quote(trigger)),
                    )
                _prepare(connection)
                by_table: dict[TableName, list[PlannedRule]] = {}
                for rule in transformation.rules:
                    by_table.setdefault(rule.column.table, []).append(rule)
                anonymizer = _Anonymizer(connection, physical, transformation)
                for table, rules in by_table.items():
                    anonymizer.table(table, rules)
                for hook in transformation.hooks:
                    _execute(connection, "an after_transform hook", hook.sql)
                connection.commit()
                _clean_up(connection)
                for owner, trigger in enabled:
                    _execute(
                        connection,
                        f"enabling the trigger {owner}.{trigger}",
                        compose("ALTER TRIGGER {}.{} ENABLE", quote(owner), quote(trigger)),
                    )
                counts = OracleRestored(connection, physical).counts(group.tables)
            unplugged = server.unplug()
        updated = info.model_copy(update={"unplugged_from": unplugged})
        (directory / _INFO).write_text(updated.model_dump_json(indent=2), "utf-8")
        return tuple(TableCount(table=t, rows=n) for t, n in counts.items())

    @staticmethod
    def _enabled_triggers(
        connection: Connection, schemas: tuple[str, ...]
    ) -> list[tuple[str, str]]:
        """The schemas' enabled table triggers, as owner and name."""
        rows = _rows(
            connection,
            "listing triggers",
            compose(
                "SELECT owner, trigger_name FROM all_triggers WHERE owner IN ({})"
                " AND status = 'ENABLED' AND base_object_type = 'TABLE' ORDER BY 1, 2",
                binds(len(schemas)),
            ),
            {f"s{i}": schema for i, schema in enumerate(schemas)},
        )
        return [(str(owner), str(trigger)) for owner, trigger in rows]

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
        server = OracleServer(self.tools, scratch, directory, (directory,))
        try:
            with server.running():
                server.plug(info.unplugged_from)
                with server.connect() as connection:
                    self._write_logical(connection, info, source, directory)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    @staticmethod
    def _write_logical(
        connection: Connection, info: DatabaseInfo, source: Path, directory: Path
    ) -> None:
        """The schema the working database was built from, and every table's rows."""
        catalog, physical = introspect(connection, info.schemas)
        shutil.copyfile(source / "schema.json", directory / "schema.json")
        tables: list[ExtractedTable] = []
        for position, table in enumerate(t.name for t in catalog.tables):
            query = Renderer(as_of=_EPOCH).select(table, text_columns(physical, table), AllRows())
            file = f"{position:05d}.dat"
            with connection.cursor() as cursor, (directory / file).open("wb") as out:
                cursor.execute(query.text)
                rows = write_rows(cursor, out)
            tables.append(extracted_table(physical, table, file, rows))
        (directory / "index.json").write_text(
            ExtractIndex(
                schemas=info.schemas, server_version=info.server_version, tables=tuple(tables)
            ).model_dump_json(indent=2),
            "utf-8",
        )

    @contextmanager
    def restore(
        self, image: Path, image_format: ImageFormat, directory: Path
    ) -> Generator[RestoredImage]:
        """Bring an image up in a scratch container for checks."""
        info = DatabaseInfo.model_validate_json((image / _INFO).read_bytes())
        directory.mkdir(parents=True, exist_ok=True)
        server = OracleServer(self.tools, directory, directory, (image,))
        if image_format is ImageFormat.PHYSICAL:
            _copy_tree(image / DATA, directory / DATA)
        with server.running():
            if image_format is ImageFormat.PHYSICAL:
                server.plug(info.unplugged_from)
            else:
                self._load_logical(server, image)
            with server.connect() as connection:
                use_schemas(connection, info.schemas)
                yield OracleRestored(connection, introspect(connection, info.schemas)[1])

    @staticmethod
    def _load_logical(server: OracleServer, image: Path) -> None:
        index = ExtractIndex.model_validate_json((image / "index.json").read_bytes())
        schema = Schema.model_validate_json((image / "schema.json").read_bytes())
        members = frozenset(t.table for t in index.tables)
        server.create()
        with server.connect() as connection:
            builder = _Builder(connection, schema)
            builder.users()
            builder.tables(members)
            _identities(connection, schema, members, loading=True)
            for table in index.tables:
                _load_table(connection, image, table)
            connection.commit()
            _identities(connection, schema, members, loading=False)
            builder.rest()


_REPLACEMENTS = "DATADOUBLE_WORK"
"""A schema the transform makes for its replacements table, and drops afterwards."""
_LIMIT = 4000
"""Bytes of an original value or a replacement, as the replacements table holds them."""


def _prepare(connection: Connection) -> None:
    """The replacements table, made before the transformation's transaction.

    Oracle commits on every DDL statement, so nothing is created inside it.
    """
    _execute(
        connection,
        "preparing replacements",
        compose(
            "CREATE USER {} NO AUTHENTICATION QUOTA UNLIMITED ON {}",
            quote(_REPLACEMENTS),
            quote("DATADOUBLE_DATA"),
        ),
    )
    _execute(
        connection,
        "preparing replacements",
        compose(
            "CREATE GLOBAL TEMPORARY TABLE {}.{} (dd_rule NUMBER, dd_old VARCHAR2({} BYTE),"
            " dd_new VARCHAR2({} BYTE), PRIMARY KEY (dd_rule, dd_old)) ON COMMIT DELETE ROWS",
            quote(_REPLACEMENTS),
            quote("MAP"),
            str(_LIMIT),
            str(_LIMIT),
        ),
    )


def _clean_up(connection: Connection) -> None:
    _execute(
        connection,
        "removing replacements",
        compose("DROP USER {} CASCADE", quote(_REPLACEMENTS)),
    )


class _Anonymizer:
    """Applies a transformation's column rules to one database."""

    def __init__(
        self, connection: Connection, physical: Physical, transformation: Transformation
    ) -> None:
        self.connection = connection
        self.physical = physical
        self.transformation = transformation
        self.map = f"{quote(_REPLACEMENTS)}.{quote('MAP')}"

    def table(self, table: TableName, rules: list[PlannedRule]) -> None:
        """Apply every rule for one table in a single UPDATE.

        Value rules look each original value up in the replacements table.
        """
        types = self.physical.types.get(table, {})
        assignments: list[str] = []
        for number, rule in enumerate(rules):
            column = compose("t.{}", quote(rule.column.column))
            family = types.get(rule.column.column, "VARCHAR2")
            match rule.rule:
                case NullRule():
                    assignments.append(compose("{} = NULL", column))
                case ExpressionRule(expression=expression, where=where):
                    value = f"({expression})"
                    if where:
                        value = compose("CASE WHEN ({}) THEN {} ELSE {} END", where, value, column)
                    assignments.append(compose("{} = {}", column, value))
                case _:
                    self._replacements(number, rule, family)
                    lookup = compose(
                        "(SELECT m.dd_new FROM {} m WHERE m.dd_rule = {} AND m.dd_old = {})",
                        self.map,
                        str(number),
                        as_text(column, family),
                    )
                    assignments.append(
                        compose(
                            "{} = CASE WHEN {} IS NULL THEN NULL ELSE {} END",
                            column,
                            column,
                            from_text(lookup, family),
                        )
                    )
        update = compose("UPDATE {} t SET {}", qualified(table), ", ".join(assignments))
        _execute(self.connection, f"anonymizing {table}", update)
        _execute(self.connection, "clearing replacements", compose("DELETE FROM {}", self.map))

    def _replacements(self, number: int, rule: PlannedRule, family: str) -> None:
        """Fill the replacements table with each original value's replacement."""
        name = columns([rule.column.column], OUTER)
        distinct = compose(
            "SELECT DISTINCT {} FROM {} {} WHERE {} IS NOT NULL",
            as_text(name, family),
            qualified(rule.column.table),
            OUTER,
            name,
        )
        read = _rows(self.connection, f"reading {rule.column}", distinct)
        mapping = self.transformation.mapper(rule, [str(row[0]) for row in read])
        if any(
            len(old.encode()) > _LIMIT or len(new.encode()) > _LIMIT for old, new in mapping.items()
        ):
            message = (
                f"{rule.column} holds values longer than {_LIMIT} bytes, which a value rule cannot"
                " replace; use an expression or null rule"
            )
            raise EngineError(message)
        try:
            with self.connection.cursor() as cursor:
                cursor.executemany(
                    compose(
                        "INSERT INTO {} (dd_rule, dd_old, dd_new) VALUES ({}, :1, :2)",
                        self.map,
                        str(number),
                    ),
                    [[old, new] for old, new in mapping.items()],
                )
        except Error as error:
            raise database_error(error, "preparing replacements") from None
