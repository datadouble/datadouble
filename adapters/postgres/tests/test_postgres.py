# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""PostgreSQL behavior beyond the conformance suite: fallbacks, hooks and failures."""

import datetime as dt
import json
import os
import uuid
from collections.abc import Generator
from pathlib import Path

import pytest

from datadouble.catalog import CatalogLock, ColumnKind
from datadouble.engine import EngineError, Tooling, Transformation, WorkingDatabases
from datadouble.images import unpack
from datadouble.names import ColumnName, ColumnRef, Generation, TableName
from datadouble.planner import Plan, PlannedRule, PlanOptions
from datadouble.registry import Registry, installed
from datadouble.run import Run, RunOptions, RunResult, Stage
from datadouble.spec import HashOptions, HashRule, ImageFormat, Strategy, parse_spec
from datadouble.storage import filesystem_store
from datadouble.subset import AllRows, Predicate
from datadouble_conformance.engine import ScratchSource, runtime_for_tests
from datadouble_postgres import PostgresEngine
from datadouble_postgres.cluster import Cluster, Tools, default_image
from datadouble_postgres.render import trusted
from datadouble_postgres.source import PostgresSession
from datadouble_s3 import S3Store, s3_store
from test_conformance import PostgresScratch, scratch

pytestmark = pytest.mark.integration

SCHEMA = """
CREATE TABLE public.parent (id integer PRIMARY KEY, name text NOT NULL, born date NOT NULL);
CREATE TABLE public.child (
    id integer PRIMARY KEY,
    parent_id integer REFERENCES public.parent (id),
    other_id integer,
    kind text,
    label text
);
CREATE TABLE public.p (id integer PRIMARY KEY) PARTITION BY RANGE (id);
CREATE TABLE public.p1 PARTITION OF public.p FOR VALUES FROM (0) TO (100);
CREATE TABLE public.r (id integer PRIMARY KEY, pid integer REFERENCES public.p1 (id));
INSERT INTO public.parent VALUES
    (1, 'Ann', '2000-01-01'), (2, 'Bo', '2000-01-02'), (3, 'Cy', '2000-01-03');
INSERT INTO public.child VALUES
    (1, 1, 7, 'x', 'a'), (2, 2, 3, 'y', 'b'), (3, 3, 2, 'x', 'c');
INSERT INTO public.p VALUES (5);
INSERT INTO public.r VALUES (1, 5);
"""

SPEC = """
name: scratch
source: {engine: postgres, connection: CONNECTION}
exclude: ["items"]
subset:
  seeds: {child: "id <= 2", r: "id = 1"}
  edges:
    child.parent_id -> parent: {on_missing: stub}
    child.other_id -> parent: {on_missing: null}
  virtual_fks:
    child.other_id -> parent: {when: "kind = 'x'"}
hooks:
  before_load: [{sql: "SELECT 1"}]
  after_transform: [{sql: "UPDATE public.child SET label = 'done'"}]
anonymize:
  parent: {name: {faker: first_name}}
  child: {kind: {expression: "upper(kind)"}}
store:
  - filesystem: {path: STORE}
"""


@pytest.fixture
def source() -> Generator[PostgresScratch]:
    with scratch() as made:
        assert isinstance(made, PostgresScratch)
        made.execute(trusted(SCHEMA))
        yield made


def registry() -> Registry:
    return Registry(
        engines={"postgres": PostgresEngine},
        stores={"filesystem": filesystem_store, "s3": s3_store},
        runtimes=installed().runtimes,
    )


def tooling() -> Tooling:
    ((kind, settings),) = runtime_for_tests().items()
    return Tooling(runtime=installed().runtime(kind, settings))


def working_databases(scratch: Path) -> WorkingDatabases:
    return PostgresEngine().working(scratch, tooling(), "18")


def build(
    source: ScratchSource,
    tmp_path: Path,
    *,
    spec: str = SPEC,
    stages: tuple[Stage, ...] = tuple(Stage),
    options: PlanOptions | None = None,
    resume: Generation | None = None,
) -> RunResult:
    parsed = parse_spec(
        spec.replace("CONNECTION", json.dumps(dict(source.connection))).replace(
            "STORE", str(tmp_path / "store")
        )
        + f"\nruntime: {json.dumps(runtime_for_tests())}\n"
    )
    with PostgresEngine().connect(source.connection) as session:
        lock = CatalogLock.of(session.catalog())
    run_options = RunOptions(
        stages=stages,
        work=tmp_path / "work",
        resume=resume,
        plan=options or PlanOptions(),
    )
    return Run(parsed, lock, registry(), run_options).execute()


