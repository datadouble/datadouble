# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Oracle behaviour beyond the conformance suite: fallbacks, schema, odd values, failures."""

import datetime as dt
import json
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
from datadouble_conformance.engine import runtime_for_tests
from datadouble_oracle import OracleEngine
from datadouble_oracle.driver import Error
from datadouble_oracle.server import OracleServer, Tools, default_image
from datadouble_oracle.source import OracleSession
from test_oracle_conformance import OracleScratch, scratch

pytestmark = pytest.mark.integration

VERSION = "23.26.3.0.0"

SCHEMA = [
    """CREATE TABLE parent (
        id NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        name VARCHAR2(20) NOT NULL,
        born DATE NOT NULL
    )""",
    """CREATE TABLE child (
        id NUMBER PRIMARY KEY,
        parent_id NUMBER,
        other_id NUMBER,
        kind VARCHAR2(5) CHECK (kind IN ('x', 'y', 'X', 'Y')),
        label VARCHAR2(20),
        changed TIMESTAMP(9) DEFAULT TIMESTAMP '2001-01-01 00:00:00.123456789' NOT NULL,
        zoned TIMESTAMP WITH TIME ZONE,
        took INTERVAL DAY TO SECOND,
        doubled NUMBER GENERATED ALWAYS AS (id * 2) VIRTUAL,
        CONSTRAINT fk_child_parent FOREIGN KEY (parent_id) REFERENCES parent (id)
    )""",
    "CREATE INDEX ix_child_label ON child (label DESC)",
    "CREATE TABLE files (id NUMBER PRIMARY KEY, data BLOB, owner NUMBER)",
    (
        "ALTER TABLE files ADD CONSTRAINT fk_files_child FOREIGN KEY (owner) REFERENCES child (id)"
        " DISABLE"
    ),
    "CREATE TABLE child_log (child_id NUMBER, note VARCHAR2(20))",
    """CREATE TRIGGER child_changed AFTER UPDATE ON child FOR EACH ROW
        BEGIN INSERT INTO child_log (child_id, note) VALUES (:NEW.id, 'changed'); END;""",
    """CREATE TRIGGER child_quiet BEFORE INSERT ON child FOR EACH ROW
        BEGIN :NEW.label := 'never'; END;""",
    "ALTER TRIGGER child_quiet DISABLE",
    "CREATE VIEW item_ids AS SELECT id FROM items",
    "CREATE VIEW child_ids AS SELECT id FROM child",
    "CREATE VIEW child_ids_again AS SELECT id FROM child_ids",
    "CREATE FUNCTION twice (x NUMBER) RETURN NUMBER IS BEGIN RETURN x * 2; END;",
    "CREATE SEQUENCE seq START WITH 10",
    "INSERT INTO parent (name, born) VALUES ('Ann', DATE '2000-01-01')",
    "INSERT INTO parent (name, born) VALUES ('Bo', DATE '2000-01-02')",
    "INSERT INTO parent (name, born) VALUES ('Cy', DATE '2000-01-03')",
    (
        "INSERT INTO child (id, parent_id, other_id, kind, label, zoned, took) VALUES"
        " (1, 1, 7, 'x', 'a', TIMESTAMP '2022-01-01 10:00:00 Europe/Paris',"
        " INTERVAL '1 02:03:04.5' DAY TO SECOND)"
    ),
    "INSERT INTO child (id, parent_id, other_id, kind, label) VALUES (2, 2, 3, 'y', 'b')",
    "INSERT INTO child (id, parent_id, other_id, kind, label) VALUES (3, 3, 2, 'x', 'c')",
    "INSERT INTO files (id, data) VALUES (1, HEXTORAW('00FF0A'))",
]

SPEC = """
name: scratch
source: {engine: oracle, connection: CONNECTION}
exclude: ["items"]
subset:
  seeds: {child: "id <= 2", files: "id > 0"}
  edges:
    child.parent_id -> parent: {on_missing: stub}
    child.other_id -> parent: {on_missing: null}
    files.owner -> child: {on_missing: null}
  virtual_fks:
    child.other_id -> parent: {when: "kind = 'x'"}
hooks:
  before_load: [{sql: "SELECT 1 FROM dual"}]
  after_transform: [{sql: "UPDATE child SET label = 'done-' || id"}]
anonymize:
  parent: {name: {faker: first_name}}
  child: {kind: {expression: "UPPER(kind)"}}
store:
  - filesystem: {path: STORE}
"""


