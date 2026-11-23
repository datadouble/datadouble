# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""MySQL behaviour beyond the conformance suite: fallbacks, hooks, odd columns, failures."""

import datetime as dt
import json
import os
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

import pymysql
import pytest
from pymysql.connections import Connection

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
from datadouble.spec import AccountPrivileges, HashOptions, HashRule, Strategy, parse_spec
from datadouble.storage import filesystem_store
from datadouble.strict import Difference
from datadouble.subset import AllRows, Predicate
from datadouble_conformance.engine import runtime_for_tests
from datadouble_mysql import MysqlEngine, list_databases
from datadouble_mysql import working as mysql_working
from datadouble_mysql.render import quote
from datadouble_mysql.server import MysqlServer, Tools, default_image
from datadouble_mysql.source import MysqlSession
from test_mysql_conformance import URL_VARIABLE, MysqlScratch, admin, scratch

if TYPE_CHECKING:
    from pydantic import JsonValue

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
sources:
  app:
    engine: mysql
    connection: CONNECTION
exclude: ["items"]
subset:
  unselected: none
  seeds:
    child: "id <= 2"
    files: "id > 0"
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
    - sql: "UPDATE child SET label = 'done', changed = changed"
anonymize:
  parent:
    name:
      faker: first_name
  child:
    kind:
      expression: "UPPER(kind)"
store:
  - filesystem:
      path: STORE
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
            "child.parent_id -> parent:\n      on_missing: stub",
            "child.parent_id -> parent:\n      on_missing: null",
        )
        .replace("  parent:\n    name:\n      faker: first_name\n", "")
    )
    result = build(source, tmp_path, spec=spec)
    assert not result.failed(), result.verification


def test_corrupt_extracts_fail_to_load(source: MysqlScratch, tmp_path: Path) -> None:
    extracted = build(source, tmp_path, stages=(Stage.PLAN, Stage.EXTRACT))
    data = tmp_path / "work" / "scratch" / extracted.generation / "extract" / "data" / "app"
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


PLUGIN_SPEC = """
name: scratch
sources:
  app:
    engine: mysql
    connection: CONNECTION
exclude: ["items"]
subset:
  unselected: none
  seeds:
    notes: "id > 0"
"""


def test_plugins_the_data_needs_are_installed(source: MysqlScratch, tmp_path: Path) -> None:
    # The example full-text parser that ships in the official image.
    admin("INSTALL PLUGIN simple_parser SONAME 'mypluglib.so'")
    try:
        source.execute(
            "CREATE TABLE notes"
            " (id INT PRIMARY KEY, body TEXT, FULLTEXT (body) WITH PARSER simple_parser)"
        )
        source.execute("INSERT INTO notes VALUES (1, 'hello world')")
        with MysqlEngine().connect(source.connection) as session:
            catalog = session.catalog()
        assert "plugin mypluglib.so" in {str(r) for r in catalog.requirements}
        result = build(
            source, tmp_path, spec=PLUGIN_SPEC, stages=(Stage.PLAN, Stage.EXTRACT, Stage.LOAD)
        )
        assert not result.failed()
        assert tuple(
            r.name
            for r in working_databases(tmp_path / "scratch").available() or ()
            if r.name == "mypluglib.so"
        ) == ("mypluglib.so",)
        # Omitted, the plugin is not installed, and the table using it must go.
        omitted = (
            PLUGIN_SPEC.replace(
                "    connection: CONNECTION\n",
                "    connection: CONNECTION\n    omit: [mypluglib.so]\n",
            )
            .replace('["items"]', '["items", "notes"]')
            .replace('notes: "id > 0"', 'files: "id > 0"')
        )
        result = build(
            source,
            tmp_path / "omitted",
            spec=omitted,
            stages=(Stage.PLAN, Stage.EXTRACT, Stage.LOAD),
        )
        assert not result.failed()
    finally:
        source.execute("DROP TABLE IF EXISTS notes")
        admin("UNINSTALL PLUGIN simple_parser")


