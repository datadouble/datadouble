# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The SQL Server adapter's pieces that need no server."""

import datetime as dt
import struct
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

import pymssql
import pytest

from datadouble.catalog import (
    Column,
    ColumnKind,
    Dependency,
    ObjectKind,
    ObjectRef,
    SourceObject,
    definition_fingerprint,
)
from datadouble.closure import Pulled
from datadouble.engine import EngineError, Tooling, group_extract_path
from datadouble.names import ColumnName, GroupName, TableName
from datadouble.planner import PlannedGroup
from datadouble.registry import installed
from datadouble.runtime import Completed, Container, Server
from datadouble.settings import SettingsError
from datadouble.spec import PulledRows
from datadouble.subset import (
    AllOf,
    AllRows,
    AnyNull,
    AnyOf,
    NoRows,
    Predicate,
    SelectedBy,
    ValuesIn,
)
from datadouble_mssql import MssqlEngine, double_quoted, parse_url, server_settings
from datadouble_mssql.errors import database_error, redact, tool_error
from datadouble_mssql.introspect import Physical, kind_of, table_problem, type_text
from datadouble_mssql.objects import read_objects
from datadouble_mssql.render import Renderer, as_of_value, native, quote
from datadouble_mssql.script import Phase, Schema, Statement
from datadouble_mssql.server import MssqlServer, Tools, default_image, literal
from datadouble_mssql.source import (
    ExtractedTable,
    ExtractIndex,
    as_text,
    extracted_table,
    field,
    schema_for,
)
from datadouble_mssql.working import (
    DatabaseInfo,
    LeftOut,
    MssqlWorking,
    format_file,
    from_text,
    group_sources,
    placeholder,
)

AS_OF = dt.datetime(2026, 1, 1, 12, 30, tzinfo=dt.timezone(dt.timedelta(hours=2)))
C = ColumnName
T = TableName.of("dbo", "orders")


def test_conditions() -> None:
    renderer = Renderer(AS_OF)
    assert renderer.condition(AllRows()) == "1 = 1"
    assert renderer.condition(NoRows()) == "1 = 0"
    assert renderer.condition(ValuesIn((C("id"),), frozenset())) == "1 = 0"
    assert renderer.condition(ValuesIn((C("id"),), frozenset({(2,), (1,)}))) == (
        "t.[id] IN (%(p0)s, %(p1)s)"
    )
    assert renderer.condition(ValuesIn((C("a"), C("b")), frozenset({(1, "x")}))) == (
        "EXISTS (SELECT 1 FROM (VALUES (%(p2)s, %(p3)s)) AS s1 (c0, c1)"
        " WHERE s1.c0 = t.[a] AND s1.c1 = t.[b])"
    )
    assert renderer.condition(AnyNull((C("a"), C("b")))) == "(t.[a] IS NULL OR t.[b] IS NULL)"
    assert renderer.condition(AnyOf((AllRows(), AllOf((NoRows(), Predicate("x > 1")))))) == (
        "(1 = 1 OR (1 = 0 AND (x > 1)))"
    )
    assert renderer.params["as_of"] == "2026-01-01T10:30:00.000000"
    assert as_of_value(AS_OF) == "2026-01-01T10:30:00.000000"
    with pytest.raises(EngineError, match="cannot bind a frozenset value"):
        renderer.condition(ValuesIn((C("id"),), frozenset({(frozenset[int](),)})))


