# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""SQL Server behaviour beyond the conformance suite: fallbacks, schema, odd values, failures."""

import datetime as dt
import json
import os
import uuid
from collections.abc import Generator
from pathlib import Path
from urllib.parse import quote as url_quote
from urllib.parse import urlsplit, urlunsplit

import pymssql
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
from datadouble_mssql import MssqlEngine
from datadouble_mssql.server import MssqlServer, Tools, default_image
from datadouble_mssql.source import MssqlSession
from test_mssql_conformance import MssqlScratch, admin, scratch

pytestmark = pytest.mark.integration

VERSION = "16.0.4325.4-CU28"

SCHEMA = [
    "CREATE SCHEMA extra",
    """CREATE TABLE parent (
        id INT IDENTITY PRIMARY KEY,
        name NVARCHAR(20) NOT NULL,
        born DATE NOT NULL,
        token UNIQUEIDENTIFIER ROWGUIDCOL NOT NULL CONSTRAINT df_token DEFAULT NEWID(),
        nick NVARCHAR(10) SPARSE NULL
    )""",
    """CREATE TABLE child (
        id INT PRIMARY KEY NONCLUSTERED,
        parent_id INT,
        other_id INT,
        kind VARCHAR(5),
        label NVARCHAR(20),
        empty NVARCHAR(10),
        raw VARBINARY(20),
        changed DATETIME2(7) NOT NULL CONSTRAINT df_changed DEFAULT '2001-01-01T00:00:00.1234567',
        doubled AS (id * 2) PERSISTED,
        version ROWVERSION,
        CONSTRAINT fk_child_parent FOREIGN KEY (parent_id) REFERENCES parent (id),
        CONSTRAINT uq_child_label UNIQUE (label)
    )""",
    "CREATE INDEX ix_child_label ON child (label DESC) INCLUDE (kind) WHERE label IS NOT NULL",
    "CREATE INDEX ix_child_kind ON child (kind)",
    "ALTER INDEX ix_child_kind ON child DISABLE",
    "ALTER TABLE child ADD CONSTRAINT ck_kind CHECK (kind IN ('x', 'y', 'X', 'Y'))",
    (
        "ALTER TABLE child WITH NOCHECK ADD CONSTRAINT ck_label CHECK NOT FOR REPLICATION"
        " (label <> 'zzz')"
    ),
    "ALTER TABLE child NOCHECK CONSTRAINT ck_label",
    "CREATE TABLE files (id INT PRIMARY KEY, data IMAGE, owner INT)",
    "CREATE UNIQUE INDEX ix_files_owner ON files (owner) WITH (IGNORE_DUP_KEY = ON)",
    (
        "ALTER TABLE files ADD CONSTRAINT fk_files_child FOREIGN KEY (owner) REFERENCES child (id)"
        " NOT FOR REPLICATION"
    ),
    "ALTER TABLE files NOCHECK CONSTRAINT fk_files_child",
    "CREATE TABLE child_log (child_id INT, note NVARCHAR(20))",
    "CREATE TABLE extra.notes (id INT PRIMARY KEY, body NVARCHAR(MAX))",
    """CREATE TRIGGER child_changed ON child AFTER UPDATE AS
        INSERT INTO child_log (child_id, note) SELECT id, N'changed' FROM inserted""",
    "CREATE VIEW item_ids AS SELECT id FROM items",
    "CREATE VIEW child_ids AS SELECT id FROM child",
    "CREATE VIEW child_ids_again AS SELECT id FROM child_ids",
    "CREATE VIEW extra.note_ids AS SELECT id FROM extra.notes",
    "CREATE VIEW extra.note_bodies AS SELECT body FROM extra.notes",
    "CREATE FUNCTION twice (@x INT) RETURNS INT AS BEGIN RETURN @x * 2 END",
    "CREATE PROCEDURE touch AS UPDATE child SET label = label",
    "CREATE SYNONYM parent_alias FOR dbo.parent",
    "CREATE SEQUENCE seq START WITH 10",
    "CREATE SEQUENCE seq_capped AS INT START WITH 1 MAXVALUE 2 CYCLE NO CACHE",
    "CREATE SEQUENCE seq_cached AS BIGINT START WITH 5 CACHE 5",
    "SELECT NEXT VALUE FOR seq",
    "SELECT NEXT VALUE FOR seq_capped",
    "SELECT NEXT VALUE FOR seq_capped",
    (
        "INSERT INTO parent (name, born) VALUES"
        " (N'Ann', '2000-01-01'), (N'Bo', '2000-01-02'), (N'Cy', '2000-01-03')"
    ),
    (
        "INSERT INTO child (id, parent_id, other_id, kind, label, empty, raw)"
        " VALUES (1, 1, 7, 'x', N'a', N'', 0x00090A5C0DFF41), (2, 2, 3, 'y', N'b', NULL, 0x),"
        " (3, 3, 2, 'x', N'c', N'', NULL)"
    ),
    "INSERT INTO files (id, data) VALUES (1, 0x00FF)",
    "INSERT INTO extra.notes VALUES (1, N'Région \u2013 東京')",
]