@contextmanager
def served_like_a_consumer(tools: Tools, image: Path) -> Generator[Server]:
    """A server on the image's data directory, started by the official image's own entrypoint."""
    container = Container(
        image=tools.image,
        command=("docker-entrypoint.sh", "mysqld", f"--datadir={image / 'mysql'}"),
        owner=image,
        mounts=(image,),
        port=3306,
        # ping succeeds once the server answers, even when it refuses the login.
        ready=("mysqladmin", "--protocol=tcp", "--host=127.0.0.1", "--user=nobody", "ping"),
    )
    with tools.runtime.serve(container) as server:
        yield server


def test_images_get_accounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    working = working_databases(tmp_path / "scratch")
    assert isinstance(working, Accounts)
    tools = Tools(tooling().runtime, default_image("8.4.13"))
    image = tmp_path / "image"
    MysqlServer(tools, image / "mysql", image).initialize()
    first, second, reading, rooted = (uuid.uuid4().hex for _ in range(4))
    working.add_accounts(image, [Account("app", first), Account("reports", reading)])
    building = MysqlServer(tools, image / "mysql", image)
    with building.running(), building.connect() as connection, connection.cursor() as cursor:
        cursor.execute("CREATE DATABASE shop")
        cursor.execute("CREATE TABLE shop.items (id INT PRIMARY KEY)")
        cursor.execute("INSERT INTO shop.items VALUES (1)")
        cursor.execute(
            "CREATE SQL SECURITY INVOKER VIEW shop.item_ids AS SELECT id FROM shop.items"
        )
    # Giving them again resets the password and leaves exactly the declared privileges.
    working.add_accounts(
        image,
        [
            Account("app", second),
            Account("reports", reading, AccountPrivileges.READ),
            Account("root", rooted),
        ],
    )
    with served_like_a_consumer(tools, image) as server:
        endpoint = server.endpoint

        def login(user: str, password: str) -> Connection:
            return pymysql.connect(
                host=endpoint.host,
                port=endpoint.port,
                user=user,
                password=password,
                autocommit=True,
            )

        with pytest.raises(pymysql.MySQLError):
            login("app", first)
        with login("app", second) as connection, connection.cursor() as cursor:
            cursor.execute("SHOW GRANTS")
            assert any("WITH GRANT OPTION" in str(row) for row in cursor.fetchall())
            cursor.execute("INSERT INTO shop.items VALUES (2)")
        with login("reports", reading) as connection, connection.cursor() as cursor:
            cursor.execute("SHOW GRANTS")
            assert set(cursor.fetchall()) == {
                ("GRANT USAGE ON *.* TO `reports`@`%`",),
                ("GRANT SELECT, SHOW VIEW ON `shop`.* TO `reports`@`%`",),
            }
            cursor.execute("SELECT id FROM shop.item_ids")
            assert cursor.fetchall() == ((1,), (2,))
            cursor.execute("SHOW CREATE VIEW shop.item_ids")
            for statement in (
                "INSERT INTO shop.items VALUES (3)",
                "CREATE TABLE shop.more (id INT)",
                "CREATE USER 'other'@'%'",
                # Not *.*, so no password hashes.
                "SELECT authentication_string FROM mysql.user",
            ):
                with pytest.raises(pymysql.MySQLError):
                    cursor.execute(statement)
        with login("root", rooted) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT CURRENT_USER()")
            assert cursor.fetchone() == ("root@%",)
        # root@localhost still logs in over the socket without a password.
        socket = server.execute(["mysql", "-uroot", "--batch", "-e", "SELECT CURRENT_USER()"])
        assert socket.status == 0, socket.stderr
        assert "root@localhost" in socket.stdout
        with login("app", second) as connection, connection.cursor() as cursor:
            # A locked account stays locked when its password is reset.
            cursor.execute("ALTER USER 'app'@'%' ACCOUNT LOCK")
    with pytest.raises(EngineError, match="logging in with the image's account 'app' failed"):
        working.add_accounts(image, [Account("app", first)])
    with pytest.raises(EngineError, match=r"giving the image the account 'u+' failed"):
        working.add_accounts(image, [Account("u" * 40, first)])

    def the_password(_: int) -> str:
        return second

    # A server that took any password, as it would here, fails the check.
    monkeypatch.setattr(mysql_working, "secrets", SimpleNamespace(token_hex=the_password))
    with pytest.raises(EngineError, match="the server accepted a wrong password"):
        working.add_accounts(image, [Account("reports", second, AccountPrivileges.READ)])


