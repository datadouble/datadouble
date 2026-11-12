# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""MongoDB behavior beyond the conformance suite: fallbacks, assertions and failures."""

import datetime as dt
import json
import shutil
from collections.abc import Generator
from pathlib import Path

import pytest
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from datadouble.catalog import CatalogLock, ColumnKind
from datadouble.engine import EngineError, Tooling, Transformation
from datadouble.names import ColumnName, ColumnRef, Generation, TableName
from datadouble.planner import Fixup, Plan, PlannedRule, PlanOptions
from datadouble.registry import Registry, installed
from datadouble.run import Run, RunOptions, RunResult, Stage
from datadouble.spec import (
    ExpressionRule,
    HashOptions,
    HashRule,
    Hook,
    ImageFormat,
    Rule,
    TransformHook,
    parse_spec,
)
from datadouble.storage import filesystem_store
from datadouble.subset import Edge, EdgeMode, Predicate
from datadouble_conformance.engine import runtime_for_tests
from datadouble_mongodb import MongoEngine
from datadouble_mongodb.server import MongoServer, Tools, default_image
from datadouble_mongodb.source import ExtractIndex, MongoSource
from datadouble_mongodb.working import MongoRestored, MongoWorking, apply_fixup
from test_mongodb_conformance import MongoScratch, scratch

pytestmark = pytest.mark.integration

SPEC = """
name: scratch
source: {engine: mongodb, connection: CONNECTION}
exclude: ["gone"]
subset:
  seeds: {child: '{"_id": {"$lte": 2}}', other: '{"_id": 1}'}
  edges:
    child.parent_id -> parent: {on_missing: stub}
    child.lost_id -> gone: {on_missing: null}
  virtual_fks:
    child.parent_id -> parent: {}
    child.lost_id -> gone: {}
    other.code -> parent: {when: '{"kind": "x"}'}
anonymize:
  parent: {code: {hash: {}}}
  child: {label: {expression: '"hidden"', where: '{"_id": 1}'}, missing: {null: {}}}
verify:
  assertions:
    - name: no children over 2
      sql: '{"collection": "child", "pipeline": [{"$match": {"_id": {"$gt": 2}}}]}'
store:
  - filesystem: {path: STORE}
"""


@pytest.fixture
def source() -> Generator[MongoScratch]:
    with scratch() as made:
        assert isinstance(made, MongoScratch)
        with made.client() as client:
            database = client[made.database]
            database.parent.insert_many([{"_id": 1, "code": 41}, {"_id": 2, "code": 42}])
            database.child.insert_many(
                [
                    {"_id": 1, "parent_id": 1, "lost_id": 9, "label": "a", "missing": None},
                    {"_id": 2, "parent_id": 7, "lost_id": None, "label": "b"},
                    {"_id": 3, "parent_id": 8, "label": "c"},
                ]
            )
            database.gone.insert_one({"_id": 9})
            database.other.insert_one({"_id": 1, "kind": "x", "code": 2})
        yield made


def registry() -> Registry:
    return Registry(
        engines={"mongodb": MongoEngine},
        stores={"filesystem": filesystem_store},
        runtimes=installed().runtimes,
    )


def tools() -> Tools:
    ((kind, settings),) = runtime_for_tests().items()
    return Tools(installed().runtime(kind, settings), default_image("9.0.8"))


def build(
    source: MongoScratch,
    tmp_path: Path,
    *,
    spec: str = SPEC,
    stages: tuple[Stage, ...] = tuple(Stage),
    resume: Generation | None = None,
) -> RunResult:
    parsed = parse_spec(
        spec.replace("CONNECTION", json.dumps(dict(source.connection))).replace(
            "STORE", str(tmp_path / "store")
        )
        + f"\nruntime: {json.dumps(runtime_for_tests())}\n"
    )
    with MongoEngine().connect(source.connection) as session:
        lock = CatalogLock.of(session.catalog())
    options = RunOptions(stages=stages, work=tmp_path / "work", resume=resume, plan=PlanOptions())
    return Run(parsed, lock, registry(), options).execute()


