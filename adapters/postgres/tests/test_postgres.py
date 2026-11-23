# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""PostgreSQL behavior beyond the conformance suite: fallbacks, hooks and failures."""

import datetime as dt
import json
import os
import re
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from datadouble.catalog import CatalogLock, ColumnKind
from datadouble.engine import (
    Account,
    Accounts,
    EngineError,
    Tooling,
    Transformation,
    WorkingDatabases,
)
from datadouble.images import unpack
from datadouble.names import ColumnName, ColumnRef, Generation, TableName
from datadouble.planner import Plan, PlanError, PlannedRule, PlanOptions
from datadouble.registry import Registry, installed
from datadouble.run import Run, RunOptions, RunResult, Stage
from datadouble.runtime import Container, Server
from datadouble.spec import (
    AccountPrivileges,
    HashOptions,
    HashRule,
    ImageFormat,
    Spec,
    Strategy,
    parse_spec,
)
from datadouble.storage import filesystem_store
from datadouble.strict import Difference
from datadouble.subset import AllRows, Predicate
from datadouble_conformance.engine import ScratchSource, runtime_for_tests
from datadouble_postgres import PostgresEngine, list_databases
from datadouble_postgres import working as postgres_working
from datadouble_postgres.cluster import Cluster, Tools, default_image
from datadouble_postgres.render import trusted
from datadouble_postgres.source import PostgresSession
from datadouble_s3 import S3Store, s3_store
from test_conformance import DSN_VARIABLE, PostgresScratch, scratch

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
source:
  engine: postgres
  connection: CONNECTION
exclude: ["items"]
subset:
  unselected: none
  seeds:
    child: "id <= 2"
    r: "id = 1"
  edges:
    child.parent_id -> parent:
      on_missing: stub
    child.other_id -> parent:
      on_missing: null
  virtual_fks:
    child.other_id -> parent:
      when: "kind = 'x'"
hooks:
  before_load:
    - sql: "SELECT 1"
  after_transform:
    - sql: "UPDATE public.child SET label = 'done'"
anonymize:
  parent:
    name:
      faker: first_name
  child:
    kind:
      expression: "upper(kind)"
store:
  - filesystem:
      path: STORE
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
        parents = TableName.of(source.database, "public", "parent")
        children = TableName.of(source.database, "public", "child")
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
        scanning = rf"scanning {re.escape(source.database)}\.public\.child\.nope failed"
        with pytest.raises(EngineError, match=scanning):
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
        "  - filesystem:\n      path: STORE",
        f"  - s3:\n      bucket: {bucket}\n      prefix: golden\n      endpoint_url: '{endpoint}'",
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
    table = rf"{re.escape(source.database)}\.public\.\w+"
    for strategy in (Strategy.NESTED, Strategy.KEYS):
        with pytest.raises(EngineError, match=rf"(extracting|reading keys of) {table} failed"):
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
            "child.parent_id -> parent:\n      on_missing: stub",
            "child.parent_id -> parent:\n      on_missing: null",
        )
        .replace("  parent:\n    name:\n      faker: first_name\n", "")
    )
    result = build(source, tmp_path, spec=spec)
    assert not result.failed(), result.verification


def test_corrupt_extracts_fail_to_load(source: PostgresScratch, tmp_path: Path) -> None:
    spec = SPEC.replace('exclude: ["items"]\n', "")
    extracted = build(source, tmp_path, spec=spec, stages=(Stage.PLAN, Stage.EXTRACT))
    data = tmp_path / "work" / "scratch" / extracted.generation / "extract" / "data"
    for file in data.glob("*.copy"):
        file.write_text("not\ta\trow\tat\tall\n", encoding="utf-8")
    table = rf"{re.escape(source.database)}\.public\.\w+"
    with pytest.raises(EngineError, match=rf"loading {table} failed: \[22P02\]"):
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
        child = TableName.of(source.database, "public", "child")
        assert len(list(snapshot.project(child, AllRows(), (ColumnName("id"),)))) == 3
        failed = rf"reading keys of {re.escape(source.database)}\.public\.child failed"
        with pytest.raises(EngineError, match=failed):
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
        column=ColumnRef(
            table=TableName.of(source.database, "public", "child"), column=ColumnName("nope")
        ),
        kind=ColumnKind.TEXT,
        type="text",
        rule=HashRule(hash=HashOptions()),
    )
    transformation = Transformation((missing,), lambda _rule, _values: {}, ())
    with pytest.raises(EngineError, match="cannot copy missing"):
        working.transform(plan.groups[0], work / "missing", tmp_path / "t1", transformation)
    with pytest.raises(
        EngineError, match=rf"reading {re.escape(source.database)}\.public\.child\.nope failed"
    ):
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