def test_nested_selections() -> None:
    parent = TableName.of("dbo", "customer")
    renderer = Renderer(AS_OF, {parent: Predicate("id < :as_of")})
    query = renderer.select(
        T,
        ["t.[id]"],
        SelectedBy((C("customer_id"),), parent, (C("id"),), where="kind = 'x'"),
        distinct=True,
    )
    assert query.text == (
        "WITH [dd_selected_0] AS (SELECT * FROM [dbo].[customer] AS s2"
        " WHERE (id < CAST(%(as_of)s AS DATETIME2(7)))) "
        "SELECT DISTINCT t.[id] FROM [dbo].[orders] AS t WHERE "
        "t.[customer_id] IN (SELECT s1.[id] FROM [dd_selected_0] AS s1 WHERE (kind = 'x'))"
    )
    inline = Renderer(AS_OF).condition(
        SelectedBy((C("a"), C("b")), parent, (C("x"), C("y")), source_condition=AllRows())
    )
    assert inline == (
        "EXISTS (SELECT 1 FROM [dbo].[customer] AS s1"
        " WHERE s1.[x] = t.[a] AND s1.[y] = t.[b] AND 1 = 1)"
    )
    bare = Renderer(AS_OF).condition(
        SelectedBy((C("a"),), parent, (C("x"),), source_condition=AllRows())
    )
    assert bare == "t.[a] IN (SELECT s1.[x] FROM [dbo].[customer] AS s1 WHERE 1 = 1)"
    keys = Renderer(AS_OF).keys(T, (C("id"),), AllRows())
    assert keys.text == "SELECT DISTINCT t.[id] FROM [dbo].[orders] AS t WHERE 1 = 1"
    with pytest.raises(EngineError, match="no selection"):
        Renderer(AS_OF).condition(SelectedBy((C("a"),), parent, (C("x"),)))


def test_quoting() -> None:
    assert quote("we]ird") == "[we]]ird]"
    assert native("x LIKE '50%' AND t < :as_of AND y::int = 1") == (
        "x LIKE '50%' AND t < CAST(%(as_of)s AS DATETIME2(7)) AND y::int = 1"
    )
    assert literal("it's") == "N'it''s'"
    assert double_quoted('a"b$c`d\\e') == 'a\\"b\\$c\\`d\\\\e'


def test_errors() -> None:
    message = b"Violation of PRIMARY KEY constraint 'PK_x'. The duplicate key value is (alice, 1)."
    error = pymssql.IntegrityError(2627, message + b"DB-Lib error message 20018, severity 14:\n")
    assert str(database_error(error, "loading")) == (
        "loading failed: [2627] Violation of PRIMARY KEY constraint '…'."
        " The duplicate key value is (…)."
    )
    assert str(database_error(pymssql.OperationalError(241, "converting 'x'"), "loading")) == (
        "loading failed: [241] converting '…'"
    )
    assert str(database_error(pymssql.InterfaceError(), "loading")) == (
        "loading failed: InterfaceError"
    )
    assert redact('name "secret" here') == "name '…' here"
    assert str(tool_error("sqlcmd", 1, "line 'one'\n\n")) == "sqlcmd exited with status 1: line '…'"
    assert str(tool_error("sqlcmd", 2, "")) == "sqlcmd exited with status 2: no output"


def test_types() -> None:
    assert kind_of("INT") is ColumnKind.INTEGER
    assert kind_of("geometry") is ColumnKind.OTHER
    assert type_text("nvarchar", 100, 0, 0) == "nvarchar(50)"
    assert type_text("varbinary", -1, 0, 0) == "varbinary(max)"
    assert type_text("decimal", 9, 10, 2) == "decimal(10,2)"
    assert type_text("datetime2", 8, 27, 7) == "datetime2(7)"
    assert type_text("timestamp", 8, 0, 0) == "rowversion"
    assert type_text("int", 4, 10, 0) == "int"
    assert table_problem((0, 0, 0, 0, 0, 0, 0, 0)) is None
    assert table_problem((1, 0, 0, 0, 0, 1, 0, 0)) == (
        "is a memory-optimized table, which the SQL Server adapter does not support yet"
    )
    assert table_problem((0, 0, 0, 0, 0, 0, 0, 1)) == (
        "has a columnstore, XML, spatial or full-text index, which the SQL Server adapter does"
        " not support yet"
    )
    physical = Physical(
        types={T: {C("id"): "int", C("label"): "varchar", C("v"): "timestamp"}},
        unloadable={T: frozenset({C("v")})},
    )
    assert physical.loaded(T) == (C("id"), C("label"))
    entry = extracted_table(physical, T, "00000.dat", 3)
    assert (entry.columns, entry.ordinals, entry.rows) == ((C("id"), C("label")), (1, 2), 3)
    index = ExtractIndex(database="shop", server_version="16.0.1-CU1", tables=(entry,))
    assert index.table(T) == entry
    assert index.table(TableName.of("dbo", "missing")) is None
    assert format_file(entry) == ('14.0\n2\n1 SQLNCHAR 4 0 "" 1 c1 ""\n2 SQLNCHAR 4 0 "" 2 c2 ""\n')