# Three databases on one server: customers in the first, orders referring to
# them from the second, and notes on the orders, with a view joining all three,
# in the third. The second has a trigger, which transform sets aside.
DATABASES_SPEC = """
name: scratch
sources:
  app:
    engine: mysql
    connection:
      url:
        env: VARIABLE
      databases: ["PREFIX_*"]
subset:
  unselected: none
  seeds:
    customers: "id <= 2"
  follow:
    down: [customers -> orders, orders -> notes]
hooks:
  # Fails without a default database, so it shows before_load hooks get one.
  before_load:
    - sql: "CREATE TEMPORARY TABLE hook_check (id INT)"
  after_transform:
    - sql: "UPDATE PREFIX_b.orders SET status = 'done'"
anonymize:
  customers:
    email:
      faker: email
verify:
  assertions:
    - name: every order has an email
      sql: "SELECT 1 FROM PREFIX_c.order_emails WHERE email IS NULL"
store:
  - filesystem:
      path: STORE
"""


DATABASES_SCHEMA = [
    "CREATE DATABASE PREFIX_a",
    "CREATE DATABASE PREFIX_b",
    "CREATE DATABASE PREFIX_c",
    "CREATE TABLE PREFIX_a.customers (id INT PRIMARY KEY, email VARCHAR(80) NOT NULL)",
    """CREATE TABLE PREFIX_b.orders (
        id INT PRIMARY KEY, customer_id INT NOT NULL, status VARCHAR(10),
        CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES PREFIX_a.customers (id)
    )""",
    "CREATE TABLE PREFIX_b.order_log (order_id INT)",
    """CREATE TRIGGER PREFIX_b.orders_changed AFTER UPDATE ON PREFIX_b.orders FOR EACH ROW
        INSERT INTO PREFIX_b.order_log (order_id) VALUES (NEW.id)""",
    """CREATE TABLE PREFIX_c.notes (
        id INT PRIMARY KEY, order_id INT NOT NULL, body TEXT,
        CONSTRAINT fk_notes_order FOREIGN KEY (order_id) REFERENCES PREFIX_b.orders (id)
    )""",
    """CREATE VIEW PREFIX_c.order_emails AS SELECT o.id, cu.email
        FROM PREFIX_b.orders o JOIN PREFIX_a.customers cu ON cu.id = o.customer_id""",
    # A database the spec does not read, and a view over it, which copies leave out.
    "CREATE DATABASE PREFIXx",
    "CREATE TABLE PREFIXx.archive (id INT PRIMARY KEY)",
    "CREATE VIEW PREFIX_a.archived AS SELECT id FROM PREFIXx.archive",
    "INSERT INTO PREFIX_a.customers VALUES (1, 'ann@shop.test'), (2, 'bo@shop.test'), (3, 'cy@x')",
    "INSERT INTO PREFIX_b.orders VALUES (10, 1, 'new'), (11, 2, 'new'), (12, 3, 'new')",
    "INSERT INTO PREFIX_c.notes VALUES (100, 10, 'x'), (101, 12, 'y')",
]

# Checks on a restored image: the references and the view across databases
# survive, and the hook's update left the trigger's log empty and the trigger
# in place. Spec SQL without a database runs in the default one, the URL's.
DATABASES_CHECKS = {
    "SELECT 1 FROM information_schema.REFERENTIAL_CONSTRAINTS"
    " WHERE CONSTRAINT_SCHEMA LIKE 'PREFIX%'": 2,
    "SELECT 1 FROM PREFIX_c.order_emails": 2,
    "SELECT 1 FROM PREFIX_b.order_log": 0,
    "SELECT 1 FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = 'PREFIX_b'": 1,
    "SELECT 1 FROM customers": 2,
    "SELECT 1 FROM information_schema.TABLES WHERE TABLE_NAME = 'archived'"
    " AND TABLE_SCHEMA = 'PREFIX_a'": 0,
}