@contextmanager
def served_like_a_consumer(tools: Tools, image: Path) -> Generator[Server]:
    """A server on the image's data directory, started by the official image's own entrypoint."""
    container = Container(
        image=tools.image,
        command=("docker-entrypoint.sh", "postgres"),
        owner=image,
        mounts=(image,),
        env={"PGDATA": str(image / "pgdata")},
        port=5432,
        ready=("pg_isready", "--host=127.0.0.1"),
    )
    with tools.runtime.serve(container) as server:
        yield server


def test_images_get_accounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    working = working_databases(tmp_path / "scratch")
    assert isinstance(working, Accounts)
    tools = Tools(tooling().runtime, default_image("18"), tmp_path / "home")
    image = tmp_path / "image"
    Cluster(tools, image / "pgdata", image).initialize()
    first, second, reading = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
    working.add_accounts(image, [Account("copyadmin", first), Account("reports", reading)])
    cluster = Cluster(tools, image / "pgdata", image)
    with cluster.running():
        cluster.create_database("shop")
        with cluster.connect("shop") as connection:
            connection.execute("CREATE SCHEMA store")
            connection.execute("CREATE TABLE store.items (id serial PRIMARY KEY)")
            connection.execute("INSERT INTO store.items DEFAULT VALUES")
            connection.execute("CREATE VIEW store.item_ids AS SELECT id FROM store.items")
    # Giving them again resets the password, leaves exactly the declared privileges,
    # and the image's pg_hba.conf keeps one password rule.
    read = Account("reports", reading, AccountPrivileges.READ)
    working.add_accounts(image, [Account("copyadmin", second), read])
    hba = (image / "pgdata" / "pg_hba.conf").read_text("utf-8")
    assert hba.splitlines().count("host all all all scram-sha-256") == 1
    with served_like_a_consumer(tools, image) as server:
        endpoint = server.endpoint

        def login(
            user: str, password: str, dbname: str = "postgres"
        ) -> psycopg.Connection[tuple[object, ...]]:
            conninfo = make_conninfo(
                host=endpoint.host, port=str(endpoint.port), dbname=dbname, user=user
            )
            return psycopg.connect(conninfo, password=password, autocommit=True)

        with pytest.raises(psycopg.OperationalError):
            login("copyadmin", first)
        with login("copyadmin", second) as connection:
            row = connection.execute(
                "SELECT rolname, rolcanlogin, rolsuper, left(rolpassword, 14)"
                " FROM pg_authid WHERE rolname IN ('copyadmin', 'reports') ORDER BY rolname"
            ).fetchall()
            assert row == [
                ("copyadmin", True, True, "SCRAM-SHA-256$"),
                ("reports", True, False, "SCRAM-SHA-256$"),
            ]
        with login("copyadmin", second, "shop") as connection:
            connection.execute("INSERT INTO store.items DEFAULT VALUES")
        with login("reports", reading, "shop") as connection:
            assert connection.execute("SELECT id FROM store.item_ids").fetchall() == [(1,), (2,)]
            assert connection.execute("SELECT last_value FROM store.items_id_seq").fetchone()
            for statement in (
                "INSERT INTO store.items DEFAULT VALUES",
                "CREATE TABLE store.more (id int)",
                "CREATE TABLE more (id int)",
                "CREATE ROLE other",
                # Unlike pg_read_all_data, no password hashes.
                "SELECT rolpassword FROM pg_authid",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    connection.execute(statement)
    with pytest.raises(EngineError, match="giving the image the account 'pg_reserved' failed"):
        working.add_accounts(image, [Account("pg_reserved", first)])
    monkeypatch.setattr(postgres_working, "_SCRAM", "SCRAM-SHA-1$")
    with pytest.raises(EngineError, match="checking the image's account 'reports' failed"):
        working.add_accounts(image, [read])
    monkeypatch.undo()
    nowhere = "host=127.0.0.1 port=1 dbname=postgres user=copyadmin connect_timeout=2"

    def unreachable(**_: str) -> str:
        return nowhere

    monkeypatch.setattr(postgres_working, "make_conninfo", unreachable)
    with pytest.raises(EngineError, match="logging in with the image's account 'copyadmin'"):
        working.add_accounts(image, [Account("copyadmin", first)])


# Three databases on one server, as an application spread across them keeps
# them: customers and their addresses in the first, another set of customers
# and their invoices in the second, orders in the third. Both customer tables
# are public.customers, so only their database tells them apart.
DATABASES_SCHEMA = {
    "a": """
CREATE TABLE public.customers (id integer PRIMARY KEY, email text NOT NULL);
CREATE TABLE public.addresses (
    id integer PRIMARY KEY, customer_id integer NOT NULL REFERENCES public.customers, line text
);
INSERT INTO public.customers VALUES (1, 'ann@shop.test'), (2, 'bo@shop.test'), (3, 'cy@x.test');
INSERT INTO public.addresses VALUES (10, 1, 'one'), (11, 2, 'two'), (12, 3, 'three');
CREATE VIEW public.emails AS SELECT email FROM public.customers;
""",
    "b": """
CREATE SCHEMA billing;
CREATE TABLE public.customers (id integer PRIMARY KEY, email text NOT NULL);
CREATE TABLE billing.invoices (
    id integer PRIMARY KEY, customer_id integer NOT NULL REFERENCES public.customers, amount int,
    referred_by integer REFERENCES public.customers
);
INSERT INTO public.customers VALUES (1, 'dee@pay.test'), (2, 'ed@pay.test');
INSERT INTO billing.invoices VALUES (20, 1, 5, 2), (21, 1, 6, NULL), (22, 2, 7, 1);
CREATE VIEW public.emails AS SELECT email FROM public.customers;
""",
    "c": """
CREATE TABLE public.orders (id integer PRIMARY KEY, note text);
INSERT INTO public.orders VALUES (30, 'x'), (31, 'y');
""",
}

DATABASES_SPEC = """
name: scratch
source:
  engine: postgres
  connection:
    dsn:
      env: VARIABLE
    databases: ["PREFIX_*"]
subset:
  unselected: none
  seeds:
    PREFIX_a.public.customers: "id <= 2"
    PREFIX_b.public.customers: "id = 1"
    orders: "true"
  follow:
    down:
      - PREFIX_a.public.customers -> addresses
      - PREFIX_b.public.customers -> invoices.customer_id
  edges:
    invoices.referred_by -> PREFIX_b.public.customers:
      on_missing: null
hooks:
  after_transform:
    - sql: "UPDATE public.addresses SET line = 'moved'"
anonymize:
  PREFIX_a.public.customers:
    email:
      faker: email
  PREFIX_b.public.customers:
    email:
      faker: email
verify:
  assertions:
    - name: addresses moved
      sql: "SELECT 1 FROM public.addresses WHERE line <> 'moved'"
store:
  - filesystem:
      path: STORE
"""


@pytest.fixture
def databases() -> Generator[tuple[str, str]]:
    """Three scratch databases, ``<prefix>_a`` to ``_c``; the prefix and a DSN variable.

    The DSN names the server's own database, which is not read, so the first
    database read is the default.
    """
    admin = os.environ[DSN_VARIABLE]
    prefix = f"dd_many_{uuid.uuid4().hex[:8]}"
    variable = f"DD_{prefix.upper()}"
    os.environ[variable] = admin
    try:
        for suffix, schema in DATABASES_SCHEMA.items():
            name = f"{prefix}_{suffix}"
            with psycopg.connect(admin, autocommit=True) as connection:
                connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            with psycopg.connect(make_conninfo(admin, dbname=name), autocommit=True) as connection:
                connection.execute(trusted(schema))
        yield prefix, variable
    finally:
        os.environ.pop(variable, None)
        with psycopg.connect(admin, autocommit=True) as connection:
            for suffix in "abcd":
                dropped = sql.Identifier(f"{prefix}_{suffix}")
                connection.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(dropped)
                )


