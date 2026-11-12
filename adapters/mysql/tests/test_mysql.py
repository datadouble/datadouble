# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""MySQL behaviour beyond the conformance suite: fallbacks, hooks, odd columns, failures."""

import datetime as dt
import json
from collections.abc import Generator
from pathlib import Path

import pymysql
import pytest

from datadouble.catalog import CatalogLock, ColumnKind
from datadouble.engine import EngineError, Tooling, Transformation, WorkingDatabases
from datadouble.images import unpack
from datadouble.names import ColumnName, ColumnRef, Generation, TableName
from datadouble.planner import Plan, PlannedRule, PlanOptions
from datadouble.registry import Registry, installed
from datadouble.run import Run, RunOptions, RunResult, Stage
from datadouble.spec import HashOptions, HashRule, Strategy, parse_spec
from datadouble.storage import filesystem_store
from datadouble.subset import AllRows, Predicate
from datadouble_conformance.engine import runtime_for_tests
from datadouble_mysql import MysqlEngine
from datadouble_mysql.server import MysqlServer, Tools, default_image
from datadouble_mysql.source import MysqlSession
from test_mysql_conformance import MysqlScratch, scratch

pytestmark = pytest.mark.integration

SCHEMA = [
    "CREATE TABLE parent (id INT PRIMARY KEY, name VARCHAR(20) NOT NULL, born DATE NOT NULL)",
    """CREATE TABLE child (
        id INT PRIMARY KEY,
        parent_id INT,
        other_id INT,
        kind VARCHAR(5),
        label VARCHAR(20),
        changed TIMESTAMP NOT NULL DEFAULT '2001-01-01 00:00:00' ON UPDATE CURRENT_TIMESTAMP,
        doubled INT GENERATED ALWAYS AS (id * 2) VIRTUAL,
        CONSTRAINT fk_child_parent FOREIGN KEY (parent_id) REFERENCES parent (id)
    )""",
    "CREATE TABLE files (id INT PRIMARY KEY, data BLOB)",
    "CREATE TABLE child_log (child_id INT, note VARCHAR(20))",
    """CREATE TRIGGER child_changed AFTER UPDATE ON child FOR EACH ROW
        INSERT INTO child_log (child_id, note) VALUES (NEW.id, 'changed')""",
    "CREATE VIEW item_ids AS SELECT id FROM items",
    (
        "INSERT INTO parent VALUES"
        " (1, 'Ann', '2000-01-01'), (2, 'Bo', '2000-01-02'), (3, 'Cy', '2000-01-03')"
    ),
    (
        "INSERT INTO child (id, parent_id, other_id, kind, label)"
        " VALUES (1, 1, 7, 'x', 'a'), (2, 2, 3, 'y', 'b'), (3, 3, 2, 'x', 'c')"
    ),
    "INSERT INTO files VALUES (1, X'00090A5C0DFF41')",
]

SPEC = """
name: scratch
source: {engine: mysql, connection: CONNECTION}
exclude: ["items"]
subset:
  seeds: {child: "id <= 2", files: "id > 0"}
  edges:
    child.parent_id -> parent: {on_missing: stub}
    child.other_id -> parent: {on_missing: null}
  virtual_fks:
    child.other_id -> parent: {when: "kind = 'x'"}
hooks:
  before_load: [{sql: "SELECT 1"}]
  after_transform: [{sql: "UPDATE child SET label = 'done', changed = changed"}]
anonymize:
  parent: {name: {faker: first_name}}
  child: {kind: {expression: "UPPER(kind)"}}
store:
  - filesystem: {path: STORE}
"""


@pytest.fixture
def source() -> Generator[MysqlScratch]:
    with scratch() as made:
        assert isinstance(made, MysqlScratch)
        for statement in SCHEMA:
            made.execute(statement)
        yield made


def registry() -> Registry:
    return Registry(
        engines={"mysql": MysqlEngine},
        stores={"filesystem": filesystem_store},
        runtimes=installed().runtimes,
    )


def tooling() -> Tooling:
    ((kind, settings),) = runtime_for_tests().items()
    return Tooling(runtime=installed().runtime(kind, settings))


def working_databases(scratch_dir: Path) -> WorkingDatabases:
    return MysqlEngine().working(scratch_dir, tooling(), "8.4.13")


