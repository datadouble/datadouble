# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The scratch side: working copies in SQLite, anonymization, images and restores.

A working copy is a SQLite database with a table per object and a column per
field, built from the extract's JSON lines, and ``objects.json`` describing
it. Salesforce runs nowhere locally, so the copy needs none of the org's
packages: a package's objects and fields are ordinary tables and columns
here. ``transform`` copies the database and applies every rule for an object
in one ``UPDATE``, all in one transaction. The image is logical: each
object's records as JSON lines, with ``objects.json``. Restoring one builds
a SQLite database from it again, where checks run as SOQL.
"""

import json
import shutil
import sqlite3
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping
from contextlib import closing, contextmanager
from pathlib import Path

from pydantic import JsonValue

from datadouble.catalog import ColumnKind, Requirement
from datadouble.engine import EngineError, RestoredImage, TableCount, Transformation
from datadouble.names import ColumnRef, TableName
from datadouble.planner import Fixup, Plan, PlannedGroup, PlannedRule
from datadouble.spec import ExpressionRule, Hook, ImageFormat, NullRule
from datadouble.subset import Edge, EdgeMode
from datadouble_salesforce.soql import SoqlError, translate
from datadouble_salesforce.source import ID, RECORDS, ObjectRecords, Objects, StoredColumn

LAYOUT = "1.0"
"""The copies' layout version, which images are identified by."""
DATABASE = "org.sqlite"
OBJECTS = "objects.json"
_AFFINITY = {
    ColumnKind.BOOLEAN: "INTEGER",
    ColumnKind.INTEGER: "INTEGER",
    ColumnKind.FLOAT: "REAL",
    ColumnKind.DECIMAL: "NUMERIC",
}