def databases_spec(prefix: str, variable: str, tmp_path: Path, spec: str = DATABASES_SPEC) -> Spec:
    return parse_spec(
        spec.replace("PREFIX", prefix)
        .replace("VARIABLE", variable)
        .replace("STORE", str(tmp_path / "store"))
        + f"\nruntime: {json.dumps(runtime_for_tests())}\n"
    )


def test_several_databases_build_one_image(databases: tuple[str, str], tmp_path: Path) -> None:
    prefix, variable = databases
    a, b, c = (f"{prefix}_{suffix}" for suffix in "abc")
    spec = databases_spec(prefix, variable, tmp_path)
    with PostgresEngine().connect(spec.source.connection) as session:
        assert isinstance(session, PostgresSession)
        assert (tuple(session.connections), session.default) == ((a, b, c), a)
        lock = CatalogLock.of(session.catalog())
    # Each database's objects, named with it, depend only on its own tables.
    assert {
        str(item.ref): {str(edge) for edge in item.depends_on} for item in lock.catalog.objects
    } == {
        f"view {a}.public.emails": {f"table {a}.public.customers"},
        f"view {b}.public.emails": {f"table {b}.public.customers"},
    }
    options = RunOptions(stages=tuple(Stage), work=tmp_path / "work")
    result = Run(spec, lock, registry(), options).execute()
    assert not result.failed(), result.verification
    manifest = tmp_path / "work" / "scratch" / result.generation / "extract" / "manifest.json"
    extracted = json.loads(manifest.read_text())["outputs"]["result"]
    assert extracted["snapshot"] == ""
    assert set(extracted["snapshots"]) == {a, b, c}
    tables = {
        name: TableName.of(*name.split("."))
        for name in (
            f"{a}.public.customers",
            f"{a}.public.addresses",
            f"{b}.public.customers",
            f"{b}.billing.invoices",
            f"{c}.public.orders",
        )
    }
    (summary,) = result.images
    working = working_databases(tmp_path / "check")
    for archive in summary.archives:
        image, _ = unpack(tmp_path / "store" / archive.key, tmp_path / f"unpacked-{archive.format}")
        with working.restore(image, archive.format, tmp_path / f"r-{archive.format}") as restored:
            counts = restored.counts(tables.values())
            assert {str(table): rows for table, rows in counts.items()} == {
                f"{a}.public.customers": 2,
                f"{a}.public.addresses": 2,
                f"{b}.public.customers": 1,
                f"{b}.billing.invoices": 2,
                f"{c}.public.orders": 2,
            }
            # Spec SQL that names no table runs in the default database.
            assert restored.failing_rows("SELECT 1 FROM public.addresses") == 2
            # Invoice 20 was referred by customer 2, who is not copied: nulled.
            referred = ColumnRef(
                table=tables[f"{b}.billing.invoices"], column=ColumnName("referred_by")
            )
            assert set(restored.text_values(referred)) == set()
            emails = ColumnRef(table=tables[f"{b}.public.customers"], column=ColumnName("email"))
            assert "dee@pay.test" not in set(restored.text_values(emails))