SPEC = """
name: scratch
source: {engine: mssql, connection: CONNECTION, server: {accept_eula: true}}
exclude: ["items"]
subset:
  seeds: {child: "id <= 2", files: "id > 0", extra.notes: "id > 0"}
  edges:
    child.parent_id -> parent: {on_missing: stub}
    child.other_id -> parent: {on_missing: null}
    files.owner -> child: {on_missing: stub}
  virtual_fks:
    child.other_id -> parent: {when: "kind = 'x'"}
hooks:
  before_load: [{sql: "SELECT 1"}]
  after_transform: [{sql: "UPDATE child SET label = CONCAT(N'done-', id)"}]
anonymize:
  parent: {name: {faker: first_name}}
  child: {kind: {expression: "UPPER(kind)"}}
  extra.notes: {body: {expression: "N'gone'", where: "id > 0"}}
store:
  - filesystem: {path: STORE}
"""


@pytest.fixture
def source() -> Generator[MssqlScratch]:
    with scratch() as made:
        assert isinstance(made, MssqlScratch)
        for statement in SCHEMA:
            made.execute(statement)
        admin(f"ALTER DATABASE [{made.database}] SET READ_COMMITTED_SNAPSHOT ON")
        yield made


def registry() -> Registry:
    return Registry(
        engines={"mssql": MssqlEngine},
        stores={"filesystem": filesystem_store},
        runtimes=installed().runtimes,
    )


def tooling(image: str | None = None) -> Tooling:
    ((kind, settings),) = runtime_for_tests().items()
    return Tooling(
        runtime=installed().runtime(kind, settings), image=image, server={"accept_eula": True}
    )


def working_databases(scratch_dir: Path) -> WorkingDatabases:
    return MssqlEngine().working(scratch_dir, tooling(), VERSION)


