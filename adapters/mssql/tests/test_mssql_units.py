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

from datadouble.catalog import Column, ColumnKind
from datadouble.engine import EngineError, Tooling
from datadouble.names import ColumnName, TableName
from datadouble.registry import installed
from datadouble.runtime import Completed, Container, Server
from datadouble.settings import SettingsError
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
from datadouble_mssql.render import Renderer, as_of_value, native, quote
from datadouble_mssql.script import Phase, Schema, Statement
from datadouble_mssql.server import MssqlServer, Tools, check_identity, default_image, literal
from datadouble_mssql.source import ExtractIndex, as_text, extracted_table, field, schema_for
from datadouble_mssql.working import (
    DatabaseInfo,
    MssqlWorking,
    format_file,
    from_text,
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
    with pytest.raises(EngineError, match=r"set source\.server\.accept_eula"):
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


def test_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    engine = MssqlEngine()
    tooling = Tooling(IdleRuntime(), server={"accept_eula": True})
    monkeypatch.setattr("os.getuid", lambda: 1000)
    monkeypatch.setattr("os.getgid", lambda: 1000)
    with pytest.raises(EngineError, match=r"need Datadouble to run as 10001:10001.*1000:1000"):
        check_identity()
    with pytest.raises(EngineError, match="10001:10001"):
        engine.working(tmp_path, tooling, "16.0.4325.4-CU28")
    monkeypatch.setattr("os.getuid", lambda: 10001)
    monkeypatch.setattr("os.getgid", lambda: 10001)
    check_identity()
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