def test_text_forms() -> None:
    assert as_text("c", "varbinary") == "CONVERT(NVARCHAR(MAX), CAST(c AS VARBINARY(MAX)), 2)"
    assert as_text("c", "datetime2") == "CONVERT(NVARCHAR(40), c, 126)"
    assert as_text("c", "float") == "CONVERT(NVARCHAR(40), c, 3)"
    assert as_text("c", "nvarchar") == "CAST(c AS NVARCHAR(MAX))"
    assert from_text("v", "image") == "CONVERT(VARBINARY(MAX), v, 2)"
    assert from_text("v", "int") == "v"
    assert field(None) == struct.pack("<i", -1)
    assert field("") == struct.pack("<i", 0)
    assert field("é") == struct.pack("<i", 2) + "é".encode("utf-16-le")


def test_schema_filtering() -> None:
    excluded = TableName.of("dbo", "secret")
    statements = (
        Statement(phase=Phase.SCHEMA, name="sales", sql="CREATE SCHEMA [sales]"),
        Statement(phase=Phase.TABLE, name="dbo.secret", sql="CREATE TABLE x", table=excluded),
        Statement(phase=Phase.TABLE, name="dbo.orders", sql="CREATE TABLE y", table=T),
    )
    schema = Schema(
        collation="SQL_Latin1_General_CP1_CI_AS",
        compatibility_level=160,
        read_committed_snapshot=False,
        statements=statements,
    )
    kept = schema_for(schema, frozenset({excluded}))
    assert [s.name for s in kept.statements] == ["sales", "dbo.orders"]
    assert [s.name for s in kept.phase(Phase.TABLE)] == ["dbo.orders"]


def test_urls_and_settings() -> None:
    assert parse_url("mssql://u%40x:p%2Fw@db.internal/shop") == (
        {"server": "db.internal", "port": "1433", "user": "u@x", "password": "p/w"},
        "shop",
    )
    for bad in ("mysql://h/db", "mssql:///db", "mssql://h/"):
        with pytest.raises(EngineError, match="mssql url must look like"):
            parse_url(bad)
    assert server_settings({"accept_eula": True}).edition == "Developer"
    assert server_settings({"accept_eula": True, "edition": "Express"}).edition == "Express"
    with pytest.raises(EngineError, match=r"set sources\.<name>\.server\.accept_eula"):
        server_settings({})
    with pytest.raises(SettingsError, match="accept_eula"):
        server_settings({"accept_eula": False})
    with pytest.raises(SettingsError, match="memory"):
        server_settings({"accept_eula": True, "memory": 1})


def test_placeholders() -> None:
    text = Column(name=C("n"), type="varchar(5)", kind=ColumnKind.TEXT, nullable=False)
    assert placeholder(T, text) == "''"
    with pytest.raises(EngineError, match="no placeholder for g"):
        placeholder(T, Column(name=C("g"), type="geometry", kind=ColumnKind.OTHER, nullable=False))


def test_images() -> None:
    assert default_image("16.0.4325.4-CU28") == (
        "mcr.microsoft.com/mssql/server:2022-CU28-ubuntu-22.04"
    )
    assert default_image("17.0.1000.7-RTM") == (
        "mcr.microsoft.com/mssql/server:2025-RTM-ubuntu-22.04"
    )
    assert default_image("16") == "mcr.microsoft.com/mssql/server:2022-latest"
    with pytest.raises(EngineError, match=r"no SQL Server image is known for version 9\.0"):
        default_image("9.0.1-SP4")