def build(
    source: MysqlScratch,
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
    with MysqlEngine().connect(source.connection) as session:
        lock = CatalogLock.of(session.catalog())
    run_options = RunOptions(
        stages=stages, work=tmp_path / "work", resume=resume, plan=options or PlanOptions()
    )
    return Run(parsed, lock, registry(), run_options).execute()


def table(source: MysqlScratch, name: str) -> TableName:
    return TableName.of(source.database, name)


def test_fallbacks_hooks_and_odd_columns(source: MysqlScratch, tmp_path: Path) -> None:
    result = build(source, tmp_path)
    assert not result.failed(), result.verification
    (summary,) = result.images
    working = working_databases(tmp_path / "check")
    for archive in summary.archives:
        image, _ = unpack(tmp_path / "store" / archive.key, tmp_path / f"unpacked-{archive.format}")
        with working.restore(
            image, archive.format, tmp_path / f"check-{archive.format}"
        ) as restored:
            parents, children = table(source, "parent"), table(source, "child")
            assert restored.counts([parents, children]) == {parents: 2, children: 2}
            # Children 1 and 2 are chosen; parents are not followed, so stubs stand in.
            born = ColumnRef(table=parents, column=ColumnName("born"))
            assert set(restored.text_values(born)) == {"1970-01-01"}
            # Child 1 is a kind 'x' row whose other parent (7) does not exist: nulled.
            other = ColumnRef(table=children, column=ColumnName("other_id"))
            assert set(restored.text_values(other)) == {"3"}
            label = ColumnRef(table=children, column=ColumnName("label"))
            assert set(restored.text_values(label)) == {"done"}
            kinds = ColumnRef(table=children, column=ColumnName("kind"))
            assert set(restored.text_values(kinds)) == {"X", "Y"}
            # Anonymizing and the hook left the ON UPDATE column, and the
            # trigger's table, as they were; the generated column survives.
            changed = ColumnRef(table=children, column=ColumnName("changed"))
            assert set(restored.text_values(changed)) == {"2001-01-01 00:00:00"}
            assert restored.failing_rows("SELECT 1 FROM child_log") == 0
            assert restored.failing_rows("SELECT 1 FROM child WHERE doubled <> id * 2") == 0
            # The view over the excluded table is left out.
            with pytest.raises(EngineError, match=r"\[1146\]"):
                restored.failing_rows("SELECT 1 FROM item_ids")
            blob = "SELECT 1 FROM files WHERE data <> X'00090A5C0DFF41'"
            assert restored.failing_rows(blob) == 0
            with pytest.raises(EngineError, match=r"running an assertion failed: \[1054\]"):
                restored.failing_rows("SELECT nope FROM child")
            nope = ColumnRef(table=children, column=ColumnName("nope"))
            with pytest.raises(EngineError, match=r"scanning .*child\.nope failed"):
                list(restored.text_values(nope))


def test_failing_hooks_name_objects_not_values(source: MysqlScratch, tmp_path: Path) -> None:
    broken = SPEC.replace(
        "UPDATE child SET label = 'done', changed = changed", "UPDATE parent SET name = NULL"
    )
    with pytest.raises(EngineError, match=r"an after_transform hook failed: \[1048\]"):
        build(source, tmp_path, spec=broken)
    before = SPEC.replace("SELECT 1", "SELEKT 1")
    with pytest.raises(EngineError, match=r"a before_load hook failed: \[1064\]"):
        build(source, tmp_path / "again", spec=before)


def test_bad_seeds_fail_in_both_strategies(source: MysqlScratch, tmp_path: Path) -> None:
    bad = SPEC.replace('"id <= 2"', '"nope <= 2"')
    for strategy in (Strategy.NESTED, Strategy.KEYS):
        with pytest.raises(EngineError, match=r"(extracting|reading keys of) .*\.\w+ failed"):
            build(
                source,
                tmp_path / str(strategy),
                spec=bad,
                stages=(Stage.PLAN, Stage.EXTRACT),
                options=PlanOptions(strategy=strategy),
            )


def test_schema_changes_after_planning_are_caught(source: MysqlScratch, tmp_path: Path) -> None:
    planned = build(source, tmp_path, stages=(Stage.PLAN,))
    source.add_column("late")
    with pytest.raises(EngineError, match="schema changed between planning and the snapshot"):
        build(source, tmp_path, stages=(Stage.EXTRACT,), resume=planned.generation)


def test_excluded_parents_are_nulled(source: MysqlScratch, tmp_path: Path) -> None:
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


def test_corrupt_extracts_fail_to_load(source: MysqlScratch, tmp_path: Path) -> None:
    extracted = build(source, tmp_path, stages=(Stage.PLAN, Stage.EXTRACT))
    data = tmp_path / "work" / "scratch" / extracted.generation / "extract" / "data"
    for file in data.glob("*.tsv"):
        file.write_text("not\ta\trow\tat\tall\tof\tthe\tright\tshape\n", encoding="utf-8")
    with pytest.raises(EngineError, match=r"loading .*\.\w+ failed: \[\d+\]"):
        build(source, tmp_path, stages=(Stage.LOAD,), resume=extracted.generation)


def test_closed_connections_and_snapshot_reads(source: MysqlScratch) -> None:
    with MysqlEngine().connect(source.connection) as session:
        assert isinstance(session, MysqlSession)
        with session.snapshot(dt.datetime.now(tz=dt.UTC)) as snapshot:
            child = table(source, "child")
            assert len(list(snapshot.project(child, AllRows(), (ColumnName("id"),)))) == 3
            with pytest.raises(EngineError, match=r"reading keys of .*child failed"):
                snapshot.project(child, Predicate("nope = 1"), (ColumnName("id"),))
        with (
            pytest.raises(EngineError, match=r"reading the snapshot failed: \[1\] boom"),
            session.snapshot(dt.datetime.now(tz=dt.UTC)),
        ):
            raise pymysql.err.OperationalError(1, "boom")
        session.connection.close()
        with pytest.raises(EngineError, match="reading the catalog failed"):
            session.catalog()
        session.settings["password"] += "-but-wrong"
        with (
            pytest.raises(EngineError, match="opening the snapshot failed"),
            session.snapshot(dt.datetime.now(tz=dt.UTC)),
        ):
            pass


def test_bad_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DD_BAD_URL", "mysql://nobody:wrong@mysql:3306/nowhere")
    with (
        pytest.raises(EngineError, match=r"connecting to the source failed"),
        MysqlEngine().connect({"url": {"env": "DD_BAD_URL"}}),
    ):
        pass


def test_working_failures(source: MysqlScratch, tmp_path: Path) -> None:
    loaded = build(source, tmp_path, stages=(Stage.PLAN, Stage.EXTRACT, Stage.LOAD))
    work = tmp_path / "work" / "scratch" / loaded.generation
    plan = Plan.model_validate_json((work / "plan" / "plan.json").read_bytes())
    working = working_databases(work / "scratch")
    missing = PlannedRule(
        column=ColumnRef(table=table(source, "child"), column=ColumnName("nope")),
        kind=ColumnKind.TEXT,
        type="text",
        rule=HashRule(hash=HashOptions()),
    )
    transformation = Transformation((missing,), lambda _rule, _values: {}, ())
    with pytest.raises(EngineError, match="cannot copy missing"):
        working.transform(plan.groups[0], work / "missing", tmp_path / "t1", transformation)
    with pytest.raises(EngineError, match=r"reading .*child\.nope failed"):
        working.transform(
            plan.groups[0], work / "load" / "all" / "db", tmp_path / "t2", transformation
        )
    # A replacement larger than the server accepts in one packet.
    label = missing.model_copy(
        update={"column": ColumnRef(table=table(source, "child"), column=ColumnName("label"))}
    )
    oversized = Transformation(
        (label,), lambda _rule, values: dict.fromkeys(values, "x" * 70_000_000), ()
    )
    with pytest.raises(EngineError, match="preparing replacements failed"):
        working.transform(plan.groups[0], work / "load" / "all" / "db", tmp_path / "t3", oversized)


def test_working_servers_need_their_password(tmp_path: Path) -> None:
    tools = Tools(tooling().runtime, default_image("8.4.13"))
    server = MysqlServer(tools, tmp_path / "db" / "mysql", tmp_path / "db")
    server.initialize()
    running = server.running()
    running.__enter__()
    with server.connect() as connection:
        connection.cursor().execute("ALTER USER 'datadouble-build'@'%' IDENTIFIED BY 'x'")
    refused = r"connecting to the working database failed: \[1045\]"
    with pytest.raises(EngineError, match=refused):
        server.connect()
    # Stopping needs the build user too, to drop it.
    with pytest.raises(EngineError, match=refused):
        running.__exit__(None, None, None)


def test_the_build_user_must_be_removable(tmp_path: Path) -> None:
    tools = Tools(tooling().runtime, default_image("8.4.13"))
    server = MysqlServer(tools, tmp_path / "db" / "mysql", tmp_path / "db")
    server.initialize()
    with (
        pytest.raises(EngineError, match="removing the build user failed"),
        server.running(),
        server.connect() as connection,
    ):
        connection.cursor().execute("SET GLOBAL super_read_only = ON")