def test_names_across_databases(databases: tuple[str, str], tmp_path: Path) -> None:
    prefix, variable = databases
    a, c = f"{prefix}_a", f"{prefix}_c"
    plan_only = RunOptions(stages=(Stage.PLAN,), work=tmp_path / "work")
    spec = databases_spec(prefix, variable, tmp_path)
    with PostgresEngine().connect(spec.source.connection) as session:
        lock = CatalogLock.of(session.catalog())
    # A name two databases share fails, naming both; a virtual reference
    # between databases cannot be read in one query.
    broken = databases_spec(
        prefix,
        variable,
        tmp_path,
        DATABASES_SPEC.replace(
            '    orders: "true"\n', '    orders: "true"\n    customers: "true"\n'
        ).replace(
            "  follow:\n",
            "  virtual_fks: {orders.id -> PREFIX_a.public.customers: {}}\n  follow:\n",
        ),
    )
    with pytest.raises(PlanError) as caught:
        Run(broken, lock, registry(), plan_only).execute()
    problems = caught.value.problems
    assert (
        f"table 'customers' is ambiguous: it matches {a}.public.customers,"
        f" {prefix}_b.public.customers; qualify it"
    ) in problems
    assert (
        f"virtual reference 'orders.id -> {a}.public.customers' joins {c} and {a},"
        " which one postgres query cannot read together"
    ) in problems
    # A new database the pattern matches is new tables to strict mode.
    admin = os.environ[DSN_VARIABLE]
    with psycopg.connect(admin, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(f"{prefix}_d")))
    with psycopg.connect(make_conninfo(admin, dbname=f"{prefix}_d"), autocommit=True) as connection:
        connection.execute("CREATE TABLE public.extra (id integer PRIMARY KEY)")
    with pytest.raises(PlanError) as caught:
        Run(spec, lock, registry(), plan_only).execute()
    assert caught.value.strict is not None
    assert {(f.difference, f.object) for f in caught.value.strict.findings} == {
        (Difference.NEW_TABLE, f"{prefix}_d.public.extra")
    }


def test_listing_databases_fails_on_a_closed_connection(source: PostgresScratch) -> None:
    with PostgresEngine().connect(source.connection) as session:
        assert isinstance(session, PostgresSession)
        session.connection.close()
        with pytest.raises(EngineError, match="listing the databases failed"):
            list_databases(session.connection)


