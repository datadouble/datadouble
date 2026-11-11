# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The scratch side: working databases, anonymization, images and restores.

``load`` builds a group's working database in a fresh cluster: hooks, the
schema's pre-data section, the rows (with triggers off), fallbacks for missing
parents, the post-data section (indexes and constraints, which also check the
data), sequences, and finally dropping the tables that belong to other groups.
``transform`` copies the stopped cluster and anonymizes the copy in one
transaction. ``package`` writes the data directory (physical) or a ``pg_dump``
archive (logical); ``restore`` brings either back up for verification.
"""

import re
import shutil
from collections.abc import Generator, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg import sql
from pydantic import BaseModel, ConfigDict

from datadouble.catalog import Column, ColumnKind
from datadouble.engine import EngineError, RestoredImage, TableCount, Transformation
from datadouble.files import copy_tree
from datadouble.names import ColumnName, ColumnRef, TableName
from datadouble.planner import Fixup, Plan, PlannedGroup, PlannedRule
from datadouble.spec import ExpressionRule, Hook, ImageFormat, NullRule
from datadouble.subset import Edge, EdgeMode
from datadouble_postgres.cluster import Binaries, Cluster
from datadouble_postgres.errors import database_error
from datadouble_postgres.render import Statement, columns, trusted
from datadouble_postgres.source import ExtractIndex

type Connection = psycopg.Connection[tuple[object, ...]]

_DATADIR = "pgdata"
_DUMP = "database.dump"
_INFO = "database.json"
_TOC = re.compile(r"^\d+; \d+ \d+ (.+) \S+$")
_DESCRIPTIONS = ("FK CONSTRAINT", "MATERIALIZED VIEW DATA", "MATERIALIZED VIEW", "VIEW", "COMMENT")
_PLACEHOLDERS: dict[ColumnKind, str] = {
    ColumnKind.TEXT: "",
    ColumnKind.INTEGER: "0",
    ColumnKind.DECIMAL: "0",
    ColumnKind.FLOAT: "0",
    ColumnKind.BOOLEAN: "false",
    ColumnKind.DATE: "epoch",
    ColumnKind.TIMESTAMP: "epoch",
    ColumnKind.TIME: "00:00",
    ColumnKind.UUID: "00000000-0000-0000-0000-000000000000",
    ColumnKind.JSON: "{}",
    ColumnKind.ARRAY: "{}",
    ColumnKind.BINARY: "",
}


class DatabaseInfo(BaseModel):
    """Stored beside a working database and in every image."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    database: str
    server_version: str
    partitioned: tuple[TableName, ...]


def _qualified(name: TableName) -> sql.Identifier:
    return sql.Identifier(name.namespace, name.name)


def _table(name: TableName, partitioned: frozenset[TableName]) -> sql.Composable:
    return _qualified(name) if name in partitioned else sql.SQL("ONLY {}").format(_qualified(name))


def _and(parts: Iterable[sql.Composable]) -> sql.Composed:
    return sql.SQL(" AND ").join(parts)


def _commas(parts: Iterable[sql.Composable]) -> sql.Composed:
    return sql.SQL(", ").join(parts)


def filter_toc(listing: str, drop: frozenset[tuple[str, str, str]]) -> str:
    """Remove ``pg_restore --list`` entries matching ``(description, schema, tag)``.

    Comments on a removed object are removed with it.
    """
    commented = {(schema, f"{description} {tag}") for description, schema, tag in drop}
    kept: list[str] = []
    for line in listing.splitlines():
        match = _TOC.match(line)
        description = (
            next((d for d in _DESCRIPTIONS if match.group(1).startswith(d + " ")), None)
            if match
            else None
        )
        if match is not None and description is not None:
            schema, _, tag = match.group(1)[len(description) + 1 :].partition(" ")
            if (description, schema, tag) in drop or (
                description == "COMMENT" and (schema, tag) in commented
            ):
                continue
        kept.append(line)
    return "\n".join(kept) + "\n"