@dataclass
class IdleRuntime:
    """A runtime that is never asked to run anything."""

    def run(self, container: Container) -> Completed:
        raise NotImplementedError

    def serve(self, container: Container) -> AbstractContextManager[Server]:
        raise NotImplementedError

    def release(self, owner: Path) -> None:
        raise NotImplementedError


def test_working(tmp_path: Path) -> None:
    engine = MssqlEngine()
    assert engine.owner == "10001:10001"
    tooling = Tooling(IdleRuntime(), server={"accept_eula": True})
    working = engine.working(tmp_path, tooling, "16.0.4325.4-CU28")
    assert isinstance(working, MssqlWorking)
    assert working.tools.image == "mcr.microsoft.com/mssql/server:2022-CU28-ubuntu-22.04"
    server = MssqlServer(Tools(IdleRuntime(), "image"), tmp_path / "db", tmp_path)
    assert server.data == tmp_path / "db" / "data"
    with pytest.raises(EngineError, match="not running"):
        _ = server.server


def test_engine(tmp_path: Path) -> None:
    engine = installed().engine("mssql")
    assert isinstance(engine, MssqlEngine)
    assert engine.name == "mssql"
    capabilities = engine.capabilities()
    assert (capabilities.nested, capabilities.hooks, capabilities.expressions) == (True, True, True)
    (tmp_path / "database.json").write_text(
        DatabaseInfo(
            database="sh'op]", server_version="16.0.4325.4-CU28", edition="Developer"
        ).model_dump_json(),
        "utf-8",
    )
    plan = engine.server(tmp_path, None)
    assert (plan.image, plan.port, plan.database, plan.user, plan.owner) == (
        "mcr.microsoft.com/mssql/server:2022-CU28-ubuntu-22.04",
        1433,
        "sh'op]",
        "datadouble",
        "10001:10001",
    )
    script = plan.command[2]
    assert "CREATE DATABASE [sh'op]]] ON (FILENAME = N'/var/lib/datadouble/data/database.mdf')" in (
        script
    )
    assert "WITH PASSWORD = N'$password'" in script
    assert "MSSQL_PID=Developer " in script
    assert engine.server(tmp_path, "mirror/mssql:2022").image == "mirror/mssql:2022"


class ScriptedCursor:
    """Answers each catalog query with the rows of the one key it contains."""

    def __init__(self, rows: dict[str, list[tuple[object, ...]]]) -> None:
        self.rows = rows
        self.found: list[tuple[object, ...]] = []

    def execute(self, operation: str, /) -> None:
        (key,) = [key for key in self.rows if key in operation]
        self.found = self.rows[key]

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.found


def test_tables_need_the_objects_their_constraints_and_columns_use() -> None:
    orders, customers = TableName.of("shop", "orders"), TableName.of("shop", "customers")
    total = ObjectRef(kind=ObjectKind.FUNCTION, namespace=("shop",), name="total")
    money = ObjectRef(kind=ObjectKind.TYPE, namespace=("shop",), name="money_t")
    cursor = ScriptedCursor(
        {
            "FROM sys.tables t JOIN": [(1, "shop", "orders"), (2, "shop", "customers")],
            "sys.sql_modules": [(10, "shop", "total", "FN", "RETURN 1", 0)],
            "sys.sequences": [],
            "FROM sys.types t JOIN": [(100, "shop", "money_t", "decimal(9,2)")],
            "('D', 'C')": [(20, 1), (21, 1)],
            "sql_expression_dependencies": [
                (20, 1, 10, "shop", "total"),  # a check constraint on orders calls total
                (21, 1, 1, "shop", "orders"),  # a default reads its own table
                (21, 1, 2, "shop", "customers"),  # ... or another, which is no object to need
            ],
            "SELECT DISTINCT c.object_id": [
                (1, 100),  # a column of orders has the alias type
                (2, 999),  # a type the catalog does not list
            ],
        }
    )
    objects, needs = read_objects(cursor)
    assert tuple(item.ref for item in objects) == (total, money)
    assert needs == {orders: (Dependency(object=total), Dependency(object=money))}
    assert customers not in needs