def build(
    source: MssqlScratch,
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
    with MssqlEngine().connect(source.connection) as session:
        lock = CatalogLock.of(session.catalog())
    run_options = RunOptions(
        stages=stages, work=tmp_path / "work", resume=resume, plan=options or PlanOptions()
    )
    return Run(parsed, lock, registry(), run_options).execute()


def dbo(name: str) -> TableName:
    return TableName.of("dbo", name)


def test_fallbacks_schema_and_odd_values(source: MssqlScratch, tmp_path: Path) -> None:
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
            parents, children = dbo("parent"), dbo("child")
            assert restored.counts([parents, children]) == {parents: 2, children: 2}
            # Children 1 and 2 are chosen; parents are not followed, so stubs stand in.
            born = ColumnRef(table=parents, column=ColumnName("born"))
            assert set(restored.text_values(born)) == {"1970-01-01"}
            # Child 1 is a kind 'x' row whose other parent (7) does not exist: nulled.
            other = ColumnRef(table=children, column=ColumnName("other_id"))
            assert set(restored.text_values(other)) == {"3"}
            label = ColumnRef(table=children, column=ColumnName("label"))
            assert set(restored.text_values(label)) == {"done-1", "done-2"}
            kinds = ColumnRef(table=children, column=ColumnName("kind"))
            assert set(restored.text_values(kinds)) == {"X", "Y"}
            changed = ColumnRef(table=children, column=ColumnName("changed"))
            assert set(restored.text_values(changed)) == {"2001-01-01T00:00:00.1234567"}
            checks = {
                # Empty strings stay empty, and NULL stays NULL.
                "SELECT 1 FROM child WHERE id = 1 AND (empty IS NULL OR empty <> N'')",
                "SELECT 1 FROM child WHERE id = 2 AND empty IS NOT NULL",
                # Bytes, empty ones too, and image data come across exactly.
                "SELECT 1 FROM child WHERE id = 1 AND raw <> 0x00090A5C0DFF41",
                "SELECT 1 FROM child WHERE id = 2 AND (raw IS NULL OR DATALENGTH(raw) <> 0)",
                "SELECT 1 FROM files WHERE CAST(data AS VARBINARY(MAX)) <> 0x00FF",
                # Computed and rowversion columns are filled in by the server.
                "SELECT 1 FROM child WHERE doubled <> id * 2 OR version IS NULL",
                # Loading and anonymizing fired no trigger, which is back on.
                "SELECT 1 FROM child_log",
                "SELECT 1 FROM sys.triggers WHERE name = 'child_changed' AND is_disabled = 1",
                "SELECT 1 WHERE OBJECT_ID('dbo.child_changed') IS NULL",
                # Indexes and constraints are as the source's.
                "SELECT 1 FROM sys.indexes WHERE name = 'ix_child_kind' AND is_disabled = 0",
                (
                    "SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM sys.indexes WHERE has_filter = 1"
                    " AND name = 'ix_child_label')"
                ),
                "SELECT 1 FROM sys.check_constraints WHERE name = 'ck_label' AND is_disabled = 0",
                "SELECT 1 FROM sys.check_constraints WHERE name = 'ck_kind' AND is_not_trusted = 1",
                "SELECT 1 FROM sys.foreign_keys WHERE name = 'fk_files_child' AND is_disabled = 0",
                # Sequences go on from where the source's were.
                "SELECT 1 FROM sys.sequences WHERE name = 'seq' AND CAST(start_value AS INT) <> 11",
                (
                    "SELECT 1 FROM sys.sequences WHERE name = 'seq_capped'"
                    " AND CAST(start_value AS INT) <> 2"
                ),
                "SELECT 1 FROM sys.sequences WHERE name = 'seq_cached' AND cache_size <> 5",
                # Identity counters are past the stub rows.
                "SELECT 1 WHERE IDENT_CURRENT('dbo.parent') < 2",
                # Modules came across, in order, in their own schemas.
                "SELECT 1 WHERE OBJECT_ID('dbo.child_ids_again') IS NULL",
                "SELECT 1 WHERE OBJECT_ID('extra.note_ids') IS NULL",
                "SELECT 1 WHERE OBJECT_ID('extra.note_bodies') IS NULL",
                (
                    "SELECT 1 FROM sys.databases WHERE database_id = DB_ID()"
                    " AND (is_read_committed_snapshot_on = 0 OR snapshot_isolation_state = 0)"
                ),
                "SELECT 1 WHERE dbo.twice(2) <> 4",
                "SELECT 1 WHERE OBJECT_ID('dbo.touch') IS NULL",
                "SELECT 1 WHERE OBJECT_ID('dbo.parent_alias') IS NULL",
                "SELECT 1 FROM extra.notes WHERE body <> N'gone'",
                "SELECT 1 FROM sys.database_principals WHERE name LIKE 'datadouble-schema-%'",
            }
            for check in checks:
                assert restored.failing_rows(check) == 0, check
            # The view over the excluded table is left out.
            with pytest.raises(EngineError, match=r"\[208\]"):
                restored.failing_rows("SELECT 1 FROM item_ids")
            with pytest.raises(EngineError, match=r"running an assertion failed: \[207\]"):
                restored.failing_rows("SELECT nope FROM child")
            nope = ColumnRef(table=children, column=ColumnName("nope"))
            with pytest.raises(EngineError, match=r"scanning .*child\.nope failed"):
                list(restored.text_values(nope))
    plan = MssqlEngine().server(images[ImageFormat.PHYSICAL], None)
    assert plan.image == default_image(VERSION)


def test_unsupported_tables_are_refused(source: MssqlScratch, tmp_path: Path) -> None:
    source.execute("CREATE TABLE odd (id INT PRIMARY KEY, v SQL_VARIANT)")
    source.execute("CREATE TABLE heaped (id INT NOT NULL, body NVARCHAR(MAX))")
    source.execute("CREATE CLUSTERED COLUMNSTORE INDEX cs_heaped ON heaped")
    with pytest.raises(EngineError, match=r"dbo\.heaped has a columnstore.*dbo\.odd has column v"):
        build(source, tmp_path, stages=(Stage.PLAN, Stage.EXTRACT))
    spec = SPEC.replace('exclude: ["items"]', 'exclude: ["items", "odd", "heaped"]')
    extracted = build(source, tmp_path / "excluded", spec=spec, stages=(Stage.PLAN, Stage.EXTRACT))
    assert not extracted.failed()


def test_snapshot_isolation_is_required(source: MssqlScratch) -> None:
    admin(f"ALTER DATABASE [{source.database}] SET ALLOW_SNAPSHOT_ISOLATION OFF")
    with (
        MssqlEngine().connect(source.connection) as session,
        pytest.raises(EngineError, match="does not allow snapshot isolation"),
        session.snapshot(dt.datetime.now(tz=dt.UTC)),
    ):
        pass


def test_a_reader_can_extract(source: MssqlScratch, tmp_path: Path) -> None:
    """A login with SELECT and VIEW DEFINITION, and no server state, is enough."""
    login = f"dd_reader_{uuid.uuid4().hex[:8]}"
    password = f"Rd-{uuid.uuid4().hex}"
    admin(f"CREATE LOGIN [{login}] WITH PASSWORD = '{password}', CHECK_POLICY = OFF")
    try:
        source.execute(f"CREATE USER [{login}] FOR LOGIN [{login}]")
        source.execute(f"GRANT SELECT, VIEW DEFINITION TO [{login}]")
        parts = urlsplit(os.environ[source.variable])
        variable = f"DD_READER_{source.database.upper()}"
        host = parts.netloc.rsplit("@", 1)[1]
        os.environ[variable] = urlunsplit(
            (parts.scheme, f"{login}:{url_quote(password)}@{host}", parts.path, "", "")
        )
        reader = MssqlScratch(source.database, variable)
        result = build(reader, tmp_path, stages=(Stage.PLAN, Stage.EXTRACT))
        assert not result.failed()
    finally:
        source.execute(f"DROP USER [{login}]")
        admin(f"DROP LOGIN [{login}]")


def test_failing_hooks_name_objects_not_values(source: MssqlScratch, tmp_path: Path) -> None:
    broken = SPEC.replace(
        "UPDATE child SET label = CONCAT(N'done-', id)", "UPDATE parent SET name = NULL"
    )
    with pytest.raises(EngineError, match=r"an after_transform hook failed: \[515\]"):
        build(source, tmp_path, spec=broken)
    before = SPEC.replace("SELECT 1", "SELEKT 1")
    with pytest.raises(EngineError, match=r"a before_load hook failed: \[2812\]"):
        build(source, tmp_path / "again", spec=before)


def test_bad_seeds_fail_in_both_strategies(source: MssqlScratch, tmp_path: Path) -> None:
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


def test_schema_changes_after_planning_are_caught(source: MssqlScratch, tmp_path: Path) -> None:
    planned = build(source, tmp_path, stages=(Stage.PLAN,))
    source.add_column("late")
    with pytest.raises(EngineError, match="schema changed between planning and the snapshot"):
        build(source, tmp_path, stages=(Stage.EXTRACT,), resume=planned.generation)


def test_excluded_parents_are_nulled(source: MssqlScratch, tmp_path: Path) -> None:
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


def test_corrupt_extracts_fail_to_load(source: MssqlScratch, tmp_path: Path) -> None:
    extracted = build(source, tmp_path, stages=(Stage.PLAN, Stage.EXTRACT))
    data = tmp_path / "work" / "scratch" / extracted.generation / "extract" / "data"
    for file in data.glob("*.dat"):
        file.write_bytes(b"\x05\x00\x00\x00short")
    with pytest.raises(EngineError, match=r"loading .*\.\w+ failed: \[\d+\]"):
        build(source, tmp_path, stages=(Stage.LOAD,), resume=extracted.generation)


def test_closed_connections_and_snapshot_reads(source: MssqlScratch) -> None:
    with MssqlEngine().connect(source.connection) as session:
        assert isinstance(session, MssqlSession)
        with session.snapshot(dt.datetime.now(tz=dt.UTC)) as snapshot:
            child = dbo("child")
            assert len(list(snapshot.project(child, AllRows(), (ColumnName("id"),)))) == 3
            with pytest.raises(EngineError, match=r"reading keys of .*child failed"):
                snapshot.project(child, Predicate("nope = 1"), (ColumnName("id"),))
        with (
            pytest.raises(EngineError, match=r"reading the snapshot failed: \[1\] boom"),
            session.snapshot(dt.datetime.now(tz=dt.UTC)),
        ):
            raise pymssql.OperationalError(1, b"boom")
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
    monkeypatch.setenv("DD_BAD_URL", "mssql://nobody:wrong@mssql:1433/nowhere")
    with (
        pytest.raises(EngineError, match=r"connecting to the source failed"),
        MssqlEngine().connect({"url": {"env": "DD_BAD_URL"}}),
    ):
        pass


def test_working_failures(source: MssqlScratch, tmp_path: Path) -> None:
    loaded = build(source, tmp_path, stages=(Stage.PLAN, Stage.EXTRACT, Stage.LOAD))
    work = tmp_path / "work" / "scratch" / loaded.generation
    plan = Plan.model_validate_json((work / "plan" / "plan.json").read_bytes())
    working = working_databases(work / "scratch")
    missing = PlannedRule(
        column=ColumnRef(table=dbo("child"), column=ColumnName("nope")),
        kind=ColumnKind.TEXT,
        type="nvarchar(20)",
        rule=HashRule(hash=HashOptions()),
    )
    transformation = Transformation((missing,), lambda _rule, _values: {}, ())
    with pytest.raises(EngineError, match="cannot copy missing"):
        working.transform(plan.groups[0], work / "missing", tmp_path / "t1", transformation)
    with pytest.raises(EngineError, match=r"reading .*child\.nope failed"):
        working.transform(
            plan.groups[0], work / "load" / "all" / "db", tmp_path / "t2", transformation
        )


def test_working_images_must_match_the_source(source: MssqlScratch, tmp_path: Path) -> None:
    extracted = build(source, tmp_path, stages=(Stage.PLAN, Stage.EXTRACT))
    work = tmp_path / "work" / "scratch" / extracted.generation
    plan = Plan.model_validate_json((work / "plan" / "plan.json").read_bytes())
    index = work / "extract" / "data" / "index.json"
    index.write_text(index.read_text("utf-8").replace(VERSION, "16.0.1000.6-RTM"), "utf-8")
    working = MssqlEngine().working(tmp_path / "w", tooling(default_image(VERSION)), VERSION)
    with pytest.raises(EngineError, match=r"the source runs 16\.0\.1000\.6-RTM"):
        working.load(plan, plan.groups[0], work / "extract" / "data", tmp_path / "db", ())
    assert working.server_version() == VERSION


def test_instances_take_only_their_password(tmp_path: Path) -> None:
    server = MssqlServer(
        Tools(tooling().runtime, default_image(VERSION)), tmp_path / "db", tmp_path
    )
    with server.running():
        with pytest.raises(EngineError, match=r"attaching x failed"):
            server.attach("x")
        with server.connect() as connection, connection.cursor() as cursor:
            cursor.execute(f"ALTER LOGIN sa WITH PASSWORD = 'Ch-{uuid.uuid4().hex}'")
        with pytest.raises(EngineError, match=r"connecting to the working database failed"):
            server.connect()
    with (
        pytest.raises(EngineError, match=r"using the working database failed: \[1\] boom"),
        server.running(),
    ):
        raise pymssql.OperationalError(1, b"boom")