# A slice of one schema: billing's invoices refer to shop's customers, a
# trigger on them calls a function in audit, and applications call a public
# function. A standalone billing image pulls all of that in, and nothing else.
SLICE_SCHEMA = """
CREATE SCHEMA shop;
CREATE SCHEMA billing;
CREATE SCHEMA audit;
CREATE TABLE shop.customers (id integer PRIMARY KEY, email text NOT NULL);
CREATE VIEW shop.emails AS SELECT email FROM shop.customers;
CREATE TABLE billing.invoices (
    id integer PRIMARY KEY, customer_id integer NOT NULL REFERENCES shop.customers, amount int
);
CREATE TABLE audit.entries (id serial PRIMARY KEY, invoice_id integer);
CREATE FUNCTION audit.record() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO audit.entries (invoice_id) VALUES (NEW.id);
    RETURN NEW;
END $$;
CREATE TRIGGER invoices_audit AFTER INSERT ON billing.invoices
    FOR EACH ROW EXECUTE FUNCTION audit.record();
CREATE FUNCTION public.greet(name text) RETURNS text LANGUAGE sql IMMUTABLE RETURN 'hello ' || name;
CREATE FUNCTION public.unused() RETURNS integer LANGUAGE sql IMMUTABLE RETURN 1;
INSERT INTO shop.customers VALUES (1, 'ann@shop.test'), (2, 'bo@shop.test'), (3, 'cy@x.test');
INSERT INTO billing.invoices VALUES (10, 1, 5), (11, 2, 6);
"""

SLICE_SPEC = """
name: scratch
source:
  engine: postgres
  connection: CONNECTION
exclude: [items]
objects:
  - function: public.greet
subset:
  unselected: none
  seeds:
    invoices: "true"
    customers: "id = 3"
images:
  billing:
    include: ["billing.*"]
    standalone: true ROWS
  rest:
    include: ["shop.*", "audit.*"]
anonymize:
  customers:
    email:
      faker: email
store:
  - filesystem:
      path: STORE
"""

SLICE_CHECKS = {
    "SELECT 1 FROM pg_catalog.pg_proc"
    " WHERE pronamespace::regnamespace::text IN ('public', 'audit')": 2,
    "SELECT 1 FROM pg_catalog.pg_proc WHERE proname = 'unused'": 0,
    "SELECT 1 FROM pg_catalog.pg_trigger WHERE tgname = 'invoices_audit'": 1,
    "SELECT 1 FROM pg_catalog.pg_views WHERE viewname = 'emails'": 1,
    "SELECT 1 WHERE public.greet('x') <> 'hello x'": 0,
    "SELECT 1 FROM pg_catalog.pg_constraint WHERE contype = 'f'": 1,
}


@pytest.mark.parametrize(
    ("rows", "customers"), [("", 2), (", rows: {customers: all}", 3)], ids=["referenced", "all"]
)
def test_a_standalone_slice_holds_its_closure(tmp_path: Path, rows: str, customers: int) -> None:
    with scratch() as made:
        made.execute(trusted(SLICE_SCHEMA))
        spec = SLICE_SPEC.replace("ROWS", rows)
        planned = build(made, tmp_path, spec=spec, stages=(Stage.PLAN,))
        assert any("audit.entries is reached only through objects" in w for w in planned.warnings)
        result = build(made, tmp_path, spec=spec, resume=planned.generation)
        assert not result.failed(), result.verification
        billing = next(s for s in result.images if s.identity.group == "billing")
        tables = {
            name: TableName.of(made.database, *name.split("."))
            for name in ("billing.invoices", "shop.customers", "audit.entries")
        }
        working = working_databases(tmp_path / "check")
        for archive in billing.archives:
            image, _ = unpack(tmp_path / "store" / archive.key, tmp_path / f"u-{archive.format}")
            with working.restore(image, archive.format, tmp_path / f"r-{archive.format}") as found:
                counts = found.counts(tables.values())
                assert {str(t).split(".", 1)[1]: n for t, n in counts.items()} == {
                    "billing.invoices": 2,
                    "shop.customers": customers,
                    "audit.entries": 0,
                }
                for query, expected in SLICE_CHECKS.items():
                    assert found.failing_rows(query) == expected, query
                # Only the closure's tables are there.
                assert (
                    found.failing_rows(
                        "SELECT 1 FROM pg_catalog.pg_tables"
                        " WHERE schemaname NOT IN ('pg_catalog', 'information_schema')"
                    )
                    == 3
                )