def _name(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _records(path: Path) -> Iterator[dict[str, JsonValue]]:
    with path.open(encoding="utf-8") as opened:
        for line in opened:
            record: dict[str, JsonValue] = json.loads(line)
            yield record


def _stored(value: JsonValue) -> object:
    return json.dumps(value) if isinstance(value, list | dict) else value


def _json(value: object, column: StoredColumn) -> JsonValue:
    if value is None:
        return None
    match column.kind:
        case ColumnKind.BOOLEAN:
            return bool(value)
        case ColumnKind.INTEGER:
            return int(str(value))
        case ColumnKind.FLOAT:
            return float(str(value))
        case _:
            return str(value)


def build(path: Path, objects: Objects, files: Path) -> None:
    """A SQLite database at ``path`` holding the records of ``objects`` from ``files``."""
    with closing(sqlite3.connect(path)) as connection, connection:
        for found in objects.objects:
            columns = ", ".join(
                f"{_name(c.name)} {_AFFINITY.get(c.kind, 'TEXT')}"
                + (" PRIMARY KEY" if c.name == ID else "")
                for c in found.columns
            )
            connection.execute(f"CREATE TABLE {_name(found.table.name)} ({columns})")
            names = [c.name for c in found.columns]
            insert = (
                f"INSERT INTO {_name(found.table.name)} ({', '.join(map(_name, names))})"
                f" VALUES ({', '.join('?' for _ in names)})"
            )
            connection.executemany(
                insert,
                (
                    tuple(_stored(record.get(name)) for name in names)
                    for record in _records(files / f"{found.table.name}.jsonl")
                ),
            )


class SalesforceRestored:
    """A copy open for checks: a working copy or a restored image."""

    def __init__(self, connection: sqlite3.Connection, objects: Objects) -> None:
        """Check the database ``connection`` holds, described by ``objects``."""
        self.connection = connection
        self.objects = {o.table.name.lower(): o for o in objects.objects}

    def _fields(self, name: str) -> Mapping[str, str] | None:
        found = self.objects.get(name.lower())
        return None if found is None else {c.name.lower(): c.name for c in found.columns}

    def counts(self, tables: Iterable[TableName]) -> dict[TableName, int]:
        """Records in each object; none for objects the copy does not hold."""
        counted: dict[TableName, int] = {}
        for table in tables:
            if table.name.lower() not in self.objects:
                counted[table] = 0
                continue
            query = f"SELECT COUNT(*) FROM {_name(table.name)}"
            counted[table] = int(self.connection.execute(query).fetchone()[0])
        return counted

    def chosen(
        self, table: TableName, when: str | None, alias: str = ""
    ) -> tuple[str, tuple[object, ...]]:
        """SQL choosing the rows of ``table`` a ``when`` predicate matches, and its parameters.

        ``alias`` qualifies the ``Id`` column, as ``c.``.
        """
        if when is None:
            return "1", ()
        try:
            translated = translate(f"SELECT Id FROM {table.name} WHERE {when}", self._fields)
        except SoqlError as error:
            message = f"a `when` predicate on {table.name}: {error}"
            raise EngineError(message) from None
        return f'{alias}"Id" IN ({translated.sql})', translated.parameters

    def orphans(self, edge: Edge) -> int:
        """Records whose non-null reference has no parent record."""
        child, parent = _name(edge.child.name), _name(edge.parent.name)
        present = " AND ".join(f"c.{_name(column)} IS NOT NULL" for column in edge.columns)
        matching = " AND ".join(
            f"p.{_name(p)} = c.{_name(c)}"
            for c, p in zip(edge.columns, edge.parent_columns, strict=True)
        )
        when, parameters = self.chosen(edge.child, edge.when, "c.")
        query = (
            f"SELECT COUNT(*) FROM {child} AS c WHERE {present} AND {when}"
            f" AND NOT EXISTS (SELECT 1 FROM {parent} AS p WHERE {matching})"
        )
        return int(self.connection.execute(query, parameters).fetchone()[0])

    def text_values(self, column: ColumnRef) -> Iterator[str]:
        """The distinct non-null values of a field, as text."""
        name = _name(column.column)
        query = (
            f"SELECT DISTINCT CAST({name} AS TEXT) FROM {_name(column.table.name)}"
            f" WHERE {name} IS NOT NULL"
        )
        for (value,) in self.connection.execute(query):
            yield str(value)

    def failing_rows(self, query: str) -> int:
        """Records a SOQL assertion returns."""
        try:
            translated = translate(query, self._fields)
        except SoqlError as error:
            message = f"an assertion: {error}"
            raise EngineError(message) from None
        counted = f"SELECT COUNT(*) FROM ({translated.sql})"
        return int(self.connection.execute(counted, translated.parameters).fetchone()[0])


@contextmanager
def _opened(directory: Path) -> Generator[SalesforceRestored]:
    objects = Objects.model_validate_json((directory / OBJECTS).read_bytes())
    with closing(sqlite3.connect(directory / DATABASE)) as connection, connection:
        yield SalesforceRestored(connection, objects)


def _counts(restored: SalesforceRestored, tables: Iterable[TableName]) -> tuple[TableCount, ...]:
    return tuple(TableCount(table=t, rows=n) for t, n in restored.counts(tables).items())


def apply_fixup(restored: SalesforceRestored, fixup: Fixup) -> None:
    """Null references to missing parents, or add stub parents for them."""
    edge = fixup.edge
    if edge.parent_columns != (ID,) or len(edge.columns) != 1:
        message = f"{edge.name}: a Salesforce reference names one field and the parent's Id"
        raise EngineError(message)
    column = _name(edge.columns[0])
    child, parent = _name(edge.child.name), _name(edge.parent.name)
    present = f'SELECT "Id" FROM {parent}' if fixup.parent_present else "SELECT NULL WHERE 0"
    when, parameters = restored.chosen(edge.child, edge.when)
    missing = f"{column} IS NOT NULL AND {column} NOT IN ({present}) AND {when}"
    if fixup.mode is EdgeMode.NULL:
        restored.connection.execute(
            f"UPDATE {child} SET {column} = NULL WHERE {missing}", parameters
        )
        return
    if edge.parent.name.lower() not in restored.objects:
        message = f"{edge.name}: stub parents need {edge.parent.name} in the same image"
        raise EngineError(message)
    restored.connection.execute(
        f'INSERT OR IGNORE INTO {parent} ("Id") SELECT DISTINCT {column} FROM {child}'
        f" WHERE {missing}",
        parameters,
    )


def _update(
    connection: sqlite3.Connection,
    table: TableName,
    rules: list[PlannedRule],
    mapper: Callable[[PlannedRule, Iterable[str]], Mapping[str, str]],
) -> None:
    """Apply every rule for one object in one ``UPDATE``."""
    assignments: list[str] = []
    for number, rule in enumerate(rules):
        column = _name(rule.column.column)
        match rule.rule:
            case NullRule():
                assignments.append(f"{column} = NULL")
            case ExpressionRule():
                message = "Salesforce copies take no expression rules"
                raise EngineError(message)
            case _:
                originals = [
                    str(value)
                    for (value,) in connection.execute(
                        f"SELECT DISTINCT CAST({column} AS TEXT) FROM {_name(table.name)}"
                        f" WHERE {column} IS NOT NULL"
                    )
                ]
                mapping = mapper(rule, originals)
                connection.execute(
                    f"CREATE TEMP TABLE _map{number} (original TEXT PRIMARY KEY, replacement TEXT)"
                )
                connection.executemany(f"INSERT INTO _map{number} VALUES (?, ?)", mapping.items())
                assignments.append(
                    f"{column} = (SELECT replacement FROM _map{number}"
                    f" WHERE original = CAST({column} AS TEXT))"
                )
    connection.execute(f"UPDATE {_name(table.name)} SET {', '.join(assignments)}")
    for number in range(len(rules)):
        connection.execute(f"DROP TABLE IF EXISTS _map{number}")


class SalesforceWorking:
    """Working copies and images under one scratch directory."""

    def __init__(self, scratch: Path) -> None:
        """Keep copies under ``scratch``."""
        self.scratch = scratch

    def server_version(self) -> str:
        """The copies' layout version; nothing runs a server."""
        return LAYOUT

    def close(self) -> None:
        """Nothing to stop: it runs no servers."""

    def available(self) -> tuple[Requirement, ...] | None:
        """None: local copies hold a package's objects without the package."""
        return None

    def load(
        self,
        plan: Plan,  # noqa: ARG002 - the protocol passes the plan; loading needs only the group
        group: PlannedGroup,
        extract: Path,
        directory: Path,
        hooks: tuple[Hook, ...],
    ) -> tuple[TableCount, ...]:
        """Build the group's copy from the extract and apply its fallbacks."""
        if hooks:
            message = "the salesforce adapter does not run hooks"
            raise EngineError(message)
        extracted = Objects.model_validate_json((extract / OBJECTS).read_bytes())
        members = set(group.tables)
        objects = Objects(
            api_version=extracted.api_version,
            objects=tuple(o for o in extracted.objects if o.table in members),
        )
        directory.mkdir(parents=True, exist_ok=True)
        (directory / OBJECTS).write_text(objects.model_dump_json(indent=2), "utf-8")
        build(directory / DATABASE, objects, extract / RECORDS)
        with _opened(directory) as restored:
            for fixup in group.fixups:
                apply_fixup(restored, fixup)
            return _counts(restored, group.tables)

    def transform(
        self,
        group: PlannedGroup,
        source: Path,
        directory: Path,
        transformation: Transformation,
    ) -> tuple[TableCount, ...]:
        """Copy the working copy and anonymize the copy, in one transaction."""
        if transformation.hooks:
            message = "the salesforce adapter does not run hooks"
            raise EngineError(message)
        directory.mkdir(parents=True, exist_ok=True)
        for name in (DATABASE, OBJECTS):
            shutil.copyfile(source / name, directory / name)
        by_table: dict[TableName, list[PlannedRule]] = {}
        for rule in transformation.rules:
            by_table.setdefault(rule.column.table, []).append(rule)
        with _opened(directory) as restored:
            for table, rules in by_table.items():
                _update(restored.connection, table, rules, transformation.mapper)
            return _counts(restored, group.tables)

    def package(self, source: Path, image_format: ImageFormat, directory: Path) -> None:
        """Write each object's records as JSON lines, with ``objects.json``."""
        if image_format is not ImageFormat.LOGICAL:
            message = "the salesforce adapter writes only logical images"
            raise EngineError(message)
        (directory / RECORDS).mkdir(parents=True)
        written: list[ObjectRecords] = []
        with _opened(source) as restored:
            for found in restored.objects.values():
                names = ", ".join(_name(c.name) for c in found.columns)
                count = 0
                path = directory / RECORDS / f"{found.table.name}.jsonl"
                with path.open("w", encoding="utf-8") as out:
                    rows = restored.connection.execute(
                        f'SELECT {names} FROM {_name(found.table.name)} ORDER BY "Id"'
                    )
                    for row in rows:
                        record = {
                            c.name: _json(v, c) for c, v in zip(found.columns, row, strict=True)
                        }
                        out.write(json.dumps(record, separators=(",", ":")) + "\n")
                        count += 1
                written.append(found.model_copy(update={"records": count}))
            api_version = Objects.model_validate_json((source / OBJECTS).read_bytes()).api_version
        objects = Objects(api_version=api_version, objects=tuple(written))
        (directory / OBJECTS).write_text(objects.model_dump_json(indent=2), "utf-8")

    @contextmanager
    def restore(
        self, image: Path, image_format: ImageFormat, directory: Path
    ) -> Generator[RestoredImage]:
        """Build a SQLite database from an image, for checks."""
        if image_format is not ImageFormat.LOGICAL:
            message = "the salesforce adapter writes only logical images"
            raise EngineError(message)
        directory.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(image / OBJECTS, directory / OBJECTS)
        objects = Objects.model_validate_json((image / OBJECTS).read_bytes())
        build(directory / DATABASE, objects, image / RECORDS)
        with _opened(directory) as restored:
            yield restored