def test_a_standalone_database_leaves_out_what_its_closure_lacks() -> None:
    orders, customers = TableName.of("dbo", "orders"), TableName.of("dbo", "customers")
    kept = ObjectRef(kind=ObjectKind.VIEW, namespace=("dbo",), name="kept")
    lacked = ObjectRef(kind=ObjectKind.PROCEDURE, namespace=("sales",), name="lacked")
    kind = ObjectRef(kind=ObjectKind.TYPE, namespace=("dbo",), name="kind")
    objects = [
        SourceObject(ref=ref, fingerprint=definition_fingerprint(ref.name))
        for ref in (kept, lacked, kind)
    ]
    group = PlannedGroup(
        name=GroupName("orders"),
        tables=(orders,),
        requires=(),
        omitted_references=(),
        fixups=(),
        standalone=True,
        objects=(kept,),
        pulled=(
            Pulled(table=customers, path=("dbo.orders", "dbo.customers"), rows=PulledRows.NONE),
        ),
    )
    left_out = LeftOut.of(objects, group)
    # Types are not scripted, so only the procedure is left out.
    assert left_out == LeftOut(
        objects=frozenset({(Phase.MODULE, "sales.lacked")}), rowless=frozenset({"dbo.customers"})
    )
    assert LeftOut.of(objects, group.model_copy(update={"standalone": False})) == LeftOut()

    present = {"dbo.orders", "dbo.customers", "dbo.kept"}
    tables = frozenset({orders, customers})

    def wanted(statement: Statement, among: frozenset[TableName] | None = None) -> bool:
        return left_out.wanted(statement, among, present)

    assert not wanted(Statement(phase=Phase.MODULE, name="sales.lacked", sql="x"))
    assert wanted(Statement(phase=Phase.MODULE, name="dbo.kept", sql="x", needs=("dbo.orders",)))
    # A table with rows keeps no foreign key into one without; one without may.
    into = ("dbo.customers",)
    assert not wanted(
        Statement(phase=Phase.FOREIGN_KEY, name="fk", sql="x", table=orders, needs=into)
    )
    assert wanted(
        Statement(phase=Phase.FOREIGN_KEY, name="fk", sql="x", table=customers, needs=into)
    )
    assert wanted(Statement(phase=Phase.TABLE, name="dbo.orders", sql="x", table=orders), tables)
    other = TableName.of("dbo", "other")
    assert not wanted(Statement(phase=Phase.TABLE, name="dbo.other", sql="x", table=other), tables)
    assert not wanted(Statement(phase=Phase.INDEX, name="ix", sql="x", table=other))
    assert not wanted(Statement(phase=Phase.MODULE, name="v", sql="x", needs=("dbo.other",)))


def _index(*tables: TableName) -> ExtractIndex:
    return ExtractIndex(
        database="shop",
        server_version="16.0.4325.4",
        tables=tuple(
            ExtractedTable(table=t, file=f"{t.name}.dat", columns=(C("id"),), ordinals=(1,), rows=1)
            for t in tables
        ),
    )


def test_a_standalone_group_loads_its_own_extract_first(tmp_path: Path) -> None:
    orders, lines = TableName.of("dbo", "orders"), TableName.of("dbo", "lines")
    customers = TableName.of("dbo", "customers")
    extract = tmp_path / "extract"
    extract.mkdir()
    shared = _index(orders, lines, customers)
    group = PlannedGroup(
        name=GroupName("orders"),
        tables=(orders, lines),
        requires=(),
        omitted_references=(),
        fixups=(),
    )
    assert [(f, t.table) for f, t in group_sources(group, shared, extract)] == [
        (extract, orders),
        (extract, lines),
    ]
    own = group_extract_path(extract, group.name)
    own.mkdir(parents=True)
    (own / "index.json").write_text(_index(customers, orders).model_dump_json(), "utf-8")
    # The pulled-in rows and the own table that gained some come from the
    # group's extract, the group's other tables from the shared one.
    assert [(f, t.table) for f, t in group_sources(group, shared, extract)] == [
        (own, customers),
        (own, orders),
        (extract, lines),
    ]