@pytest.fixture
def databases() -> Generator[tuple[str, str]]:
    """Three scratch databases, ``<prefix>_a`` to ``_c``; the prefix and a URL variable."""
    prefix = f"dd_many_{uuid.uuid4().hex[:8]}"
    variable = f"DD_{prefix.upper()}"
    parts = urlsplit(os.environ[URL_VARIABLE])
    os.environ[variable] = urlunsplit((parts.scheme, parts.netloc, f"/{prefix}_a", "", ""))
    try:
        for statement in DATABASES_SCHEMA:
            admin(statement.replace("PREFIX", prefix))
        yield prefix, variable
    finally:
        os.environ.pop(variable, None)
        # Foreign keys run between the databases, so whichever goes first
        # would be referenced by one still there.
        admin(
            "SET foreign_key_checks = 0",
            *(
                f"DROP DATABASE IF EXISTS {quote(f'{prefix}{suffix}')}"
                for suffix in ("_a", "_b", "_c", "_d", "x")
            ),
        )


def test_several_databases_build_as_one(databases: tuple[str, str], tmp_path: Path) -> None:
    prefix, variable = databases
    text = (
        DATABASES_SPEC.replace("PREFIX", prefix)
        .replace("VARIABLE", variable)
        .replace("STORE", str(tmp_path / "store"))
        + f"\nruntime: {json.dumps(runtime_for_tests())}\n"
    )
    spec = parse_spec(text)
    with MysqlEngine().connect(spec.working.connection) as session:
        assert isinstance(session, MysqlSession)
        assert session.databases == (f"{prefix}_a", f"{prefix}_b", f"{prefix}_c")
        assert session.default == f"{prefix}_a"
        lock = CatalogLock.of(session.catalog())
    # Without a database in the URL, the first database read is the default.
    parts = urlsplit(os.environ[variable])
    os.environ[f"{variable}_BARE"] = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    try:
        bare: dict[str, JsonValue] = {
            "url": {"env": f"{variable}_BARE"},
            "databases": [f"{prefix}_c", f"{prefix}_b"],
        }
        with MysqlEngine().connect(bare) as session:
            assert isinstance(session, MysqlSession)
            assert (session.databases, session.default) == (
                (f"{prefix}_b", f"{prefix}_c"),
                f"{prefix}_b",
            )
    finally:
        os.environ.pop(f"{variable}_BARE", None)
    # Objects are read in every database, with edges across them; a view over a
    # database not read is left out, as copies leave it out.
    edges = {
        str(item.ref): {str(edge) for edge in item.depends_on} for item in lock.catalog.objects
    }
    assert edges == {
        f"view {prefix}_c.order_emails": {
            f"table {prefix}_b.orders",
            f"table {prefix}_a.customers",
        },
        f"trigger {prefix}_b.orders_changed": {
            f"table {prefix}_b.orders",
            f"table {prefix}_b.order_log (inferred)",
        },
    }
    orders = TableName.of(f"{prefix}_b", "orders")
    (fk,) = next(t for t in lock.catalog.tables if t.name == orders).foreign_keys
    assert fk.target == TableName.of(f"{prefix}_a", "customers")
    options = RunOptions(stages=tuple(Stage), work=tmp_path / "work")
    result = Run(spec, lock, registry(), options).execute()
    assert not result.failed(), result.verification
    extracted = json.loads(
        (
            tmp_path / "work" / "scratch" / result.generation / "extract" / "manifest.json"
        ).read_text()
    )
    (read,) = extracted["outputs"]["sources"]
    assert read["result"]["snapshots"] == {}
    (summary,) = result.images
    working = working_databases(tmp_path / "check")
    customers, notes = (
        TableName.of(f"{prefix}_a", "customers"),
        TableName.of(f"{prefix}_c", "notes"),
    )
    for archive in summary.archives:
        image, _ = unpack(tmp_path / "store" / archive.key, tmp_path / f"unpacked-{archive.format}")
        with working.restore(image, archive.format, tmp_path / f"r-{archive.format}") as restored:
            assert restored.counts([customers, orders, notes]) == {
                customers: 2,
                orders: 2,
                notes: 1,
            }
            for query, rows in DATABASES_CHECKS.items():
                assert restored.failing_rows(query.replace("PREFIX", prefix)) == rows, query
    # A new database the pattern matches is new tables to strict mode.
    admin(f"CREATE DATABASE {quote(f'{prefix}_d')}")
    admin(f"CREATE TABLE {quote(f'{prefix}_d')}.extra (id INT PRIMARY KEY)")
    with pytest.raises(PlanError) as caught:
        Run(spec, lock, registry(), RunOptions(stages=(Stage.PLAN,), work=tmp_path / "w")).execute()
    assert caught.value.strict is not None
    assert {(f.difference, f.object) for f in caught.value.strict.findings} == {
        (Difference.NEW_TABLE, f"{prefix}_d.extra")
    }