def placeholder(table: TableName, column: Column) -> sql.Composable:
    """A stand-in value for a required column of a stub row."""
    value = _PLACEHOLDERS.get(column.kind)
    if value is None:
        message = (
            f"cannot stub {table}: no placeholder for {column.name} ({column.type}); "
            "make the column nullable, give it a default, or use another fallback"
        )
        raise EngineError(message)
    return sql.SQL("{}::{}").format(sql.Literal(value), trusted(column.type))


def _execute(connection: Connection, action: str, statement: Statement) -> None:
    try:
        connection.execute(statement)
    except psycopg.Error as error:
        raise database_error(error, action) from None


class PostgresRestored:
    """A database open for checks: a restored image, or a working database."""

    def __init__(self, connection: Connection, partitioned: frozenset[TableName]) -> None:
        """Wrap a connection to the database."""
        self.connection = connection
        self.partitioned = partitioned

    def _scalar(self, action: str, statement: Statement) -> int:
        try:
            row = self.connection.execute(statement).fetchone()
        except psycopg.Error as error:
            raise database_error(error, action) from None
        return int(str(row[0])) if row else 0

    def counts(self, tables: Iterable[TableName]) -> dict[TableName, int]:
        """Rows per table."""
        return {
            table: self._scalar(
                f"counting {table}",
                sql.SQL("SELECT count(*) FROM {}").format(_table(table, self.partitioned)),
            )
            for table in tables
        }

    def orphans(self, edge: Edge) -> int:
        """Child rows with a non-null reference and no parent."""
        present = _and(sql.SQL("c.{} IS NOT NULL").format(sql.Identifier(c)) for c in edge.columns)
        matches = _and(
            (
                sql.SQL("p.{} = c.{}").format(sql.Identifier(p), sql.Identifier(c))
                for p, c in zip(edge.parent_columns, edge.columns, strict=True)
            ),
        )
        when = sql.SQL(" AND ({})").format(trusted(edge.when)) if edge.when else sql.SQL("")
        return self._scalar(
            f"checking {edge.name}",
            sql.SQL(
                "SELECT count(*) FROM {} c WHERE {}{} AND NOT EXISTS (SELECT 1 FROM {} p WHERE {})"
            ).format(
                _table(edge.child, self.partitioned),
                present,
                when,
                _table(edge.parent, self.partitioned),
                matches,
            ),
        )

    def text_values(self, column: ColumnRef) -> Iterator[str]:
        """Distinct non-null values of a column, as text, streamed."""
        statement = sql.SQL("SELECT DISTINCT {}::text FROM {} WHERE {} IS NOT NULL").format(
            sql.Identifier(column.column),
            _table(column.table, self.partitioned),
            sql.Identifier(column.column),
        )
        try:
            with (
                self.connection.transaction(),
                self.connection.cursor(name="dd_values") as cursor,
            ):
                cursor.execute(statement)
                for (value,) in cursor:
                    yield str(value)
        except psycopg.Error as error:
            raise database_error(error, f"scanning {column}") from None

    def failing_rows(self, query: str) -> int:
        """Rows a spec assertion returns."""
        return self._scalar(
            "running an assertion",
            sql.SQL("SELECT count(*) FROM ({}) AS assertion").format(trusted(query)),
        )