@pytest.fixture
def source() -> Generator[OracleScratch]:
    with scratch() as made:
        assert isinstance(made, OracleScratch)
        for statement in SCHEMA:
            made.execute(statement)
        yield made


def registry() -> Registry:
    return Registry(
        engines={"oracle": OracleEngine},
        stores={"filesystem": filesystem_store},
        runtimes=installed().runtimes,
    )


def tooling(image: str | None = None) -> Tooling:
    ((kind, settings),) = runtime_for_tests().items()
    return Tooling(runtime=installed().runtime(kind, settings), image=image)


def working_databases(scratch_dir: Path) -> WorkingDatabases:
    return OracleEngine().working(scratch_dir, tooling(), VERSION)


def build(
    source: OracleScratch,
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
    with OracleEngine().connect(source.connection) as session:
        lock = CatalogLock.of(session.catalog())
    run_options = RunOptions(
        stages=stages, work=tmp_path / "work", resume=resume, plan=options or PlanOptions()
    )
    return Run(parsed, lock, registry(), run_options).execute()


def test_fallbacks_schema_and_odd_values(source: OracleScratch, tmp_path: Path) -> None:
    result = build(source, tmp_path)
    assert not result.failed(), result.verification
    (summary,) = result.images
    working = working_databases(tmp_path / "check")
    images: dict[ImageFormat, Path] = {}
    for archive in summary.archives:
        image, _ = unpack(tmp_path / "store" / archive.key, tmp_path / f"unpacked-{archive.format}")
        images[archive.format] = image
        with working.restore(
            image, archive.format, tmp_path / f"check-{archive.format}"
        ) as restored:
            parents = TableName.of(source.schema, "PARENT")
            children = TableName.of(source.schema, "CHILD")
            assert restored.counts([parents, children]) == {parents: 2, children: 2}
            # Children 1 and 2 are chosen; parents are not followed, so stubs stand in.
            born = ColumnRef(table=parents, column=ColumnName("BORN"))
            assert set(restored.text_values(born)) == {" 1970-01-02 00:00:00"}
            # Child 1 is a kind 'x' row whose other parent (7) does not exist: nulled.
            other = ColumnRef(table=children, column=ColumnName("OTHER_ID"))
            assert set(restored.text_values(other)) == {"3"}
            label = ColumnRef(table=children, column=ColumnName("LABEL"))
            assert set(restored.text_values(label)) == {"done-1", "done-2"}
            kinds = ColumnRef(table=children, column=ColumnName("KIND"))
            assert set(restored.text_values(kinds)) == {"X", "Y"}
            changed = ColumnRef(table=children, column=ColumnName("CHANGED"))
            assert set(restored.text_values(changed)) == {" 2001-01-01 00:00:00.123456789"}
            zoned = ColumnRef(table=children, column=ColumnName("ZONED"))
            assert set(restored.text_values(zoned)) == {
                " 2022-01-01 10:00:00.000000000 EUROPE/PARIS"
            }
            checks = {
                (
                    "SELECT 1 FROM child WHERE id = 1"
                    " AND took <> INTERVAL '1 02:03:04.5' DAY TO SECOND"
                ),
                "SELECT 1 FROM child WHERE doubled <> id * 2",
                (
                    "SELECT 1 FROM files"
                    " WHERE DBMS_LOB.COMPARE(data, TO_BLOB(HEXTORAW('00FF0A'))) <> 0"
                ),
                # Loading and anonymizing fired no trigger; each is back as it was.
                "SELECT 1 FROM child_log",
                (
                    "SELECT 1 FROM all_triggers"
                    " WHERE owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')"
                    " AND trigger_name = 'CHILD_CHANGED'"
                    " AND status <> 'ENABLED'"
                ),
                (
                    "SELECT 1 FROM all_triggers"
                    " WHERE owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')"
                    " AND trigger_name = 'CHILD_QUIET'"
                    " AND status <> 'DISABLED'"
                ),
                # Constraints, indexes, views and PL/SQL came across.
                (
                    "SELECT 1 FROM all_constraints"
                    " WHERE owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')"
                    " AND constraint_name = 'FK_FILES_CHILD'"
                    " AND status <> 'DISABLED'"
                ),
                (
                    "SELECT 1 FROM dual WHERE NOT EXISTS"
                    " (SELECT 1 FROM all_indexes"
                    " WHERE owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')"
                    " AND index_name = 'IX_CHILD_LABEL')"
                ),
                (
                    "SELECT 1 FROM dual WHERE NOT EXISTS"
                    " (SELECT 1 FROM all_views"
                    " WHERE owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')"
                    " AND view_name = 'CHILD_IDS_AGAIN')"
                ),
                "SELECT 1 FROM dual WHERE twice(2) <> 4",
                (
                    "SELECT 1 FROM all_objects"
                    " WHERE owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')"
                    " AND status <> 'VALID'"
                ),
                # The identity column goes on after the stub rows.
                (
                    "SELECT 1 FROM all_tab_identity_cols"
                    " WHERE owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')"
                    " AND table_name = 'PARENT'"
                    " AND generation_type <> 'ALWAYS'"
                ),
            }
            for check in checks:
                assert restored.failing_rows(check) == 0, check
            # The view over the excluded table is left out.
            with pytest.raises(EngineError, match="ORA-00942"):
                restored.failing_rows("SELECT 1 FROM item_ids")
            nope = ColumnRef(table=children, column=ColumnName("NOPE"))
            with pytest.raises(EngineError, match=r"scanning .*CHILD\.NOPE failed"):
                list(restored.text_values(nope))
    plan = OracleEngine().server(images[ImageFormat.PHYSICAL], None)
    assert plan.image == default_image(VERSION)


def test_unsupported_tables_are_refused(source: OracleScratch, tmp_path: Path) -> None:
    source.execute("CREATE TABLE odd (id NUMBER PRIMARY KEY, raw_row ROWID)")
    with pytest.raises(EngineError, match=r"ODD has column RAW_ROW of type ROWID"):
        build(source, tmp_path, stages=(Stage.PLAN, Stage.EXTRACT))
    spec = SPEC.replace('exclude: ["items"]', 'exclude: ["items", "odd"]')
    extracted = build(source, tmp_path / "excluded", spec=spec, stages=(Stage.PLAN, Stage.EXTRACT))
    assert not extracted.failed()


def test_failing_hooks_name_objects_not_values(source: OracleScratch, tmp_path: Path) -> None:
    broken = SPEC.replace("UPDATE child SET label = 'done-' || id", "UPDATE parent SET name = NULL")
    with pytest.raises(EngineError, match=r"an after_transform hook failed: ORA-01407"):
        build(source, tmp_path, spec=broken)
    before = SPEC.replace("SELECT 1 FROM dual", "SELEKT 1 FROM dual")
    with pytest.raises(EngineError, match=r"a before_load hook failed: ORA-00900"):
        build(source, tmp_path / "again", spec=before)


def test_bad_seeds_fail_in_both_strategies(source: OracleScratch, tmp_path: Path) -> None:
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


def test_schema_changes_after_planning_are_caught(source: OracleScratch, tmp_path: Path) -> None:
    planned = build(source, tmp_path, stages=(Stage.PLAN,))
    source.add_column("late")
    with pytest.raises(EngineError, match="schema changed between planning and the snapshot"):
        build(source, tmp_path, stages=(Stage.EXTRACT,), resume=planned.generation)


def test_corrupt_extracts_fail_to_load(source: OracleScratch, tmp_path: Path) -> None:
    extracted = build(source, tmp_path, stages=(Stage.PLAN, Stage.EXTRACT))
    data = tmp_path / "work" / "scratch" / extracted.generation / "extract" / "data"
    for file in data.glob("*.dat"):
        file.write_bytes(b"\x03\x00\x00\x00abc" * 20)
    with pytest.raises(EngineError, match=r"loading .*\.\w+ failed: ORA-"):
        build(source, tmp_path, stages=(Stage.LOAD,), resume=extracted.generation)


def test_closed_connections_and_snapshot_reads(source: OracleScratch) -> None:
    with OracleEngine().connect(source.connection) as session:
        assert isinstance(session, OracleSession)
        with session.snapshot(dt.datetime.now(tz=dt.UTC)) as snapshot:
            child = TableName.of(source.schema, "CHILD")
            assert len(list(snapshot.project(child, AllRows(), (ColumnName("ID"),)))) == 3
            with pytest.raises(EngineError, match=r"reading keys of .*CHILD failed"):
                snapshot.project(child, Predicate("nope = 1"), (ColumnName("ID"),))
        with (
            pytest.raises(EngineError, match=r"reading the snapshot failed"),
            session.snapshot(dt.datetime.now(tz=dt.UTC)),
        ):
            raise Error(1)
        session.settings["password"] += "-but-wrong"
        with (
            pytest.raises(EngineError, match="opening the snapshot failed"),
            session.snapshot(dt.datetime.now(tz=dt.UTC)),
        ):
            pass
        session.connection.close()
        with pytest.raises(EngineError, match="reading the catalog failed"):
            session.catalog()


def test_bad_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DD_BAD_URL", "oracle://nobody:wrong@oracle:1521/FREEPDB1")
    with (
        pytest.raises(EngineError, match=r"connecting to the source failed"),
        OracleEngine().connect({"url": {"env": "DD_BAD_URL"}}),
    ):
        pass


def test_working_failures(source: OracleScratch, tmp_path: Path) -> None:
    loaded = build(source, tmp_path, stages=(Stage.PLAN, Stage.EXTRACT, Stage.LOAD))
    work = tmp_path / "work" / "scratch" / loaded.generation
    plan = Plan.model_validate_json((work / "plan" / "plan.json").read_bytes())
    working = working_databases(work / "scratch")
    missing = PlannedRule(
        column=ColumnRef(table=TableName.of(source.schema, "CHILD"), column=ColumnName("NOPE")),
        kind=ColumnKind.TEXT,
        type="VARCHAR2(20 BYTE)",
        rule=HashRule(hash=HashOptions()),
    )
    transformation = Transformation((missing,), lambda _rule, _values: {}, ())
    with pytest.raises(EngineError, match="cannot copy missing"):
        working.transform(plan.groups[0], work / "missing", tmp_path / "t1", transformation)
    with pytest.raises(EngineError, match=r"reading .*CHILD\.NOPE failed"):
        working.transform(
            plan.groups[0], work / "load" / "all" / "db", tmp_path / "t2", transformation
        )
    label = missing.model_copy(
        update={
            "column": ColumnRef(
                table=TableName.of(source.schema, "CHILD"), column=ColumnName("LABEL")
            )
        }
    )
    long = Transformation((label,), lambda _rule, values: dict.fromkeys(values, "x" * 5000), ())
    with pytest.raises(EngineError, match="longer than 4000 bytes"):
        working.transform(plan.groups[0], work / "load" / "all" / "db", tmp_path / "t3", long)


def test_working_images_must_match_the_source(source: OracleScratch, tmp_path: Path) -> None:
    extracted = build(source, tmp_path, stages=(Stage.PLAN, Stage.EXTRACT))
    work = tmp_path / "work" / "scratch" / extracted.generation
    plan = Plan.model_validate_json((work / "plan" / "plan.json").read_bytes())
    index = work / "extract" / "data" / "index.json"
    index.write_text(index.read_text("utf-8").replace(VERSION, "23.26.0.0.0"), "utf-8")
    working = OracleEngine().working(tmp_path / "w", tooling(default_image(VERSION)), VERSION)
    with pytest.raises(EngineError, match=r"the source runs 23\.26\.0\.0\.0"):
        working.load(plan, plan.groups[0], work / "extract" / "data", tmp_path / "db", ())
    assert working.server_version() == VERSION


def test_instances_take_only_their_password(tmp_path: Path) -> None:
    server = OracleServer(
        Tools(tooling().runtime, default_image(VERSION)), tmp_path / "db", tmp_path
    )
    with (
        server.running(),
        pytest.raises(EngineError, match=r"connecting to the working database failed"),
    ):
        server.connect("NOPE")
    with (
        pytest.raises(EngineError, match=r"using the working database failed"),
        server.running(),
    ):
        raise Error(1)