def test_fallbacks_hooks_and_partition_references(source: PostgresScratch, tmp_path: Path) -> None:
    result = build(source, tmp_path)
    assert not result.failed(), result.verification
    (summary,) = result.images
    archive = next(a for a in summary.archives if a.format is ImageFormat.LOGICAL)
    image, _ = unpack(tmp_path / "store" / archive.key, tmp_path / "unpacked")
    working = working_databases(tmp_path / "check")
    with working.restore(image, ImageFormat.LOGICAL, tmp_path / "check" / "db") as restored:
        parents = TableName.of("public", "parent")
        children = TableName.of("public", "child")
        assert restored.counts([parents, children]) == {parents: 2, children: 2}
        # Children 1 and 2 are chosen; parents are not followed, so stubs stand in.
        assert set(restored.text_values(ColumnRef(table=parents, column=ColumnName("born")))) == {
            "1970-01-01"
        }
        # Child 1 is a kind 'x' row whose other parent (7) does not exist: nulled.
        assert set(
            restored.text_values(ColumnRef(table=children, column=ColumnName("other_id")))
        ) == {"3"}
        assert set(restored.text_values(ColumnRef(table=children, column=ColumnName("label")))) == {
            "done"
        }
        with pytest.raises(EngineError, match=r"running an assertion failed: \[42703\]"):
            restored.failing_rows("SELECT nope FROM public.child")
        with pytest.raises(EngineError, match=r"scanning public\.child\.nope failed"):
            list(restored.text_values(ColumnRef(table=children, column=ColumnName("nope"))))