class PostgresWorking:
    """Working databases and images under one scratch directory."""

    def __init__(self, binaries: Binaries, scratch: Path) -> None:
        """Use ``binaries`` for every cluster under ``scratch``."""
        self.binaries = binaries
        self.scratch = scratch

    def server_version(self) -> str:
        """The version of the local binaries, which images are built for."""
        return self.binaries.version()

    def release(self, directory: Path) -> None:
        """Stop servers an interrupted attempt left running under ``directory``."""
        for pid_file in directory.rglob("postmaster.pid"):
            Cluster(self.binaries, pid_file.parent).stop_abandoned()

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
        partitioned = frozenset(index.partitioned)
        members = frozenset(group.tables)
        cluster = Cluster(self.binaries, directory / _DATADIR)
        cluster.initialize()
        dump = extract / "schema.dump"
        listing = self.binaries.run("pg_restore", ["--list", str(dump)])
        views = frozenset(
            (kind, view.namespace, view.name)
            for view in index.omitted_views
            for kind in ("VIEW", "MATERIALIZED VIEW", "MATERIALIZED VIEW DATA")
        )
        # A constraint whose table is in the group but whose target is not (in
        # another group, or excluded) would fail on the rows: leave it out.
        foreign = frozenset(
            ("FK CONSTRAINT", constraint.namespace, f"{constraint.table} {constraint.name}")
            for constraint in index.constraints
            if constraint.child in members and constraint.parent not in members
        )
        with cluster.running():
            cluster.create_database(index.database)
            with cluster.connect(index.database) as connection:
                for hook in hooks:
                    _execute(connection, "a before_load hook", trusted(hook.sql))
            self._restore(cluster, index.database, dump, "pre-data", filter_toc(listing, views))
            with cluster.connect(index.database) as connection, connection.transaction():
                connection.execute("SET LOCAL session_replication_role = replica")
                for table in index.tables:
                    if table.table in members:
                        self._copy_in(connection, extract / table.file, table.table, table.columns)
                for fixup in group.fixups:
                    self._fixup(connection, plan, index, fixup)
            self._restore(
                cluster, index.database, dump, "post-data", filter_toc(listing, views | foreign)
            )
            with cluster.connect(index.database) as connection:
                self._sequences(connection)
                for table in index.tables:
                    if table.table not in members:
                        _execute(
                            connection,
                            f"dropping {table.table}",
                            sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(
                                _qualified(table.table)
                            ),
                        )
                counts = PostgresRestored(connection, partitioned).counts(group.tables)
        info = DatabaseInfo(
            database=index.database,
            server_version=self.binaries.version(),
            partitioned=index.partitioned,
        )
        (directory / _INFO).write_text(info.model_dump_json(indent=2), "utf-8")
        return tuple(TableCount(table=t, rows=n) for t, n in counts.items())

    def _restore(self, cluster: Cluster, database: str, dump: Path, section: str, toc: str) -> None:
        listing = cluster.datadir.parent / f"{section}.list"
        listing.write_text(toc, "utf-8")
        try:
            self.binaries.run(
                "pg_restore",
                [
                    f"--section={section}",
                    f"--use-list={listing}",
                    "--no-owner",
                    "--no-privileges",
                    "--exit-on-error",
                    f"--dbname={cluster.conninfo(database)}",
                    str(dump),
                ],
            )
        finally:
            listing.unlink(missing_ok=True)

    @staticmethod
    def _copy_in(
        connection: Connection, path: Path, table: TableName, names: tuple[ColumnName, ...]
    ) -> None:
        statement = sql.SQL("COPY {} ({}) FROM STDIN").format(_qualified(table), columns(names))
        try:
            with (
                connection.cursor() as cursor,
                cursor.copy(statement) as copy,
                path.open("rb") as data,
            ):
                while chunk := data.read(1 << 20):
                    copy.write(chunk)
        except psycopg.Error as error:
            raise database_error(error, f"loading {table}") from None

    def _fixup(self, connection: Connection, plan: Plan, index: ExtractIndex, fixup: Fixup) -> None:
        """Null or stub references whose parent rows are missing."""
        edge = fixup.edge
        conditions = [sql.SQL("c.{} IS NOT NULL").format(sql.Identifier(c)) for c in edge.columns]
        if edge.when:
            conditions.append(sql.SQL("({})").format(trusted(edge.when)))
        if fixup.parent_present:
            conditions.append(
                sql.SQL("NOT EXISTS (SELECT 1 FROM {} p WHERE {})").format(
                    _table(edge.parent, frozenset(index.partitioned)),
                    _and(
                        (
                            sql.SQL("p.{} = c.{}").format(sql.Identifier(p), sql.Identifier(c))
                            for p, c in zip(edge.parent_columns, edge.columns, strict=True)
                        ),
                    ),
                )
            )
        where = _and(conditions)
        child = _qualified(edge.child)
        if fixup.mode is EdgeMode.NULL:
            assignments = _commas(
                sql.SQL("{} = NULL").format(sql.Identifier(c)) for c in edge.columns
            )
            statement: Statement = sql.SQL("UPDATE {} c SET {} WHERE {}").format(
                child, assignments, where
            )
        else:
            statement = self._stub(plan, index, edge, child, where)
        _execute(connection, f"applying the fallback for {edge.name}", statement)

    @staticmethod
    def _stub(
        plan: Plan, index: ExtractIndex, edge: Edge, child: sql.Composable, where: sql.Composable
    ) -> Statement:
        # The planner only allows stubs into tables the image has; if one is
        # missing anyway, the INSERT fails and the database says why.
        parent = plan.catalog.table(edge.parent)
        extracted = index.table(edge.parent)
        columns = parent.columns if parent else ()
        copied = extracted.columns if extracted else ()
        fillers = [
            (column.name, placeholder(edge.parent, column))
            for column in columns
            if column.name not in edge.parent_columns
            and not column.nullable
            and column.name in copied
        ]
        return sql.SQL("INSERT INTO {} ({}) SELECT DISTINCT {} FROM {} c WHERE {}").format(
            _qualified(edge.parent),
            _commas(sql.Identifier(n) for n in [*edge.parent_columns, *(n for n, _ in fillers)]),
            _commas(
                [
                    *(sql.SQL("c.{}").format(sql.Identifier(c)) for c in edge.columns),
                    *(value for _, value in fillers),
                ],
            ),
            child,
            where,
        )

    @staticmethod
    def _sequences(connection: Connection) -> None:
        """Move every column-owned sequence past the loaded rows."""
        rows = connection.execute(
            """
            SELECT n.nspname, c.relname, a.attname,
                   pg_catalog.pg_get_serial_sequence(
                       pg_catalog.format('%I.%I', n.nspname, c.relname), a.attname)
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_catalog.pg_attribute a
              ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
            WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
            """
        ).fetchall()
        for schema, table, column, sequence in rows:
            if sequence is None:
                continue
            name = sql.Identifier(str(column))
            _execute(
                connection,
                f"setting {sequence}",
                sql.SQL(
                    "SELECT pg_catalog.setval({}, coalesce(max({}), 1), max({}) IS NOT NULL) "
                    "FROM {}"
                ).format(
                    sql.Literal(str(sequence)), name, name, sql.Identifier(str(schema), str(table))
                ),
            )

    # transform

    def transform(
        self,
        group: PlannedGroup,
        source: Path,
        directory: Path,
        transformation: Transformation,
    ) -> tuple[TableCount, ...]:
        """Copy the working database and anonymize the copy in one transaction."""
        directory.parent.mkdir(parents=True, exist_ok=True)
        _copy_tree(source, directory)
        info = DatabaseInfo.model_validate_json((directory / _INFO).read_bytes())
        partitioned = frozenset(info.partitioned)
        cluster = Cluster(self.binaries, directory / _DATADIR)
        with cluster.running(), cluster.connect(info.database) as connection:
            with connection.transaction():
                connection.execute("SET LOCAL session_replication_role = replica")
                by_table: dict[TableName, list[PlannedRule]] = {}
                for rule in transformation.rules:
                    by_table.setdefault(rule.column.table, []).append(rule)
                for position, (table, rules) in enumerate(by_table.items()):
                    self._anonymize(
                        connection, (position, table), rules, transformation, partitioned
                    )
                for hook in transformation.hooks:
                    _execute(connection, "an after_transform hook", trusted(hook.sql))
            counts = PostgresRestored(connection, partitioned).counts(group.tables)
        return tuple(TableCount(table=t, rows=n) for t, n in counts.items())

    def _anonymize(
        self,
        connection: Connection,
        numbered: tuple[int, TableName],
        rules: list[PlannedRule],
        transformation: Transformation,
        partitioned: frozenset[TableName],
    ) -> None:
        """Apply every rule for one table in a single UPDATE."""
        position, table = numbered
        assignments: list[sql.Composable] = []
        for number, rule in enumerate(rules):
            column = sql.Identifier(rule.column.column)
            match rule.rule:
                case NullRule():
                    assignments.append(sql.SQL("{} = NULL").format(column))
                case ExpressionRule(expression=expression, where=where):
                    value = sql.SQL("({})").format(trusted(expression))
                    if where:
                        value = sql.SQL("CASE WHEN ({}) THEN {} ELSE {} END").format(
                            trusted(where), value, column
                        )
                    assignments.append(sql.SQL("{} = {}").format(column, value))
                case _:
                    name = sql.Identifier(f"dd_map_{position}_{number}")
                    self._replacements(connection, name, rule, transformation, partitioned)
                    assignments.append(
                        sql.SQL(
                            "{} = (SELECT m.new FROM {} m WHERE m.old = t.{}::text)::{}"
                        ).format(column, name, column, trusted(rule.type))
                    )
        update = sql.SQL("UPDATE {} AS t SET {}").format(
            _table(table, partitioned), _commas(assignments)
        )
        _execute(connection, f"anonymizing {table}", update)

    @staticmethod
    def _replacements(
        connection: Connection,
        name: sql.Identifier,
        rule: PlannedRule,
        transformation: Transformation,
        partitioned: frozenset[TableName],
    ) -> None:
        """Fill a temporary table mapping each original value to its replacement."""
        column = sql.Identifier(rule.column.column)
        distinct = sql.SQL("SELECT DISTINCT {}::text FROM {} WHERE {} IS NOT NULL").format(
            column, _table(rule.column.table, partitioned), column
        )
        try:
            values = [str(row[0]) for row in connection.execute(distinct)]
        except psycopg.Error as error:
            raise database_error(error, f"reading {rule.column}") from None
        mapping = transformation.mapper(rule, values)
        create = sql.SQL(
            "CREATE TEMPORARY TABLE {} (old text PRIMARY KEY, new text) ON COMMIT DROP"
        ).format(name)
        _execute(connection, "preparing replacements", create)
        statement = sql.SQL("COPY {} (old, new) FROM STDIN").format(name)
        with connection.cursor() as cursor, cursor.copy(statement) as copy:
            for old, new in mapping.items():
                copy.write_row((old, new))

    # package and restore

    def package(self, source: Path, image_format: ImageFormat, directory: Path) -> None:
        """Write one format of the image into ``directory``."""
        info = DatabaseInfo.model_validate_json((source / _INFO).read_bytes())
        shutil.copyfile(source / _INFO, directory / _INFO)
        if image_format is ImageFormat.PHYSICAL:
            _copy_tree(source / _DATADIR, directory / _DATADIR)
            return
        cluster = Cluster(self.binaries, source / _DATADIR)
        with cluster.running():
            self.binaries.run(
                "pg_dump",
                [
                    "--format=custom",
                    "--no-owner",
                    "--no-privileges",
                    f"--file={directory / _DUMP}",
                    f"--dbname={cluster.conninfo(info.database)}",
                ],
            )

    @contextmanager
    def restore(
        self, image: Path, image_format: ImageFormat, directory: Path
    ) -> Generator[RestoredImage]:
        """Bring an image up in a scratch cluster for checks."""
        info = DatabaseInfo.model_validate_json((image / _INFO).read_bytes())
        datadir = directory / _DATADIR
        directory.mkdir(parents=True, exist_ok=True)
        cluster = Cluster(self.binaries, datadir)
        if image_format is ImageFormat.PHYSICAL:
            _copy_tree(image / _DATADIR, datadir)
            datadir.chmod(0o700)
        else:
            cluster.initialize()
        with cluster.running():
            if image_format is ImageFormat.LOGICAL:
                cluster.create_database(info.database)
                self.binaries.run(
                    "pg_restore",
                    [
                        "--no-owner",
                        "--no-privileges",
                        "--exit-on-error",
                        f"--dbname={cluster.conninfo(info.database)}",
                        str(image / _DUMP),
                    ],
                )
            with cluster.connect(info.database) as connection:
                yield PostgresRestored(connection, frozenset(info.partitioned))


def _copy_tree(source: Path, destination: Path) -> None:
    """Copy a directory, sharing blocks where the filesystem allows."""
    problem = copy_tree(source, destination)
    if problem:
        message = f"cannot copy {source.name}: {problem}"
        raise EngineError(message)
