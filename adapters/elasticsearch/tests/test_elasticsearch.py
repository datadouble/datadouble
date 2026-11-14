# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Elasticsearch behavior beyond the conformance suite: fallbacks, assertions and failures."""

import datetime as dt
import json
import os
import shutil
from collections.abc import Callable, Generator, Mapping
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import JsonValue

from datadouble.catalog import CatalogLock, ColumnKind
from datadouble.engine import EngineError, Tooling, Transformation
from datadouble.names import ColumnName, ColumnRef, Generation, TableName
from datadouble.planner import Fixup, Plan, PlannedRule, PlanOptions
from datadouble.registry import Registry, installed
from datadouble.run import Run, RunOptions, RunResult, Stage
from datadouble.runtime import Container
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
from datadouble.subset import AllRows, Edge, EdgeMode, Predicate
from datadouble_conformance.engine import runtime_for_tests
from datadouble_elasticsearch import ElasticEngine
from datadouble_elasticsearch.client import Client, Document, document, items, path
from datadouble_elasticsearch.documents import Dump
from datadouble_elasticsearch.server import (
    LOGIN_ENV,
    PORT,
    ElasticServer,
    Tools,
    default_image,
    ready_command,
)
from datadouble_elasticsearch.source import ElasticSource, Layout, server_version
from datadouble_elasticsearch.working import (
    DATA,
    ElasticRestored,
    ElasticWorking,
    apply_fixup,
    serve_command,
)
from test_elasticsearch_conformance import URL

pytestmark = pytest.mark.integration

VERSION = "9.5.5"
INDICES = ("parent", "child", "gone", "other")
SPEC = """
name: scratch
source: {engine: elasticsearch, connection: CONNECTION}
exclude: ["gone"]
subset:
  seeds: {child: '{"range": {"n": {"lte": 2}}}', other: '{"ids": {"values": ["1"]}}'}
  edges:
    child.parent_id -> parent: {on_missing: stub}
    child.lost_id -> gone: {on_missing: null}
  virtual_fks:
    child.parent_id -> parent: {}
    child.lost_id -> gone: {}
    other.code -> parent: {when: '{"term": {"kind": "x"}}'}
anonymize:
  parent: {code: {hash: {}}}
  child:
    label: {expression: "ctx._source.label = 'hidden'", where: '{"term": {"n": 1}}'}
    missing: {null: {}}
verify:
  assertions:
    - name: no children over 2
      sql: '{"index": "child", "query": {"range": {"n": {"gt": 2}}}}'
store:
  - filesystem: {path: STORE}
"""


class Scratch:
    """The test indices on the test cluster."""

    def __init__(self) -> None:
        """Connect to the test cluster."""
        self.client = Client.from_url(os.environ[URL])

    @property
    def connection(self) -> Mapping[str, JsonValue]:
        """Settings naming the test indices."""
        return {"url": {"env": URL}, "indices": list(INDICES)}

    def drop(self) -> None:
        """Delete the test indices."""
        self.client.request(
            "DELETE", path(",".join(INDICES)), "deleting", params={"ignore_unavailable": True}
        )


KEYWORD: JsonValue = {"type": "keyword"}


@pytest.fixture
def source() -> Generator[Scratch]:
    made = Scratch()
    made.drop()
    longs: JsonValue = {"properties": {"n": {"type": "long"}, "code": {"type": "long"}}}
    made.client.request("PUT", path("parent"), "creating", body={"mappings": longs})
    made.client.request(
        "PUT",
        path("child"),
        "creating",
        body={
            "mappings": {
                "properties": {
                    "n": {"type": "long"},
                    "parent_id": KEYWORD,
                    "lost_id": KEYWORD,
                    "label": KEYWORD,
                    "missing": KEYWORD,
                }
            }
        },
    )
    made.client.request("PUT", path("gone"), "creating")
    made.client.request(
        "PUT",
        path("other"),
        "creating",
        body={"mappings": {"properties": {"kind": KEYWORD, "code": KEYWORD}}},
    )
    made.client.bulk(
        [
            {"index": {"_index": "parent", "_id": "1"}},
            {"code": 41},
            {"index": {"_index": "parent", "_id": "2"}},
            {"code": 42},
            {"index": {"_index": "child", "_id": "1"}},
            {"n": 1, "parent_id": "1", "lost_id": "9", "label": "a", "missing": "m"},
            {"index": {"_index": "child", "_id": "2"}},
            {"n": 2, "parent_id": "7", "label": "b"},
            {"index": {"_index": "child", "_id": "3"}},
            {"n": 3, "parent_id": "8", "label": "c"},
            {"index": {"_index": "gone", "_id": "9"}},
            {},
            {"index": {"_index": "other", "_id": "1"}},
            {"kind": "x", "code": "2"},
        ],
        "seeding",
    )
    made.client.request("POST", path(",".join(INDICES), "_refresh"), "refreshing")
    try:
        yield made
    finally:
        made.drop()
        made.client.close()