def test_fallbacks_rules_and_assertions(source: MongoScratch, tmp_path: Path) -> None:
    result = build(source, tmp_path)
    assert not result.failed(), result.verification
    work = Path(result.work)
    working = MongoWorking(tools(), tmp_path / "check")
    image = work / "transform" / "all" / "db"
    server = MongoServer(tools(), image / "db", tmp_path / "check")
    with server.running(), server.client() as client:
        database = client[source.database]
        parents = {d["_id"]: d.get("code") for d in database.parent.find()}
        children = {d["_id"]: d for d in database.child.find()}
    # Parent 2 comes from `other`; the parents of children 1 and 2 are not
    # followed and become stubs; references into "gone" are nulled.
    assert set(parents) == {1, 2, 7}
    assert parents[1] is None
    assert parents[2] not in {None, 2}
    assert children[1]["lost_id"] is None
    assert children[1]["label"] == "hidden"
    assert children[2]["label"] == "b"
    assert set(children) == {1, 2}
    assert working.server_version().startswith("9.")


def test_restored_checks(source: MongoScratch) -> None:
    with source.client() as client:
        restored = MongoRestored(client[source.database])
        assert restored.failing_rows('{"collection": "child", "pipeline": []}') == 3
        with pytest.raises(EngineError, match="a MongoDB assertion is"):
            restored.failing_rows('{"pipeline": []}')
        with pytest.raises(
            EngineError, match=r"running an assertion failed: OperationFailure \(code"
        ):
            restored.failing_rows('{"collection": "child", "pipeline": [{"$bogus": 1}]}')
        edge = Edge(
            name="virtual: other.code -> parent",
            child=TableName.of(source.database, "other"),
            columns=(ColumnName("code"),),
            parent=TableName.of(source.database, "parent"),
            parent_columns=(ColumnName("_id"),),
            when='{"kind": "x"}',
        )
        assert restored.orphans(edge) == 0
        composite = Edge(
            name="pair",
            child=edge.child,
            columns=(ColumnName("a"), ColumnName("b")),
            parent=edge.parent,
            parent_columns=(ColumnName("a"), ColumnName("b")),
        )
        with pytest.raises(EngineError, match="one field on each side"):
            restored.orphans(composite)
    closed: MongoClient[dict[str, object]] = MongoClient(source.uri)
    closed.close()
    gone = MongoRestored(closed[source.database])
    for check, action in (
        (lambda: gone.counts([edge.child]), "counting"),
        (lambda: gone.orphans(edge), "checking"),
        (
            lambda: list(gone.text_values(ColumnRef(table=edge.child, column=ColumnName("x")))),
            "scanning",
        ),
    ):
        with pytest.raises(EngineError, match=f"{action} .*failed"):
            check()


