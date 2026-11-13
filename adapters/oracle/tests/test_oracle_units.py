# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The Oracle adapter's pieces that need no server."""

import datetime as dt
import struct
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

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
from datadouble_oracle import OracleEngine, parse_url
from datadouble_oracle.driver import read
from datadouble_oracle.errors import redact
from datadouble_oracle.introspect import (
    Declared,
    Physical,
    family,
    kind_of,
    table_problem,
    type_text,
)
from datadouble_oracle.render import Renderer, as_of_value, bindable, quote, uses_as_of
from datadouble_oracle.script import Phase, Schema, Statement, split_trigger
from datadouble_oracle.server import OracleServer, Tools, default_image, literal
from datadouble_oracle.source import (
    ExtractIndex,
    as_text,
    extracted_table,
    field,
    from_text,
    schema_for,
)
from datadouble_oracle.working import DatabaseInfo, OracleWorking, placeholder, read_rows

AS_OF = dt.datetime(2026, 1, 1, 12, 30, tzinfo=dt.timezone(dt.timedelta(hours=2)))
UTC_AS_OF = dt.datetime(2026, 1, 1, 10, 30, tzinfo=dt.UTC).replace(tzinfo=None)
C = ColumnName
T = TableName.of("SHOP", "ORDERS")


def test_conditions() -> None:
    renderer = Renderer(AS_OF)
    assert renderer.condition(AllRows()) == "1 = 1"
    assert renderer.condition(NoRows()) == "1 = 0"
    assert renderer.condition(ValuesIn((C("ID"),), frozenset())) == "1 = 0"
    assert renderer.condition(ValuesIn((C("ID"),), frozenset({(2,), (1,)}))) == (
        't."ID" IN (:p0, :p1)'
    )
    assert renderer.condition(ValuesIn((C("A"), C("B")), frozenset({(1, "x")}))) == (
        '(t."A", t."B") IN ((:p2, :p3))'
    )
    assert renderer.condition(AnyNull((C("A"), C("B")))) == '(t."A" IS NULL OR t."B" IS NULL)'
    assert renderer.condition(AnyOf((AllRows(), AllOf((NoRows(), Predicate("x > 1")))))) == (
        "(1 = 1 OR (1 = 0 AND (x > 1)))"
    )
    assert "as_of" not in renderer.params
    assert renderer.condition(Predicate("t < :as_of")) == "(t < :as_of)"
    assert renderer.params["as_of"] == UTC_AS_OF
    many = Renderer(AS_OF).condition(ValuesIn((C("ID"),), frozenset((i,) for i in range(1500))))
    assert many.startswith('(t."ID" IN (')
    assert ' OR t."ID" IN (' in many
    with pytest.raises(EngineError, match="cannot bind a frozenset value"):
        bindable(frozenset[int]())


def test_nested_selections() -> None:
    parent = TableName.of("SHOP", "CUSTOMER")
    renderer = Renderer(AS_OF, {parent: Predicate("id < :as_of")})
    query = renderer.select(
        T,
        ['t."ID"'],
        SelectedBy((C("CUSTOMER_ID"),), parent, (C("ID"),), where="kind = 'x'"),
        distinct=True,
    )
    assert query.text == (
        'WITH "DD_SELECTED_0" AS (SELECT * FROM "SHOP"."CUSTOMER" s2 WHERE (id < :as_of)) '
        'SELECT DISTINCT t."ID" FROM "SHOP"."ORDERS" t WHERE '
        't."CUSTOMER_ID" IN (SELECT s1."ID" FROM "DD_SELECTED_0" s1 WHERE (kind = \'x\'))'
    )
    inline = Renderer(AS_OF).condition(
        SelectedBy((C("A"), C("B")), parent, (C("X"), C("Y")), source_condition=AllRows())
    )
    assert (
        inline == '(t."A", t."B") IN (SELECT s1."X", s1."Y" FROM "SHOP"."CUSTOMER" s1 WHERE 1 = 1)'
    )
    keys = Renderer(AS_OF).keys(T, (C("ID"),), AllRows())
    assert keys.text == 'SELECT DISTINCT t."ID" FROM "SHOP"."ORDERS" t WHERE 1 = 1'
    with pytest.raises(EngineError, match="no selection"):
        Renderer(AS_OF).condition(SelectedBy((C("A"),), parent, (C("X"),)))