def registry() -> Registry:
    return Registry(
        engines={"elasticsearch": ElasticEngine},
        stores={"filesystem": filesystem_store},
        runtimes=installed().runtimes,
    )


def tools() -> Tools:
    ((kind, settings),) = runtime_for_tests().items()
    return Tools(installed().runtime(kind, settings), default_image(VERSION))


def build(
    source: Scratch,
    tmp_path: Path,
    *,
    stages: tuple[Stage, ...] = tuple(Stage),
    resume: Generation | None = None,
    sample: int = 1000,
) -> RunResult:
    connection = {**source.connection, "sample": sample}
    parsed = parse_spec(
        SPEC.replace("CONNECTION", json.dumps(connection)).replace("STORE", str(tmp_path / "store"))
        + f"\nruntime: {json.dumps(runtime_for_tests())}\n"
    )
    with ElasticEngine().connect(connection) as session:
        lock = CatalogLock.of(session.catalog())
    options = RunOptions(stages=stages, work=tmp_path / "work", resume=resume, plan=PlanOptions())
    return Run(parsed, lock, registry(), options).execute()


def replying(reply: Document) -> Callable[..., Document]:
    """A stand-in for :meth:`Client.request` that always gives ``reply``."""

    def request(*_arguments: object, **_options: object) -> Document:
        return reply

    return request


def table(name: str) -> TableName:
    return TableName.of("", name)


def sources(client: Client, index: str) -> dict[str, Document]:
    reply = client.request("POST", path(index, "_search"), "reading", body={"size": 100})
    hits = items(document(reply.get("hits"), "hits").get("hits"), "hits")
    return {
        str(found.get("_id")): document(found.get("_source"), "a source")
        for found in (document(hit, "a hit") for hit in hits)
    }


def test_fallbacks_rules_and_assertions(source: Scratch, tmp_path: Path) -> None:
    result = build(source, tmp_path)
    assert not result.failed(), result.verification
    working = ElasticWorking(tools(), tmp_path / "check")
    transformed = Path(result.work) / "transform" / "all" / "db"
    server = ElasticServer(tools(), transformed / DATA, tmp_path / "check")
    with server.running() as client:
        parents = sources(client, "parent")
        children = sources(client, "child")
    # Parent 2 comes from `other`; the parents of children 1 and 2 are not
    # followed and become stubs; references into "gone" are nulled.
    assert set(parents) == {"1", "2", "7"}
    assert parents["7"] == {}
    assert parents["2"]["code"] not in {None, 42}
    assert parents["1"] == {}
    assert children["1"]["lost_id"] is None
    assert children["1"]["missing"] is None
    assert children["1"]["label"] == "hidden"
    assert children["2"]["label"] == "b"
    assert set(children) == {"1", "2"}
    assert working.server_version() == VERSION


def test_restored_checks(source: Scratch) -> None:
    restored = ElasticRestored(source.client)
    assert restored.failing_rows('{"index": "child", "query": {"match_all": {}}}') == 3
    with pytest.raises(EngineError, match="an Elasticsearch assertion is"):
        restored.failing_rows('{"query": {}}')
    with pytest.raises(EngineError, match=r"running an assertion failed: \w+ \(HTTP 400\)"):
        restored.failing_rows('{"index": "child", "query": {"bogus": {}}}')
    edge = Edge(
        name="virtual: other.code -> parent",
        child=table("other"),
        columns=(ColumnName("code"),),
        parent=table("parent"),
        parent_columns=(ColumnName("_id"),),
        when='{"term": {"kind": "x"}}',
    )
    assert restored.orphans(edge) == 0
    by_field = replace(edge, parent_columns=(ColumnName("code"),))
    assert restored.orphans(by_field) == 1
    to_ids = Edge(
        name="child.parent_id -> parent",
        child=table("child"),
        columns=(ColumnName("parent_id"),),
        parent=table("parent"),
        parent_columns=(ColumnName("_id"),),
    )
    assert restored.orphans(to_ids) == 2
    ids = list(restored.text_values(ColumnRef(table=table("parent"), column=ColumnName("_id"))))
    assert sorted(ids) == ["1", "2"]
    labels = restored.text_values(ColumnRef(table=table("child"), column=ColumnName("label")))
    assert sorted(labels) == ["a", "b", "c"]
    composite = Edge(
        name="pair",
        child=edge.child,
        columns=(ColumnName("a"), ColumnName("b")),
        parent=edge.parent,
        parent_columns=(ColumnName("a"), ColumnName("b")),
    )
    with pytest.raises(EngineError, match="one field on each side"):
        restored.orphans(composite)


