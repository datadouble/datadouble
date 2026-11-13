# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Redis behavior beyond the conformance suite: fallbacks, rules, assertions and failures."""

import dataclasses
import datetime as dt
import json
import shutil
from collections.abc import Generator
from pathlib import Path

import pytest

from datadouble.catalog import CatalogLock, ColumnKind
from datadouble.engine import EngineError, Transformation
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
    NullOptions,
    NullRule,
    Rule,
    TransformHook,
    parse_spec,
)
from datadouble.storage import filesystem_store
from datadouble.subset import AllRows, Edge, EdgeMode
from datadouble_conformance.engine import runtime_for_tests
from datadouble_redis import RedisEngine
from datadouble_redis.driver import Client, texts
from datadouble_redis.server import RedisServer, Tools, default_image
from datadouble_redis.source import ExtractIndex, Layout
from datadouble_redis.working import Keyspace, RedisRestored, RedisWorking, apply_fixup
from test_redis_conformance import RedisScratch, scratch

pytestmark = pytest.mark.integration

VERSION = "8.10.3"

SPEC = """
name: scratch
source: {engine: redis, connection: CONNECTION}
exclude: ["gone:{}"]
subset:
  seeds:
    "child:{}": "[12]"
    "other:{}": "1"
    "tags:{}": "*"
    "queue:{}": "*"
    "rank:{}": "*"
    "note:{}": "*"
    "ref:{}": "*"
    "doc:{}": "*"
    "words:{}": "*"
    "names:{}": "*"
    "scores:{}": "*"
    log: "*"
  edges:
    "child:{}.parent_id -> parent:{}": {on_missing: stub}
    "child:{}.lost_id -> gone:{}": {on_missing: null}
    "tags:{}.members -> parent:{}": {on_missing: null}
    "queue:{}.members -> parent:{}": {on_missing: null}
    "rank:{}.members -> parent:{}": {on_missing: null}
    "ref:{}.value -> parent:{}": {on_missing: null}
  virtual_fks:
    "child:{}.parent_id -> parent:{}": {}
    "child:{}.lost_id -> gone:{}": {}
    "tags:{}.members -> parent:{}": {}
    "queue:{}.members -> parent:{}": {}
    "rank:{}.members -> parent:{}": {}
    "ref:{}.value -> parent:{}": {when: "1"}
    "other:{}.code -> parent:{}": {when: "1"}
anonymize:
  "parent:{}": {code: {hash: {}}}
  "child:{}":
    label: {expression: "redis.call('HSET', KEYS[1], ARGV[1], 'hidden')", where: "1"}
    missing: {null: {}}
  "note:{}": {value: {hash: {length: 8}}}
  "words:{}": {members: {hash: {length: 8}}}
  "names:{}": {members: {hash: {length: 8}}}
  "scores:{}": {members: {hash: {length: 8}}}
  log: {ip: {hash: {length: 8}}}
verify:
  assertions:
    - name: no more than two children
      sql: "local n = #redis.call('KEYS', 'child:*'); if n > 2 then return n end; return 0"
store:
  - filesystem: {path: STORE}
"""


@pytest.fixture
def source() -> Generator[RedisScratch]:
    with scratch() as made:
        assert isinstance(made, RedisScratch)
        for command in [
            ("HSET", "parent:1", "code", "41"),
            ("HSET", "parent:2", "code", "42"),
            ("HSET", "child:1", "parent_id", "1", "lost_id", "9", "label", "a", "missing", "m"),
            ("HSET", "child:2", "parent_id", "7", "label", "b"),
            ("HSET", "child:3", "parent_id", "8", "label", "c"),
            ("HSET", "gone:9", "x", "1"),
            ("HSET", "other:1", "code", "2"),
            ("SADD", "tags:1", "1", "7", "9"),
            ("SET", "ref:1", "9"),
            ("SET", "ref:2", "9"),
            ("VADD", "doc:1", "VALUES", "2", "0.1", "0.2", "a"),
            ("RPUSH", "queue:1", "1", "9", "1"),
            ("ZADD", "rank:1", "10", "1", "20", "9"),
            ("SET", "note:1", "secret"),
            ("SADD", "words:1", "alpha", "beta"),
            ("RPUSH", "names:1", "Ann", "Bo"),
            ("ZADD", "scores:1", "1", "Ann", "2", "Bo"),
            ("XADD", "log", "1-1", "ip", "192.0.2.1", "user", "1"),
            ("XADD", "log", "1-2", "ip", "192.0.2.2", "user", "2"),
        ]:
            made.command(*command)
        yield made