def test_listing_databases_fails_on_a_closed_connection(source: MysqlScratch) -> None:
    with MysqlEngine().connect(source.connection) as session:
        assert isinstance(session, MysqlSession)
        session.connection.close()
        with pytest.raises(EngineError, match="listing the databases failed"):
            list_databases(session.connection)


# A slice of one database: the second's orders refer to the first's
# customers, a trigger on them calls a function in the third, and
# applications call a function in the first. A standalone image of the
# second pulls all of that in, and nothing else. Taken without rows, the
# customers keep their own foreign key, but the orders lose theirs.
SLICE_SCHEMA = [
    "CREATE DATABASE PREFIX_a",
    "CREATE DATABASE PREFIX_b",
    "CREATE DATABASE PREFIX_c",
    """CREATE TABLE PREFIX_a.customers (
        id INT PRIMARY KEY, email VARCHAR(80) NOT NULL, referrer_id INT,
        CONSTRAINT fk_slice_referrer FOREIGN KEY (referrer_id) REFERENCES PREFIX_a.customers (id)
    )""",
    "CREATE TABLE PREFIX_a.visits (id INT PRIMARY KEY, at DATE)",
    """CREATE TABLE PREFIX_b.orders (
        id INT PRIMARY KEY, customer_id INT NOT NULL, status VARCHAR(10),
        CONSTRAINT fk_slice_customer FOREIGN KEY (customer_id) REFERENCES PREFIX_a.customers (id)
    )""",
    "CREATE FUNCTION PREFIX_c.fresh() RETURNS VARCHAR(10) DETERMINISTIC NO SQL RETURN 'fresh'",
    """CREATE TRIGGER PREFIX_b.orders_fresh BEFORE INSERT ON PREFIX_b.orders FOR EACH ROW
        SET NEW.status = PREFIX_c.fresh()""",
    (
        "CREATE FUNCTION PREFIX_a.greet(n VARCHAR(20)) RETURNS VARCHAR(30) DETERMINISTIC NO SQL"
        " RETURN CONCAT('hello ', n)"
    ),
    "CREATE FUNCTION PREFIX_a.unused() RETURNS INT DETERMINISTIC NO SQL RETURN 1",
    (
        "INSERT INTO PREFIX_a.customers (id, email)"
        " VALUES (1, 'ann@shop.test'), (2, 'bo@shop.test'), (3, 'cy@x')"
    ),
    "INSERT INTO PREFIX_a.visits VALUES (1, '2026-01-01')",
    "INSERT INTO PREFIX_b.orders VALUES (10, 1, 'new'), (11, 2, 'new')",
]