def test_source_failures(source: Scratch, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DD_BAD_ES", "http://127.0.0.1:1")
    with (
        pytest.raises(EngineError, match="reading the server version failed: ConnectionError"),
        ElasticEngine().connect({"url": {"env": "DD_BAD_ES"}}),
    ):
        pass
    monkeypatch.setenv("DD_WRONG_ES", os.environ[URL].replace("Datadouble-1", "wrong"))
    with (
        pytest.raises(EngineError, match=r"security_exception \(HTTP 401\)"),
        ElasticEngine().connect({"url": {"env": "DD_WRONG_ES"}}),
    ):
        pass
    with ElasticEngine().connect(source.connection) as session:
        assert isinstance(session, ElasticSource)
        with (
            session.snapshot(dt.datetime.now(tz=dt.UTC)) as snapshot,
            pytest.raises(EngineError, match=r"searching child failed: \w+ \(HTTP 400\)"),
        ):
            list(snapshot.project(table("child"), Predicate('{"bogus": {}}'), (ColumnName("n"),)))
    # A document that does not survive the bulk load names its error, not its value.
    with pytest.raises(
        EngineError, match=r"seeding failed: document_parsing_exception \(HTTP 400\)"
    ):
        source.client.bulk([{"index": {"_index": "child"}}, {"n": "alice@example.com"}], "seeding")
    # Unreadable sources: data streams, indices without _source, OpenSearch.
    source.client.request(
        "PUT",
        "/_index_template/datadouble-test",
        "templating",
        body={"index_patterns": ["gone-stream*"], "data_stream": {}},
    )
    source.client.request("PUT", "/_data_stream/gone-stream", "streaming")
    try:
        streams = Layout(indices=("gone-*",), sample=10)
        with pytest.raises(EngineError, match="match data streams"):
            ElasticSource(source.client, streams).catalog()
    finally:
        source.client.request("DELETE", "/_data_stream/gone-stream", "unstreaming")
        source.client.request("DELETE", "/_index_template/datadouble-test", "untemplating")
    source.client.request(
        "PUT", path("gone", "_mapping"), "mapping", body={"properties": {"x": KEYWORD}}
    )
    source.client.request("DELETE", path("gone"), "deleting")
    source.client.request(
        "PUT", path("gone"), "creating", body={"mappings": {"_source": {"enabled": False}}}
    )
    with pytest.raises(EngineError, match="gone does not keep whole documents"):
        ElasticSource(source.client, Layout(indices=("gone",), sample=10)).catalog()
    monkeypatch.setattr(Client, "request", replying({"version": {"distribution": "opensearch"}}))
    with pytest.raises(EngineError, match="use the opensearch engine"):
        server_version(source.client)
    monkeypatch.setattr(Client, "request", replying({"version": {}}))
    with pytest.raises(EngineError, match="did not report its version"):
        server_version(source.client)
    assert tmp_path.exists()


def test_schema_changes_after_planning(source: Scratch, tmp_path: Path) -> None:
    planned = build(source, tmp_path, stages=(Stage.PLAN,))
    source.client.request(
        "PUT", path("child", "_mapping"), "mapping", body={"properties": {"late": KEYWORD}}
    )
    with pytest.raises(EngineError, match="changed between planning and the point in time"):
        build(source, tmp_path, stages=(Stage.EXTRACT,), resume=planned.generation)


def test_unlisted_fields_fail_extract(source: Scratch, tmp_path: Path) -> None:
    source.client.request("PUT", path("other", "_mapping"), "closing", body={"dynamic": False})
    planned = build(source, tmp_path, stages=(Stage.PLAN,), sample=1)
    # A field the one-document sample did not see, in a document the extract copies.
    source.client.request(
        "PUT",
        path("other", "_doc", "2"),
        "indexing",
        body={"kind": "y", "secret": {"pin": "1234"}},
        params={"refresh": True},
    )
    plan = Plan.model_validate_json((Path(planned.work) / "plan" / "plan.json").read_bytes())
    selections = dict.fromkeys(plan.extracted(), AllRows())
    with (
        ElasticEngine().connect({**source.connection, "sample": 1}) as session,
        session.snapshot(plan.as_of) as snapshot,
        pytest.raises(EngineError, match=r"holds the field secret\.pin, which the catalog"),
    ):
        snapshot.extract(plan, selections, tmp_path / "extract", Tooling(tools().runtime))


def test_working_failures(source: Scratch, tmp_path: Path) -> None:
    loaded = build(source, tmp_path, stages=(Stage.PLAN, Stage.EXTRACT, Stage.LOAD))
    work = Path(loaded.work)
    plan = Plan.model_validate_json((work / "plan" / "plan.json").read_bytes())
    group = plan.groups[0]
    working = ElasticWorking(tools(), tmp_path / "scratch")
    extract = work / "extract" / "data"
    with pytest.raises(EngineError, match="does not run hooks"):
        working.load(plan, group, extract, tmp_path / "x", (Hook(sql="x"),))
    # An extract without the group's indices leaves nothing to count.
    empty = tmp_path / "empty"
    empty.mkdir()
    Dump(indices=()).write(empty)
    with pytest.raises(EngineError, match=r"counting \w+ failed: index_not_found_exception"):
        working.load(plan, group, empty, tmp_path / "empty-db", ())

    child = table("child")

    def rule(column: str, chosen: Rule) -> PlannedRule:
        return PlannedRule(
            column=ColumnRef(table=child, column=ColumnName(column)),
            kind=ColumnKind.TEXT,
            type="keyword",
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
    bogus = rule("label", ExpressionRule(expression="ctx._source.label = undefined_name"))
    with pytest.raises(EngineError, match=r"anonymizing child.label failed: \w+ \(HTTP 400\)"):
        working.transform(
            group, loaded_db, tmp_path / "t3", Transformation((bogus,), lambda _r, _v: {}, ())
        )
    with pytest.raises(EngineError, match="cannot copy db"):
        working.transform(
            group, tmp_path / "db", tmp_path / "t4", Transformation((), lambda _r, _v: {}, ())
        )


def test_fixup_failures(source: Scratch) -> None:
    edge = Edge(
        name="bad",
        child=table("child"),
        columns=(ColumnName("parent_id"),),
        parent=table("parent"),
        parent_columns=(ColumnName("_id"),),
        when='{"bogus": {}}',
    )
    for mode in (EdgeMode.NULL, EdgeMode.STUB):
        with pytest.raises(EngineError, match="searching child failed"):
            apply_fixup(source.client, Fixup(edge=edge, mode=mode, parent_present=True))


def test_served_copies(source: Scratch, tmp_path: Path) -> None:
    """A copy of a physical image restores its snapshot at first start, then serves it."""
    stages = (Stage.PLAN, Stage.EXTRACT, Stage.LOAD, Stage.TRANSFORM)
    result = build(source, tmp_path, stages=stages)
    image = tmp_path / "image"
    image.mkdir()
    working = ElasticWorking(tools(), tmp_path / "scratch")
    working.package(Path(result.work) / "transform" / "all" / "db", ImageFormat.PHYSICAL, image)
    plan = ElasticEngine().server(image, None)
    assert plan.image == default_image(VERSION)
    assert (plan.user, plan.database, plan.port) == ("elastic", "", PORT)
    # The plan's mount is a fixed path; serve the copy where it is instead.
    copy = tmp_path / "copy"
    shutil.copytree(image, copy)
    command, marker = serve_command(str(copy), "512m")
    container = Container(
        image=default_image(VERSION),
        command=("sh", "-c", command),
        owner=copy,
        mounts=(copy,),
        secrets={LOGIN_ENV: "copy-password"},
        port=PORT,
        ready=ready_command(marker),
    )
    for _start in range(2):
        with tools().runtime.serve(container) as server:
            client = Client.at(server.endpoint.host, server.endpoint.port, "copy-password")
            assert set(sources(client, "child")) == {"1", "2"}
            client.close()