def test_quoting_and_errors() -> None:
    assert quote('we"ird') == '"we""ird"'
    assert literal("it's") == "'it''s'"
    assert uses_as_of("x < :as_of")
    assert not uses_as_of("x < :as_offset")
    assert as_of_value(AS_OF) == UTC_AS_OF
    message = (
        "ORA-00001: unique constraint (SHOP.PK) violated on table SHOP.T columns (EMAIL)\n"
        "ORA-03301: (ORA-00001 details) row with column values (EMAIL:'a@b.c') already exists"
    )
    assert redact(message) == (
        "ORA-00001: unique constraint (SHOP.PK) violated on table SHOP.T columns (EMAIL)"
    )
    assert redact("ORA-12899: value too large: 'secret'") == "ORA-12899: value too large: '…'"
    assert redact("ORA-1: insert values (1, 'x')") == "ORA-1: insert values (…)"
    assert redact("") == ""


def test_types() -> None:
    assert family("TIMESTAMP(6) WITH TIME ZONE") == "TIMESTAMP WITH TIME ZONE"
    assert family("INTERVAL DAY(2) TO SECOND(6)") == "INTERVAL DAY TO SECOND"
    assert family("INTERVAL YEAR(2) TO MONTH") == "INTERVAL YEAR TO MONTH"
    assert kind_of("NUMBER", 0) is ColumnKind.INTEGER
    assert kind_of("NUMBER", 2) is ColumnKind.DECIMAL
    assert kind_of("DATE", None) is ColumnKind.TIMESTAMP
    assert kind_of("INTERVAL DAY(2) TO SECOND(6)", None) is ColumnKind.OTHER
    assert type_text(Declared("NUMBER", 22, 5, 2, 0, "B")) == "NUMBER(5,2)"
    assert type_text(Declared("NUMBER", 22, None, 0, 0, "B")) == "NUMBER(*,0)"
    assert type_text(Declared("VARCHAR2", 180, None, None, 45, "C")) == "VARCHAR2(45 CHAR)"
    assert type_text(Declared("CHAR", 1, None, None, 1, "B")) == "CHAR(1 BYTE)"
    assert type_text(Declared("NVARCHAR2", 100, None, None, 50, "C")) == "NVARCHAR2(50)"
    assert type_text(Declared("RAW", 8, None, None, 0, "B")) == "RAW(8)"
    assert type_text(Declared("CLOB", 4000, None, None, 0, "B")) == "CLOB"
    assert table_problem((0, 0, 0, 0)) is None
    assert table_problem((0, 1, 1, 0)) == (
        "is an external table, which the Oracle adapter does not support yet"
    )
    physical = Physical(
        types={T: {C("ID"): "NUMBER", C("LABEL"): "VARCHAR2", C("V"): "VARCHAR2"}},
        virtual={T: frozenset({C("V")})},
    )
    assert physical.loaded(T) == (C("ID"), C("LABEL"))
    entry = extracted_table(physical, T, "00000.dat", 3)
    assert (entry.columns, entry.types, entry.rows) == (
        (C("ID"), C("LABEL")),
        ("NUMBER", "VARCHAR2"),
        3,
    )
    index = ExtractIndex(schemas=("SHOP",), server_version="23.26.3.0.0", tables=(entry,))
    assert index.table(T) == entry
    assert index.table(TableName.of("SHOP", "MISSING")) is None


def test_text_forms(tmp_path: Path) -> None:
    assert as_text("c", "NUMBER") == "TO_CHAR(c, 'TM9')"
    assert as_text("c", "TIMESTAMP WITH TIME ZONE") == (
        "TO_CHAR(c, 'SYYYY-MM-DD HH24:MI:SS.FF9 TZR')"
    )
    assert as_text("c", "VARCHAR2") == "c"
    assert from_text(":1", "DATE") == "TO_DATE(:1, 'SYYYY-MM-DD HH24:MI:SS')"
    assert from_text(":1", "RAW") == "HEXTORAW(:1)"
    assert from_text(":1", "CLOB") == ":1"
    assert field(None) == struct.pack("<i", -1)
    assert field(b"\x00\xff") == struct.pack("<i", 4) + b"00ff"
    assert field("é") == struct.pack("<i", 2) + "é".encode()
    assert read("x") == "x"
    data = tmp_path / "rows.dat"
    data.write_bytes(field("a") + field(None) + field("") + field("b") + struct.pack("<i", 1))
    assert list(read_rows(data, 2)) == [["a", None], ["", "b"]]


