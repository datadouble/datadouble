# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The engine conformance suite.

An adapter's tests provide a ``harness`` fixture returning an
:class:`EngineHarness` and declare ``class TestConformance(EngineConformance)``.
The suite drives the adapter only through Datadouble's own interfaces; the
harness supplies what needs the engine's own language: a spec over the sample
dataset, and a small scratch source the suite may change.
"""

import datetime as dt
import json
import os
import signal
import subprocess
import sys
import tarfile
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pytest
from pydantic import BaseModel, JsonValue, TypeAdapter

from datadouble.catalog import CatalogLock, write_lock
from datadouble.engine import EngineAdapter, ExtractResult, Tooling
from datadouble.images import ImageSummary, unpack
from datadouble.names import ColumnName, ColumnRef, Generation, GroupName, TableName
from datadouble.planner import Plan, PlanError, PlanOptions
from datadouble.programs import start_program
from datadouble.registry import Registry, installed
from datadouble.run import Run, RunOptions, RunResult, Stage, StageManifest, StageStatus
from datadouble.spec import ImageFormat, Spec, Strategy, StrictLevel, parse_spec
from datadouble.storage import filesystem_store
from datadouble.strict import Difference, Severity
from datadouble.subset import AllRows

_BUILD = (Stage.PLAN, Stage.EXTRACT, Stage.LOAD, Stage.TRANSFORM, Stage.PACKAGE, Stage.VERIFY)
_ID = ColumnName("id")


class ScratchSource(Protocol):
    """A small source the suite may change, holding one table ``items(id)``."""

    @property
    def connection(self) -> Mapping[str, JsonValue]:
        """``source.connection`` settings for it."""
        ...

    @property
    def items(self) -> TableName:
        """The ``items`` table."""
        ...

    def add_table(self, name: str) -> None:
        """Create another table ``name(id)`` with a primary key."""
        ...

    def add_column(self, column: str) -> None:
        """Add a nullable text column to ``items``."""
        ...

    def insert(self, item_id: int) -> None:
        """Insert a row into ``items`` from another connection, committed at once."""
        ...


@dataclass(frozen=True)
class EngineHarness:
    """What the suite needs from an adapter's tests."""

    engine: EngineAdapter
    sample_spec: str
    """A spec over the sample dataset; ``STORE`` stands for a store directory.

    It must plan with the nested strategy if the engine offers it, produce both
    formats, anonymize ``sample_column`` and verify cleanly.
    """
    sample_column: ColumnRef
    """A column the sample spec anonymizes."""
    scratch: Callable[[], AbstractContextManager[ScratchSource]]
    """A fresh scratch source, removed afterwards."""
    runtime: Mapping[str, Mapping[str, JsonValue]]
    """The ``runtime`` every spec in the suite gets, such as ``{"docker": {}}``."""

    def with_runtime(self, spec: str) -> str:
        """``spec`` with the harness's runtime added."""
        return f"{spec}\nruntime: {json.dumps(self.runtime)}\n"


_RUNTIME: TypeAdapter[dict[str, dict[str, JsonValue]]] = TypeAdapter(
    dict[str, dict[str, JsonValue]]
)


def runtime_for_tests() -> dict[str, dict[str, JsonValue]]:
    """The runtime for test specs: ``DATADOUBLE_TEST_RUNTIME`` (JSON), or local Docker."""
    return _RUNTIME.validate_json(os.environ.get("DATADOUBLE_TEST_RUNTIME", '{"docker": {}}'))