def test_images_go_to_s3(source: PostgresScratch, tmp_path: Path) -> None:
    endpoint = os.environ.get("DATADOUBLE_TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("no S3 endpoint; run in the Compose default group")
    bucket = f"dd-{uuid.uuid4().hex[:12]}"
    store = s3_store({"bucket": bucket, "endpoint_url": endpoint})
    assert isinstance(store, S3Store)
    store.client.create_bucket(Bucket=bucket)
    spec = SPEC.replace(
        "  - filesystem: {path: STORE}",
        f"  - s3: {{bucket: {bucket}, prefix: golden, endpoint_url: '{endpoint}'}}",
    )
    result = build(source, tmp_path, spec=spec)
    assert not result.failed()
    (summary,) = result.images
    assert result.stored == (f"s3://{bucket}/golden/{summary.identity.summary_key()}",)
    keys = store.list("golden/")
    assert f"golden/{summary.identity.summary_key()}" in keys
    assert len(keys) == 4


def test_failing_hooks_name_objects_not_values(source: PostgresScratch, tmp_path: Path) -> None:
    broken = SPEC.replace(
        "UPDATE public.child SET label = 'done'", "UPDATE public.parent SET name = NULL"
    )
    with pytest.raises(EngineError) as caught:
        build(source, tmp_path, spec=broken)
    assert str(caught.value) == (
        'an after_transform hook failed: [23502] null value in column "…" of relation "…" '
        "violates not-null constraint (schema public, table parent, column name)"
    )
    before = SPEC.replace("SELECT 1", "SELEKT 1")
    with pytest.raises(EngineError, match=r"a before_load hook failed: \[42601\]"):
        build(source, tmp_path / "again", spec=before)


def test_bad_seeds_fail_in_both_strategies(source: PostgresScratch, tmp_path: Path) -> None:
    bad = SPEC.replace('"id <= 2"', '"nope <= 2"')
    for strategy in (Strategy.NESTED, Strategy.KEYS):
        with pytest.raises(EngineError, match=r"(extracting|reading keys of) public\.\w+ failed"):
            build(
                source,
                tmp_path / str(strategy),
                spec=bad,
                stages=(Stage.PLAN, Stage.EXTRACT),
                options=PlanOptions(strategy=strategy),
            )


def test_schema_changes_after_planning_are_caught(source: PostgresScratch, tmp_path: Path) -> None:
    planned = build(source, tmp_path, stages=(Stage.PLAN,))
    source.add_column("late")
    with pytest.raises(EngineError, match="schema changed between planning and the snapshot"):
        build(source, tmp_path, stages=(Stage.EXTRACT,), resume=planned.generation)


def test_excluded_parents_are_nulled(source: PostgresScratch, tmp_path: Path) -> None:
    spec = (
        SPEC.replace('exclude: ["items"]', 'exclude: ["items", "parent"]')
        .replace(
            "child.parent_id -> parent: {on_missing: stub}",
            "child.parent_id -> parent: {on_missing: null}",
        )
        .replace("  parent: {name: {faker: first_name}}\n", "")
    )
    result = build(source, tmp_path, spec=spec)
    assert not result.failed(), result.verification


def test_corrupt_extracts_fail_to_load(source: PostgresScratch, tmp_path: Path) -> None:
    spec = SPEC.replace('exclude: ["items"]\n', "")
    extracted = build(source, tmp_path, spec=spec, stages=(Stage.PLAN, Stage.EXTRACT))
    data = tmp_path / "work" / "scratch" / extracted.generation / "extract" / "data"
    for file in data.glob("*.copy"):
        file.write_text("not\ta\trow\tat\tall\n", encoding="utf-8")
    with pytest.raises(EngineError, match=r"loading public\.\w+ failed: \[22P02\]"):
        build(source, tmp_path, spec=spec, stages=(Stage.LOAD,), resume=extracted.generation)


def test_closed_connections(source: PostgresScratch) -> None:
    with PostgresEngine().connect(source.connection) as session:
        assert isinstance(session, PostgresSession)
        session.connection.close()
        with pytest.raises(EngineError, match="reading the catalog failed"):
            session.catalog()
        with (
            pytest.raises(EngineError, match="reading the snapshot failed"),
            session.snapshot(dt.datetime.now(tz=dt.UTC)),
        ):
            pass


def test_snapshot_reads(source: PostgresScratch) -> None:
    with (
        PostgresEngine().connect(source.connection) as session,
        session.snapshot(dt.datetime.now(tz=dt.UTC)) as snapshot,
    ):
        child = TableName.of("public", "child")
        assert len(list(snapshot.project(child, AllRows(), (ColumnName("id"),)))) == 3
        with pytest.raises(EngineError, match=r"reading keys of public\.child failed"):
            snapshot.project(child, Predicate("nope = 1"), (ColumnName("id"),))


def test_bad_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DD_BAD_DSN", "postgresql://nobody:wrong@postgres:5432/nowhere")
    with (
        pytest.raises(EngineError, match=r"connecting to the source failed"),
        PostgresEngine().connect({"dsn": {"env": "DD_BAD_DSN"}}),
    ):
        pass


def test_working_failures(source: PostgresScratch, tmp_path: Path) -> None:
    loaded = build(source, tmp_path, stages=(Stage.PLAN, Stage.EXTRACT, Stage.LOAD))
    work = tmp_path / "work" / "scratch" / loaded.generation
    plan = Plan.model_validate_json((work / "plan" / "plan.json").read_bytes())
    working = working_databases(work / "scratch")
    missing = PlannedRule(
        column=ColumnRef(table=TableName.of("public", "child"), column=ColumnName("nope")),
        kind=ColumnKind.TEXT,
        type="text",
        rule=HashRule(hash=HashOptions()),
    )
    transformation = Transformation((missing,), lambda _rule, _values: {}, ())
    with pytest.raises(EngineError, match="cannot copy missing"):
        working.transform(plan.groups[0], work / "missing", tmp_path / "t1", transformation)
    with pytest.raises(EngineError, match=r"reading public\.child\.nope failed"):
        working.transform(
            plan.groups[0], work / "load" / "all" / "db", tmp_path / "t2", transformation
        )


def test_clusters(tmp_path: Path) -> None:
    tools = Tools(tooling().runtime, default_image("18"), tmp_path / "home")
    cluster = Cluster(tools, tmp_path / "db" / "pgdata", tmp_path / "db")
    cluster.initialize()
    with cluster.running():
        cluster.create_database("postgres")
        cluster.create_database("other")
        with cluster.connect("other") as connection:
            assert connection.execute("SELECT 1").fetchone() == (1,)