def registry() -> Registry:
    return Registry(
        engines={"redis": RedisEngine},
        stores={"filesystem": filesystem_store},
        runtimes=installed().runtimes,
    )


def tools() -> Tools:
    ((kind, settings),) = runtime_for_tests().items()
    return Tools(installed().runtime(kind, settings), default_image(VERSION))


def build(
    source: RedisScratch,
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
    with RedisEngine().connect(source.connection) as session:
        lock = CatalogLock.of(session.catalog())
    options = RunOptions(stages=stages, work=tmp_path / "work", resume=resume, plan=PlanOptions())
    return Run(parsed, lock, registry(), options).execute()


def table(source: RedisScratch, name: str) -> TableName:
    return TableName.of(f"db{source.database}", name)


def test_fallbacks_rules_and_assertions(source: RedisScratch, tmp_path: Path) -> None:
    result = build(source, tmp_path)
    assert not result.failed(), result.verification
    layout = Layout(databases=(source.database,), separator=":", patterns=("items:{}",))
    data = Path(result.work) / "transform" / "all" / "db"
    server = RedisServer(tools(), data / "data", tmp_path / "check", 16)
    with server.running():
        client = server.client(source.database)
        keys = sorted(texts(client.command("KEYS", "*")))
        child = texts(client.command("HGETALL", "child:1"))
        parents = {key for key in keys if key.startswith("parent:")}
        tags = texts(client.command("SMEMBERS", "tags:1"))
        queue = texts(client.command("LRANGE", "queue:1", "0", "-1"))
        rank = texts(client.command("ZRANGE", "rank:1", "0", "-1"))
        words = texts(client.command("SMEMBERS", "words:1"))
        log = str(client.command("XRANGE", "log", "-", "+"))
        client.close()
    # Parent 2 comes from other:1; the parents of children 1 and 2 are not
    # followed and become stubs; references into gone:{} are removed, and so
    # are members naming parents that are missing.
    assert parents == {"parent:1", "parent:2", "parent:7"}
    assert dict(zip(child[::2], child[1::2], strict=True)) == {"parent_id": "1", "label": "hidden"}
    assert sorted(tags) == ["1", "7"]
    assert queue == ["1", "1"]
    assert rank == ["1"]
    assert "child:3" not in keys
    # The reference in ref:1 named a missing parent and the key went; ref:2's
    # does not refer (its `when` glob does not match), so it stays.
    assert "ref:1" not in keys
    assert "ref:2" in keys
    assert "doc:1" in keys
    assert not {"alpha", "beta"} & set(words)
    assert "192.0.2.1" not in log
    assert "1-1" in log
    assert "secret" not in str(keys)
    assert layout.parsed()[0].text == "items:{}"


def test_restored_checks(source: RedisScratch, tmp_path: Path) -> None:
    result = build(source, tmp_path, stages=(Stage.PLAN, Stage.EXTRACT, Stage.LOAD))
    layout = Layout(databases=(source.database,), separator=":", patterns=())
    server = RedisServer(
        tools(), Path(result.work) / "load" / "all" / "db" / "data", tmp_path / "c", 16
    )
    with server.running():
        restored = RedisRestored(Keyspace(server, layout))
        child, parent = table(source, "child:{}"), table(source, "parent:{}")
        edge = Edge(
            name="e",
            child=child,
            columns=(ColumnName("parent_id"),),
            parent=parent,
            parent_columns=(ColumnName("_key"),),
        )
        assert restored.orphans(edge) == 0
        server.client(source.database).command("HSET", "child:1", "parent_id", "99")
        assert restored.orphans(edge) == 1
        assert restored.orphans(dataclasses.replace(edge, when="2")) == 0
        values = set(restored.text_values(ColumnRef(table=child, column=ColumnName("label"))))
        assert values == {"a", "b"}
        assert restored.counts([child]) == {child: 2}
        assert restored.failing_rows("return 3") == 3
        with pytest.raises(EngineError, match="running an assertion failed"):
            restored.failing_rows("error(")


def test_source_failures(source: RedisScratch, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = RedisEngine()
    monkeypatch.setenv("NOWHERE", "redis://127.0.0.1:1")
    with (
        pytest.raises(EngineError, match="connecting to the source failed"),
        engine.connect({"url": {"env": "NOWHERE"}}),
    ):
        pass
    monkeypatch.setenv("NOWHERE", "http://example.com")
    with (
        pytest.raises(EngineError, match="a Redis URL is"),
        engine.connect({"url": {"env": "NOWHERE"}}),
    ):
        pass
    with engine.connect(source.connection) as session:
        for number in range(70):
            source.command("SET", f"kind{number}", "1")
        with pytest.raises(EngineError, match="look like data"):
            session.catalog()


def test_schema_changes_after_planning(source: RedisScratch, tmp_path: Path) -> None:
    planned = build(source, tmp_path, stages=(Stage.PLAN,))
    source.command("HSET", "child:1", "added", "x")
    with pytest.raises(EngineError, match="plan again"):
        build(source, tmp_path, stages=(Stage.EXTRACT,), resume=Generation(planned.generation))


def test_working_failures(source: RedisScratch, tmp_path: Path) -> None:
    loaded = build(source, tmp_path, stages=(Stage.PLAN, Stage.EXTRACT, Stage.LOAD))
    work = Path(loaded.work)
    plan = Plan.model_validate_json((work / "plan" / "plan.json").read_bytes())
    group = plan.groups[0]
    working = RedisWorking(tools(), tmp_path / "scratch")
    assert working.server_version() == VERSION
    extract = work / "extract" / "data"
    with pytest.raises(EngineError, match="does not run hooks"):
        working.load(plan, group, extract, tmp_path / "x", (Hook(sql="x"),))
    index = ExtractIndex.model_validate_json((extract / "index.json").read_bytes())
    broken = tmp_path / "broken"
    shutil.copytree(extract, broken)
    (broken / index.tables[0].file).write_bytes(b"\x00\x00\x00\x01\x00\x00\x00\x02xab")
    with pytest.raises(EngineError, match="loading failed"):
        working.load(plan, group, broken, tmp_path / "broken-db", ())
    loaded_db = work / "load" / "all" / "db"

    def rule(name: str, column: str, chosen: Rule) -> PlannedRule:
        return PlannedRule(
            column=ColumnRef(table=table(source, name), column=ColumnName(column)),
            kind=ColumnKind.TEXT,
            type="string",
            rule=chosen,
        )

    def transform(target: str, *rules: PlannedRule) -> None:
        mapper = Transformation(rules, lambda _r, values: {v: "x" + v for v in values}, ())
        working.transform(group, loaded_db, tmp_path / target, mapper)

    cleared = NullRule(null=NullOptions())
    transform("t1", rule("tags:{}", "members", cleared), rule("note:{}", "value", cleared))
    transform("t2", rule("queue:{}", "members", HashRule(hash=HashOptions())))
    cases: list[tuple[str, str, Rule, str]] = [
        ("log", "ip", cleared, "a null rule cannot clear stream"),
        ("tags:{}", "_key", cleared, "a null rule cannot clear set _key"),
        ("note:{}", "missing", HashRule(hash=HashOptions()), ""),
        ("rank:{}", "scores", HashRule(hash=HashOptions()), "cannot rewrite zset scores"),
        ("child:{}", "label", ExpressionRule(expression="error("), "anonymizing .* failed"),
    ]
    for name, column, chosen, problem in cases:
        if not problem:
            transform("t3", rule(name, column, chosen))
            continue
        with pytest.raises(EngineError, match=problem):
            transform(f"t-{column}", rule(name, column, chosen))
    with pytest.raises(EngineError, match="does not run hooks"):
        working.transform(
            group,
            loaded_db,
            tmp_path / "t4",
            Transformation((), lambda _r, _v: {}, (TransformHook(sql="x"),)),
        )
    for image_format in (ImageFormat.LOGICAL,):
        with pytest.raises(EngineError, match="only physical images"):
            working.package(loaded_db, image_format, tmp_path / "p")
        with (
            pytest.raises(EngineError, match="only physical images"),
            working.restore(loaded_db, image_format, tmp_path / "r"),
        ):
            pass


def test_fixup_failures(source: RedisScratch, tmp_path: Path) -> None:
    result = build(source, tmp_path, stages=(Stage.PLAN, Stage.EXTRACT, Stage.LOAD))
    layout = Layout(databases=(source.database,), separator=":", patterns=())
    server = RedisServer(
        tools(), Path(result.work) / "load" / "all" / "db" / "data", tmp_path / "c", 16
    )
    with server.running():
        database = Keyspace(server, layout)
        child, parent = table(source, "tags:{}"), table(source, "parent:{}")
        by_key = Edge(
            name="by-key",
            child=table(source, "child:{}"),
            columns=(ColumnName("_key"),),
            parent=parent,
            parent_columns=(ColumnName("_key"),),
        )
        with pytest.raises(EngineError, match="cannot null a reference held in hash _key"):
            apply_fixup(database, Fixup(edge=by_key, mode=EdgeMode.NULL, parent_present=False))
        wide = Edge(
            name="wide",
            child=child,
            columns=(ColumnName("members"), ColumnName("x")),
            parent=parent,
            parent_columns=(ColumnName("_key"), ColumnName("x")),
        )
        with pytest.raises(EngineError, match="names one column"):
            apply_fixup(database, Fixup(edge=wide, mode=EdgeMode.NULL, parent_present=True))
        database.close()


def test_served_copies(tmp_path: Path) -> None:
    image = tmp_path / "image"
    image.mkdir()
    layout = Layout(databases=(3,), separator=":", patterns=())
    (image / "database.json").write_text(
        json.dumps({"layout": layout.model_dump(mode="json"), "server_version": VERSION}), "utf-8"
    )
    plan = RedisEngine().server(image, None)
    assert plan.image == default_image(VERSION)
    assert (plan.user, plan.database, plan.port) == ("default", "3", 6379)
    assert "--save" not in plan.command[-1]
    client = Client.at("127.0.0.1", 1, "x", 0)
    client.close()


def test_large_keyspaces_scan_in_rounds(source: RedisScratch) -> None:
    for start in range(0, 2500, 500):
        client = Client.from_url(source.url, source.database)
        client.commands([("SET", f"bulk:{n}", "x") for n in range(start, start + 500)])
        client.close()
    connection = {**source.connection, "sample": 3}
    with RedisEngine().connect(connection) as session:
        catalog = session.catalog()
        bulk = table(source, "bulk:{}")
        assert catalog.table(bulk) is not None
        with session.snapshot(dt.datetime.now(tz=dt.UTC)) as snapshot:
            keys = snapshot.project(bulk, AllRows(), (ColumnName("_key"),))
            assert len(set(keys)) == 2500