class _Builds:
    """Runs against one harness, in one temporary directory."""

    def __init__(self, harness: EngineHarness, root: Path) -> None:
        self.harness = harness
        self.root = root
        self.registry = Registry(
            engines={str(harness.engine.name): lambda: harness.engine},
            stores={"filesystem": filesystem_store},
            runtimes=installed().runtimes,
        )

    def sample(self, extra: str = "") -> Spec:
        text = self.harness.sample_spec.replace("STORE", str(self.root / "store"))
        return parse_spec(self.harness.with_runtime(text + extra))

    def scratch_spec(self, source: ScratchSource, extra: str = "") -> Spec:
        settings = json.dumps(dict(source.connection))
        # Working databases need the sample's server settings, such as a licence acceptance.
        server = json.dumps(self.sample().source.server)
        engine = self.harness.engine.name
        return parse_spec(
            self.harness.with_runtime(
                f"name: scratch\nsource: {{engine: {engine}, connection: {settings},"
                f" server: {server}}}\n{extra}"
            )
        )

    def lock(self, connection: Mapping[str, JsonValue]) -> CatalogLock:
        with self.harness.engine.connect(connection) as session:
            return CatalogLock.of(session.catalog())

    def run(
        self,
        spec: Spec,
        lock: CatalogLock | None,
        work: str,
        *,
        stages: tuple[Stage, ...] = tuple(Stage),
        resume: Generation | None = None,
        options: PlanOptions | None = None,
    ) -> RunResult:
        settings = RunOptions(
            stages=stages, work=self.root / work, resume=resume, plan=options or PlanOptions()
        )
        return Run(spec, lock, self.registry, settings).execute()

    def restored_values(
        self, result: RunResult, summary: ImageSummary, columns: list[ColumnRef], tag: str
    ) -> dict[str, set[str]]:
        """Distinct values of each column, and row counts, from the logical image if any."""
        archive = min(summary.archives, key=lambda a: a.format is not ImageFormat.LOGICAL)
        group = summary.identity.group
        file = Path(result.work) / "package" / group / archive.key.rsplit("/", 1)[1]
        scratch = self.root / f"restore-{tag}-{group}"
        image, manifest = unpack(file, scratch / "archive")
        ((kind, settings),) = self.harness.runtime.items()
        working = self.harness.engine.working(
            scratch,
            Tooling(
                runtime=self.registry.runtime(kind, settings),
                image=self.sample().source.image,
                server=self.sample().source.server,
            ),
            # The full version: the identity names only the major one, and an
            # engine's images may be tagged by more (Oracle's 23.26.3.0).
            _Built.model_validate_json(manifest).working_server_version,
        )
        found: dict[str, set[str]] = {}
        with working.restore(image, archive.format, scratch / "db") as restored:
            tables = sorted({column.table for column in columns})
            for table, rows in restored.counts(tables).items():
                found[f"{table} rows"] = {str(rows)}
            for column in columns:
                found[str(column)] = set(restored.text_values(column))
        return found


def _sizes(archive: Path) -> dict[str, int]:
    """An archive's bytes as stored, and the bytes of the files in it."""
    with tarfile.open(archive, "r:gz") as opened:
        unpacked = sum(member.size for member in opened.getmembers())
    return {"packed": archive.stat().st_size, "unpacked": unpacked}


class _Built(BaseModel):
    """The part of an image's manifest :meth:`_Builds.restored_values` reads."""

    working_server_version: str


def _plan(result: RunResult) -> Plan:
    return Plan.model_validate_json((Path(result.work) / "plan" / "plan.json").read_bytes())


def _extract_counts(result: RunResult) -> list[tuple[str, int]]:
    manifest = StageManifest.model_validate_json(
        (Path(result.work) / "extract" / "manifest.json").read_bytes()
    )
    extracted = ExtractResult.model_validate_json(json.dumps(manifest.outputs["result"]))
    return sorted((str(count.table), count.rows) for count in extracted.counts)


def wait_for_stage(root: Path, stage: str, process: subprocess.Popen[str]) -> Path:
    """The generation under ``root`` that reaches ``stage`` while ``process`` runs."""
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline and process.poll() is None:
        for generation in root.glob("*"):
            if (generation / stage).exists():
                return generation
        time.sleep(0.2)
    message = f"the run never reached {stage}"
    raise AssertionError(message)


