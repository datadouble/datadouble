# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""A whole engine adapter backed by JSON files, for testing the core end to end.

Working databases and images are JSON documents of rows per table. Hooks and
assertions use a tiny language: ``delete TABLE where PREDICATE`` and
``rows TABLE where PREDICATE``. Expressions are a quoted literal or a column
name.
"""

import datetime as dt
import json
import shutil
from collections.abc import Generator, Hashable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import JsonValue

from datadouble.catalog import Catalog
from datadouble.engine import (
    EngineAdapter,
    EngineError,
    ExtractResult,
    RestoredImage,
    Snapshot,
    SourceSession,
    TableCount,
    Transformation,
    WorkingDatabases,
)
from datadouble.names import ColumnName, ColumnRef, EngineName, TableName
from datadouble.planner import Capabilities, Plan, PlannedGroup
from datadouble.spec import ExpressionRule, Hook, ImageFormat, NullRule
from datadouble.subset import Condition, Edge, EdgeMode, KeyTuple
from fakes import FakeSource, evaluate_predicate

type Rows = dict[str, list[dict[str, Hashable]]]


def _key(table: TableName) -> str:
    return str(table)


def _read(path: Path) -> Rows:
    data: Rows = json.loads(path.read_text(encoding="utf-8"))
    return data


def _write(path: Path, rows: Rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, sort_keys=True), encoding="utf-8")


def _counts(rows: Rows) -> tuple[TableCount, ...]:
    return tuple(
        TableCount(table=TableName.of(*name.split(".", 1)), rows=len(table_rows))
        for name, table_rows in sorted(rows.items())
    )


def _statement(text: str) -> tuple[str, str, str]:
    verb, table, rest = text.split(" ", 2)
    _, predicate = rest.split("where ", 1)
    return verb, table, predicate


@dataclass
class FakeSnapshot:
    source: FakeSource
    opened: list[str]

    def project(
        self, table: TableName, condition: Condition, columns: tuple[ColumnName, ...]
    ) -> Iterable[KeyTuple]:
        return self.source.project(table, condition, columns)

    def extract(
        self, plan: Plan, selections: Mapping[TableName, Condition], directory: Path
    ) -> ExtractResult:
        rows: Rows = {
            _key(table): [dict(r) for r in self.source.select(table, selections)]
            for table in plan.extracted()
        }
        _write(directory / "rows.json", rows)
        return ExtractResult(
            server_version="1.2",
            snapshot="fake-snapshot",
            snapshot_seconds=0.0,
            counts=_counts(rows),
        )


@dataclass
class FakeSession:
    engine: "FakeEngine"

    def catalog(self) -> Catalog:
        return self.engine.catalog

    @contextmanager
    def snapshot(self, as_of: dt.datetime) -> Generator[Snapshot]:
        self.engine.snapshots += 1
        yield FakeSnapshot(self.engine.source, [])


@dataclass
class FakeRestored:
    rows: Rows

    def counts(self, tables: Iterable[TableName]) -> dict[TableName, int]:
        return {t: len(self.rows.get(_key(t), [])) for t in tables}

    def orphans(self, edge: Edge) -> int:
        parents = {
            tuple(r[c] for c in edge.parent_columns) for r in self.rows.get(_key(edge.parent), [])
        }
        return sum(
            1
            for r in self.rows.get(_key(edge.child), [])
            if all(r[c] is not None for c in edge.columns)
            and (edge.when is None or evaluate_predicate(edge.when, r))
            and tuple(r[c] for c in edge.columns) not in parents
        )

    def text_values(self, column: ColumnRef) -> Iterable[str]:
        return sorted(
            {
                str(r[column.column])
                for r in self.rows.get(_key(column.table), [])
                if r[column.column] is not None
            }
        )

    def failing_rows(self, query: str) -> int:
        _, table, predicate = _statement(query)
        return sum(1 for r in self.rows.get(table, []) if evaluate_predicate(predicate, r))


def _apply_fixups(rows: Rows, group: PlannedGroup, catalog: Catalog) -> None:
    for fixup in group.fixups:
        edge = fixup.edge
        parents = {
            tuple(r[c] for c in edge.parent_columns) for r in rows.get(_key(edge.parent), [])
        }
        missing: set[tuple[Hashable, ...]] = set()
        for row in rows.get(_key(edge.child), []):
            value = tuple(row[c] for c in edge.columns)
            if None in value or value in parents:
                continue
            if fixup.mode is EdgeMode.NULL:
                for column in edge.columns:
                    row[column] = None
            else:
                missing.add(value)
        for value in sorted(missing, key=str):
            parent = catalog.table(edge.parent)
            names: list[str] = [c.name for c in parent.columns] if parent else []
            stub: dict[str, Hashable] = {}
            stub.update((name, None) for name in names)
            stub.update(zip(edge.parent_columns, value, strict=True))
            rows.setdefault(_key(edge.parent), []).append(stub)


@dataclass
class FakeWorking:
    engine: "FakeEngine"
    scratch: Path

    def server_version(self) -> str:
        return "1.0"

    def release(self, directory: Path) -> None:
        self.engine.released.append(directory.name)

    def load(
        self,
        plan: Plan,
        group: PlannedGroup,
        extract: Path,
        directory: Path,
        hooks: tuple[Hook, ...],
    ) -> tuple[TableCount, ...]:
        if self.engine.fail_load:
            message = "the fake engine was told to fail"
            raise EngineError(message)
        self.engine.hooks.extend(h.sql for h in hooks)
        everything = _read(extract / "rows.json")
        rows = {_key(t): everything.get(_key(t), []) for t in group.tables}
        _apply_fixups(rows, group, plan.catalog)
        _write(directory / "db.json", rows)
        return _counts(rows)

    def transform(
        self,
        group: PlannedGroup,
        source: Path,
        directory: Path,
        transformation: Transformation,
    ) -> tuple[TableCount, ...]:
        self.engine.transforms += 1
        rows = _read(source / "db.json")
        for rule in transformation.rules:
            table_rows = rows.get(_key(rule.column.table), [])
            column = rule.column.column
            match rule.rule:
                case NullRule():
                    for row in table_rows:
                        row[column] = None
                case ExpressionRule(expression=expression, where=where):
                    for row in table_rows:
                        if where is None or evaluate_predicate(where, row):
                            literal = expression.strip("'")
                            row[column] = literal if expression.startswith("'") else row[expression]
                case _:
                    values = [str(r[column]) for r in table_rows if r[column] is not None]
                    mapping = transformation.mapper(rule, values)
                    for row in table_rows:
                        if row[column] is not None:
                            row[column] = mapping[str(row[column])]
        for hook in transformation.hooks:
            _, table, predicate = _statement(hook.sql)
            rows[table] = [r for r in rows.get(table, []) if not evaluate_predicate(predicate, r)]
        _write(directory / "db.json", rows)
        return _counts(rows)

    def package(self, source: Path, image_format: ImageFormat, directory: Path) -> None:
        shutil.copyfile(source / "db.json", directory / f"{image_format}.json")
        if image_format in self.engine.corrupt:
            (directory / f"{image_format}.json").write_text("{not json", encoding="utf-8")

    @contextmanager
    def restore(
        self, image: Path, image_format: ImageFormat, directory: Path
    ) -> Generator[RestoredImage]:
        directory.mkdir(parents=True, exist_ok=True)
        try:
            rows = _read(image / f"{image_format}.json")
        except json.JSONDecodeError as error:
            message = f"cannot restore: {error.msg}"
            raise EngineError(message) from None
        yield FakeRestored(rows)


@dataclass
class FakeEngine:
    catalog: Catalog
    source: FakeSource
    capabilities_: Capabilities = field(
        default_factory=lambda: Capabilities(
            formats=(ImageFormat.PHYSICAL, ImageFormat.LOGICAL), expressions=True, nested=True
        )
    )
    fail_load: bool = False
    corrupt: set[ImageFormat] = field(default_factory=set[ImageFormat])
    connections: list[Mapping[str, JsonValue]] = field(
        default_factory=list[Mapping[str, JsonValue]]
    )
    hooks: list[str] = field(default_factory=list[str])
    released: list[str] = field(default_factory=list[str])
    snapshots: int = 0
    transforms: int = 0

    @property
    def name(self) -> EngineName:
        return EngineName("fake")

    def capabilities(self) -> Capabilities:
        return self.capabilities_

    @contextmanager
    def connect(self, connection: Mapping[str, JsonValue]) -> Generator[SourceSession]:
        self.connections.append(connection)
        yield FakeSession(self)

    def working(self, scratch: Path) -> WorkingDatabases:
        return FakeWorking(self, scratch)


def check_protocols(engine: FakeEngine) -> EngineAdapter:
    return engine