def test_source_failures(source: MongoScratch, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DD_BAD_MONGO", "mongodb://nowhere.invalid:1/?serverSelectionTimeoutMS=200")
    with (
        pytest.raises(EngineError, match="connecting to the source failed"),
        MongoEngine().connect({"uri": {"env": "DD_BAD_MONGO"}, "database": "x"}),
    ):
        pass
    with MongoEngine().connect(source.connection) as session:
        assert isinstance(session, MongoSource)
        with (
            session.snapshot(dt.datetime.now(tz=dt.UTC)) as snapshot,
            pytest.raises(EngineError, match="reading keys of"),
        ):
            snapshot.project(source.items, Predicate('{"$bogus": 1}'), (ColumnName("_id"),))
        with (
            pytest.raises(EngineError, match=r"reading the snapshot failed: PyMongoError"),
            session.snapshot(dt.datetime.now(tz=dt.UTC)),
        ):
            raise PyMongoError
    unreachable: MongoClient[dict[str, object]] = MongoClient(
        "mongodb://nowhere.invalid:1", serverSelectionTimeoutMS=200
    )
    with (
        unreachable,
        pytest.raises(EngineError, match="reading the catalog failed"),
    ):
        MongoSource(unreachable, "x", 10).catalog()


def test_schema_changes_after_planning(source: MongoScratch, tmp_path: Path) -> None:
    planned = build(source, tmp_path, stages=(Stage.PLAN,))
    source.add_column("late")
    with pytest.raises(EngineError, match="changed between planning and the snapshot"):
        build(source, tmp_path, stages=(Stage.EXTRACT,), resume=planned.generation)


def test_bad_seeds_fail_extract(source: MongoScratch, tmp_path: Path) -> None:
    planned = build(source, tmp_path, stages=(Stage.PLAN,))
    plan = Plan.model_validate_json((Path(planned.work) / "plan" / "plan.json").read_bytes())
    child = TableName.of(source.database, "child")
    selections = {table: Predicate('{"$bogus": 1}') for table in plan.extracted()}
    with (
        MongoEngine().connect(source.connection) as session,
        session.snapshot(plan.as_of) as snapshot,
    ):
        assert child in selections
        with pytest.raises(EngineError, match="extracting failed"):
            snapshot.extract(plan, selections, tmp_path / "extract", Tooling(tools().runtime))


def test_working_failures(source: MongoScratch, tmp_path: Path) -> None:
    loaded = build(source, tmp_path, stages=(Stage.PLAN, Stage.EXTRACT, Stage.LOAD))
    work = Path(loaded.work)
    plan = Plan.model_validate_json((work / "plan" / "plan.json").read_bytes())
    group = plan.groups[0]
    working = MongoWorking(tools(), tmp_path / "scratch")
    with pytest.raises(EngineError, match="does not run hooks"):
        working.load(plan, group, work / "extract" / "data", tmp_path / "x", (Hook(sql="x"),))
    # An extract without the group's collections restores nothing.
    empty = tmp_path / "empty"
    empty.mkdir()
    shutil.copyfile(work / "load" / "all" / "db" / "database.json", empty / "database.json")
    (empty / "index.json").write_text(
        ExtractIndex(database=source.database, collections=()).model_dump_json(), "utf-8"
    )
    counts = working.load(plan, group, empty, tmp_path / "empty-db", ())
    assert all(count.rows == 0 for count in counts)

    child = TableName.of(source.database, "child")

    def rule(column: str, chosen: Rule) -> PlannedRule:
        return PlannedRule(
            column=ColumnRef(table=child, column=ColumnName(column)),
            kind=ColumnKind.TEXT,
            type="string",
            rule=chosen,
        )

    loaded_db = work / "load" / "all" / "db"
    nothing = rule("missing", HashRule(hash=HashOptions()))
    counts = working.transform(
        group, loaded_db, tmp_path / "t1", Transformation((nothing,), lambda _r, _v: {}, ())
    )
    assert counts
    with pytest.raises(EngineError, match="does not run hooks"):
        working.transform(
            group,
            loaded_db,
            tmp_path / "t2",
            Transformation((), lambda _r, _v: {}, (TransformHook(sql="x"),)),
        )
    bogus = rule("label", ExpressionRule(expression='{"$bogus": 1}'))
    with pytest.raises(EngineError, match=r"anonymizing .*label failed"):
        working.transform(
            group, loaded_db, tmp_path / "t3", Transformation((bogus,), lambda _r, _v: {}, ())
        )
    with pytest.raises(EngineError, match="cannot copy db"):
        working.package(empty, ImageFormat.PHYSICAL, tmp_path)


def test_fixup_failures(source: MongoScratch) -> None:
    edge = Edge(
        name="bad",
        child=TableName.of(source.database, "child"),
        columns=(ColumnName("parent_id"),),
        parent=TableName.of(source.database, "parent"),
        parent_columns=(ColumnName("_id"),),
        when='{"$bogus": 1}',
    )
    with source.client() as client:
        for mode in (EdgeMode.NULL, EdgeMode.STUB):
            with pytest.raises(EngineError, match="applying the fallback for bad failed"):
                apply_fixup(
                    client[source.database], Fixup(edge=edge, mode=mode, parent_present=True)
                )