class EngineConformance:
    """Tests every engine adapter must pass. Subclass it as ``TestConformance``."""

    def test_catalog_references_resolve(self, harness: EngineHarness, tmp_path: Path) -> None:
        """Every key names columns the catalog has, and every reference a table it has."""
        builds = _Builds(harness, tmp_path)
        with harness.engine.connect(builds.sample().source.connection) as session:
            catalog = session.catalog()
        assert catalog.tables
        for table in catalog.tables:
            names = {c.name for c in table.columns}
            assert set(table.primary_key) <= names
            for fk in table.foreign_keys:
                target = catalog.table(fk.target)
                assert target is not None, fk
                assert set(fk.columns) <= names
                assert set(fk.target_columns) <= {c.name for c in target.columns}

    def test_snapshot_is_consistent(self, harness: EngineHarness) -> None:
        """Rows committed after the snapshot opens are not seen through it."""
        with harness.scratch() as source:
            source.insert(1)
            with (
                harness.engine.connect(source.connection) as session,
                session.snapshot(dt.datetime.now(tz=dt.UTC)) as snapshot,
            ):
                before = set(snapshot.project(source.items, AllRows(), (_ID,)))
                source.insert(2)
                after = set(snapshot.project(source.items, AllRows(), (_ID,)))
            assert before == after == {(1,)}

    def test_strict_mode(self, harness: EngineHarness, tmp_path: Path) -> None:
        """New tables and columns behave as the strict-mode table says."""
        builds = _Builds(harness, tmp_path)
        with harness.scratch() as source:
            lock = builds.lock(source.connection)
            source.add_table("added")
            source.add_column("extra")
            spec = builds.scratch_spec(source)

            def findings(level: StrictLevel) -> set[tuple[Difference, Severity]]:
                with pytest.raises(PlanError) as caught:
                    builds.run(
                        spec,
                        lock,
                        f"strict-{level}",
                        stages=(Stage.PLAN,),
                        options=PlanOptions(strict=level),
                    )
                assert caught.value.strict is not None
                return {(f.difference, f.severity) for f in caught.value.strict.findings}

            assert findings(StrictLevel.COLUMNS) == {
                (Difference.NEW_TABLE, Severity.FAIL),
                (Difference.NEW_COLUMN, Severity.FAIL),
            }
            assert findings(StrictLevel.TABLES) == {
                (Difference.NEW_TABLE, Severity.FAIL),
                (Difference.NEW_COLUMN, Severity.WARN),
            }
            relaxed = builds.run(
                spec,
                lock,
                "strict-off",
                stages=(Stage.PLAN,),
                options=PlanOptions(strict=StrictLevel.OFF),
            )
            assert len(relaxed.warnings) == 2
            excluded = builds.scratch_spec(source, 'exclude: ["added"]\nstrict: tables\n')
            result = builds.run(excluded, lock, "strict-excluded", stages=(Stage.PLAN,))
            assert [w for w in result.warnings if "new-column" in w]
            assert not [w for w in result.warnings if "new-table" in w]

    def test_both_strategies_choose_the_same_rows(
        self, harness: EngineHarness, tmp_path: Path
    ) -> None:
        """Strategies A and B produce identical row sets on the sample data.

        An engine without strategy A runs strategy B alone, which must verify.
        """
        builds = _Builds(harness, tmp_path)
        spec = builds.sample()
        lock = builds.lock(spec.source.connection)
        if not harness.engine.capabilities().nested:
            only = builds.run(
                spec, lock, "keys", stages=_BUILD, options=PlanOptions(strategy=Strategy.KEYS)
            )
            assert not only.failed(), only.verification
            assert any(rows for _, rows in _extract_counts(only))
            return
        results = {
            strategy: builds.run(
                spec, lock, str(strategy), stages=_BUILD, options=PlanOptions(strategy=strategy)
            )
            for strategy in (Strategy.NESTED, Strategy.KEYS)
        }
        for result in results.values():
            assert not result.failed(), result.verification
        assert _plan(results[Strategy.NESTED]).strategy is Strategy.NESTED
        assert _extract_counts(results[Strategy.NESTED]) == _extract_counts(results[Strategy.KEYS])
        assert any(rows for _, rows in _extract_counts(results[Strategy.NESTED]))
        plan = _plan(results[Strategy.NESTED])
        anonymized = {rule.column for rule in plan.anonymization}
        compared = [
            ColumnRef(table=table.name, column=column.name)
            for table in plan.catalog.tables
            if table.name in plan.groups[0].tables
            for column in table.columns
            if ColumnRef(table=table.name, column=column.name) not in anonymized
        ]
        restored = {
            strategy: builds.restored_values(result, result.images[0], compared, str(strategy))
            for strategy, result in results.items()
        }
        nested, keys = restored[Strategy.NESTED], restored[Strategy.KEYS]
        differing = {
            name: (sorted(nested[name] - keys[name])[:3], sorted(keys[name] - nested[name])[:3])
            for name in nested
            if nested[name] != keys.get(name)
        }
        assert not differing, differing

    def test_whole_build(self, harness: EngineHarness, tmp_path: Path) -> None:
        """The sample spec runs plan through store, verifies, and stores every format."""
        builds = _Builds(harness, tmp_path)
        spec = builds.sample()
        result = builds.run(spec, builds.lock(spec.source.connection), "work")
        failures = [c for r in result.verification for c in r.checks() if not c.passed]
        assert not failures, failures
        assert result.stored
        for summary in result.images:
            formats = harness.engine.capabilities().formats
            assert {a.format for a in summary.archives} == set(formats)
            assert summary.verification.passed
        # Each archive's size, packed and unpacked, where CI keeps its reports.
        reports = Path(os.environ.get("DATADOUBLE_TEST_REPORTS", str(tmp_path)))
        (reports / f"image-sizes.{harness.engine.name}.json").write_text(
            json.dumps(
                {
                    f"{s.identity.group}/{a.format}": _sizes(
                        Path(result.work) / "package" / s.identity.group / a.key.rsplit("/", 1)[1]
                    )
                    for s in result.images
                    for a in s.archives
                },
                indent=1,
                sort_keys=True,
            ),
            "utf-8",
        )

    def test_killed_run_resumes(self, harness: EngineHarness, tmp_path: Path) -> None:
        """A run killed mid-way resumes from its last completed stage."""
        builds = _Builds(harness, tmp_path)
        spec = builds.sample()
        spec_path = tmp_path / "sample.yml"
        spec_path.write_text(
            harness.with_runtime(harness.sample_spec.replace("STORE", str(tmp_path / "store"))),
            "utf-8",
        )
        lock = builds.lock(spec.source.connection)
        write_lock(lock, tmp_path / "catalog.lock")
        work = tmp_path / "work"
        command = [
            sys.executable,
            "-m",
            "datadouble.cli",
            "run",
            ",".join(Stage),
            "--spec",
            str(spec_path),
            "--work",
            str(work),
        ]
        process = start_program(command[0], command[1:])
        try:
            generation = wait_for_stage(work / spec.name, "transform", process)
        finally:
            process.send_signal(signal.SIGKILL)
            process.wait()
        result = builds.run(spec, lock, "work", resume=Generation(generation.name))
        status = {(r.stage, r.group): r.status for r in result.stages}
        assert status[(Stage.PLAN, None)] is StageStatus.REUSED
        assert status[(Stage.EXTRACT, None)] is StageStatus.REUSED
        assert not result.failed(), result.verification
        assert result.stored

    def test_groups_share_a_generation_and_agree(
        self, harness: EngineHarness, tmp_path: Path
    ) -> None:
        """Images from one build share a generation and agree on anonymized values."""
        builds = _Builds(harness, tmp_path)
        table = harness.sample_column.table
        # The small image depends on the big one for its references.
        spec = builds.sample(
            "images:\n"
            '  everything: {include: ["*"]}\n'
            f'  one: {{include: ["{table}"], requires: [everything]}}\n'
        )
        result = builds.run(
            spec,
            builds.lock(spec.source.connection),
            "work",
            stages=_BUILD,
            options=PlanOptions(formats=(ImageFormat.LOGICAL,)),
        )
        assert not result.failed(), result.verification
        assert {s.identity.generation for s in result.images} == {result.generation}
        values = {
            s.identity.group: builds.restored_values(result, s, [harness.sample_column], "groups")[
                str(harness.sample_column)
            ]
            for s in result.images
        }
        assert values[GroupName("one")]
        assert values[GroupName("one")] == values[GroupName("everything")]