SLICE_SPEC = """
name: scratch
sources:
  app:
    engine: mysql
    connection:
      url:
        env: VARIABLE
      databases: ["PREFIX_*"]
objects:
  - function: PREFIX_a.greet
subset:
  unselected: none
  seeds:
    orders: "true"
    customers: "id = 3"
    visits: "true"
images:
  orders:
    include: ["PREFIX_b.*"]
    standalone: true ROWS
  rest:
    include: ["PREFIX_a.*", "PREFIX_c.*"]
anonymize:
  customers:
    email:
      faker: email
store:
  - filesystem:
      path: STORE
"""

SLICE_CHECKS = {
    "SELECT 1 FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA LIKE 'PREFIX%'": 2,
    "SELECT 1 FROM information_schema.ROUTINES WHERE ROUTINE_NAME = 'unused'": 0,
    "SELECT 1 FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = 'PREFIX_b'": 1,
    "SELECT 1 FROM DUAL WHERE PREFIX_a.greet('x') <> 'hello x'": 0,
    "SELECT 1 FROM DUAL WHERE PREFIX_c.fresh() <> 'fresh'": 0,
    "SELECT 1 FROM information_schema.REFERENTIAL_CONSTRAINTS"
    " WHERE CONSTRAINT_SCHEMA = 'PREFIX_a'": 1,
    "SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA LIKE 'PREFIX%'": 2,
}

ORDERS_REFERENCES = (
    "SELECT 1 FROM information_schema.REFERENTIAL_CONSTRAINTS WHERE CONSTRAINT_SCHEMA = 'PREFIX_b'"
)
"""The orders' foreign key, which customers without rows leave out."""


@pytest.mark.parametrize(
    ("rows", "customers"),
    [
        ("", 2),
        ("\n    rows:\n      customers: all", 3),
        ("\n    rows:\n      customers: none", 0),
    ],
    ids=["referenced", "all", "none"],
)
def test_a_standalone_slice_holds_its_closure(tmp_path: Path, rows: str, customers: int) -> None:
    prefix = f"dd_slice_{uuid.uuid4().hex[:8]}"
    variable = f"DD_{prefix.upper()}"
    parts = urlsplit(os.environ[URL_VARIABLE])
    os.environ[variable] = urlunsplit((parts.scheme, parts.netloc, f"/{prefix}_b", "", ""))
    try:
        for statement in SLICE_SCHEMA:
            admin(statement.replace("PREFIX", prefix))
        spec = parse_spec(
            SLICE_SPEC.replace("ROWS", rows)
            .replace("PREFIX", prefix)
            .replace("VARIABLE", variable)
            .replace("STORE", str(tmp_path / "store"))
            + f"\nruntime: {json.dumps(runtime_for_tests())}\n"
        )
        with MysqlEngine().connect(spec.working.connection) as session:
            lock = CatalogLock.of(session.catalog())
        options = RunOptions(stages=tuple(Stage), work=tmp_path / "work")
        result = Run(spec, lock, registry(), options).execute()
        assert not result.failed(), result.verification
        image_summary = next(s for s in result.images if s.identity.group == "orders")
        orders = TableName.of(f"{prefix}_b", "orders")
        found_customers = TableName.of(f"{prefix}_a", "customers")
        working = working_databases(tmp_path / "check")
        for archive in image_summary.archives:
            image, _ = unpack(tmp_path / "store" / archive.key, tmp_path / f"u-{archive.format}")
            with working.restore(image, archive.format, tmp_path / f"r-{archive.format}") as found:
                assert found.counts([orders, found_customers]) == {
                    orders: 2,
                    found_customers: customers,
                }
                for query, expected in SLICE_CHECKS.items():
                    assert found.failing_rows(query.replace("PREFIX", prefix)) == expected, query
                references = found.failing_rows(ORDERS_REFERENCES.replace("PREFIX", prefix))
                assert references == (1 if customers else 0)
    finally:
        os.environ.pop(variable, None)
        # Foreign keys run between the databases, so whichever goes first
        # would be referenced by one still there.
        admin(
            "SET foreign_key_checks = 0",
            *(
                f"DROP DATABASE IF EXISTS {quote(f'{prefix}{suffix}')}"
                for suffix in ("_a", "_b", "_c")
            ),
        )