def test_schema_filtering_and_triggers() -> None:
    excluded = TableName.of("SHOP", "SECRET")
    statements = (
        Statement(phase=Phase.SCHEMA, owner="SHOP", name="SHOP.SEQ", sql="CREATE SEQUENCE s"),
        Statement(phase=Phase.TABLE, owner="SHOP", name="SHOP.SECRET", sql="x", table=excluded),
        Statement(phase=Phase.TABLE, owner="SHOP", name="SHOP.ORDERS", sql="y", table=T),
    )
    schema = Schema(schemas=("SHOP",), statements=statements, identities=())
    kept = schema_for(schema, frozenset({excluded}))
    assert [s.name for s in kept.statements] == ["SHOP.SEQ", "SHOP.ORDERS"]
    assert [s.name for s in kept.phase(Phase.TABLE)] == ["SHOP.ORDERS"]
    ddl = 'CREATE OR REPLACE TRIGGER "SHOP"."T" BEFORE INSERT ON t BEGIN NULL; END;'
    assert split_trigger(ddl + '\nALTER TRIGGER "SHOP"."T" ENABLE') == (ddl, False)
    assert split_trigger(ddl + '\nALTER TRIGGER "SHOP"."T" DISABLE') == (ddl, True)
    assert split_trigger(ddl) == (ddl, False)


def test_urls_settings_and_images() -> None:
    assert parse_url("oracle://u%40x:p%2Fw@db.internal/SHOPPDB") == {
        "user": "u@x",
        "password": "p/w",
        "dsn": "db.internal:1521/SHOPPDB",
    }
    for bad in ("mysql://h/db", "oracle:///db", "oracle://h/"):
        with pytest.raises(EngineError, match="oracle url must look like"):
            parse_url(bad)
    assert (
        default_image("23.26.3.0.0") == "container-registry.oracle.com/database/free:23.26.3.0-lite"
    )
    with pytest.raises(EngineError, match=r"no Oracle image is known for version 19c"):
        default_image("19c")


def test_placeholders() -> None:
    text = Column(name=C("N"), type="VARCHAR2(5 BYTE)", kind=ColumnKind.TEXT, nullable=False)
    assert placeholder(T, text) == "' '"
    with pytest.raises(EngineError, match="no placeholder for G"):
        placeholder(T, Column(name=C("G"), type="INTERVAL", kind=ColumnKind.OTHER, nullable=False))


@dataclass
class IdleRuntime:
    """A runtime that is never asked to run anything."""

    def run(self, container: Container) -> Completed:
        raise NotImplementedError

    def serve(self, container: Container) -> AbstractContextManager[Server]:
        raise NotImplementedError

    def release(self, owner: Path) -> None:
        raise NotImplementedError


def test_engine(tmp_path: Path) -> None:
    engine = installed().engine("oracle")
    assert isinstance(engine, OracleEngine)
    assert (engine.name, engine.owner) == ("oracle", "54321:54321")
    capabilities = engine.capabilities()
    assert (capabilities.nested, capabilities.hooks, capabilities.expressions) == (True, True, True)
    working = engine.working(tmp_path, Tooling(IdleRuntime()), "23.26.3.0.0")
    assert isinstance(working, OracleWorking)
    assert working.tools.image.endswith("free:23.26.3.0-lite")
    with pytest.raises(SettingsError, match="edition"):
        engine.working(tmp_path, Tooling(IdleRuntime(), server={"edition": "x"}), "23.26.3.0.0")
    server = OracleServer(Tools(IdleRuntime(), "image"), tmp_path / "db", tmp_path)
    assert server.data == tmp_path / "db" / "data"
    with pytest.raises(EngineError, match="not running"):
        _ = server.server
    (tmp_path / "database.json").write_text(
        DatabaseInfo(
            schemas=("SHOP",), server_version="23.26.3.0.0", unplugged_from="/w/a'b/data"
        ).model_dump_json(),
        "utf-8",
    )
    plan = engine.server(tmp_path, None)
    assert (plan.image, plan.port, plan.database, plan.user, plan.owner) == (
        "container-registry.oracle.com/database/free:23.26.3.0-lite",
        1521,
        "DATADOUBLE",
        "datadouble",
        "54321:54321",
    )
    script = plan.command[2]
    assert "SOURCE_FILE_NAME_CONVERT = ('/w/a''b/data/', '/var/lib/datadouble/data/')" in script
    assert 'IDENTIFIED BY "$password"' in script
    assert engine.server(tmp_path, "mirror/oracle:23").image == "mirror/oracle:23"
