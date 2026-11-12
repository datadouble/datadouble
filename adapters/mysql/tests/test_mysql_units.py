# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The MySQL adapter's pieces that need no server."""

import datetime as dt
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

import pymysql
import pytest

from datadouble.catalog import Column, ColumnKind
from datadouble.engine import EngineError
from datadouble.names import ColumnName, TableName
from datadouble.registry import installed
from datadouble.runtime import Completed, Container, Server
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
from datadouble_mysql import MysqlEngine, parse_url
from datadouble_mysql.errors import database_error, redact, tool_error
from datadouble_mysql.introspect import kind_of
from datadouble_mysql.render import Renderer, native, quote
from datadouble_mysql.server import MysqlServer, Tools, create_user, default_image
from datadouble_mysql.source import field
from datadouble_mysql.working import DatabaseInfo, placeholder, without_definers

AS_OF = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
C = ColumnName
T = TableName.of("shop", "orders")


def test_conditions() -> None:
    renderer = Renderer(AS_OF)
    assert renderer.condition(AllRows()) == "TRUE"
    assert renderer.condition(NoRows()) == "FALSE"
    assert renderer.condition(ValuesIn((C("id"),), frozenset())) == "FALSE"
    assert renderer.condition(ValuesIn((C("id"),), frozenset({(2,), (1,)}))) == (
        "`id` IN (%(p0)s, %(p1)s)"
    )
    assert renderer.condition(ValuesIn((C("a"), C("b")), frozenset({(1, "x")}))) == (
        "(`a`, `b`) IN ((%(p2)s, %(p3)s))"
    )
    assert renderer.condition(AnyNull((C("a"), C("b")))) == "(`a` IS NULL OR `b` IS NULL)"
    assert renderer.condition(AnyOf((AllRows(), AllOf((NoRows(), Predicate("x > 1")))))) == (
        "(TRUE OR (FALSE AND (x > 1)))"
    )
    assert renderer.params["as_of"] == AS_OF


def test_nested_selections() -> None:
    parent = TableName.of("shop", "customer")
    renderer = Renderer(AS_OF, {parent: Predicate("id < :as_of")})
    query = renderer.select(
        T,
        (C("id"),),
        SelectedBy((C("customer_id"),), parent, (C("id"),), where="kind = 'x'"),
        distinct=True,
    )
    assert query.text == (
        "WITH `dd_selected_0` AS (SELECT * FROM `shop`.`customer` WHERE (id < %(as_of)s)) "
        "SELECT DISTINCT `id` FROM `shop`.`orders` WHERE "
        "`customer_id` IN (SELECT `id` FROM `dd_selected_0` WHERE (kind = 'x'))"
    )
    inline = Renderer(AS_OF).condition(
        SelectedBy((C("a"), C("b")), parent, (C("x"), C("y")), source_condition=AllRows())
    )
    assert inline == "(`a`, `b`) IN (SELECT `x`, `y` FROM `shop`.`customer` WHERE TRUE)"
    with pytest.raises(EngineError, match="no selection"):
        Renderer(AS_OF).condition(SelectedBy((C("a"),), parent, (C("x"),)))


def test_quoting() -> None:
    assert quote("we`ird") == "`we``ird`"
    assert native("x LIKE '50%' AND t < :as_of AND y::int = 1") == (
        "x LIKE '50%%' AND t < %(as_of)s AND y::int = 1"
    )


def test_errors() -> None:
    assert redact("Duplicate entry 'alice@example.com' for key `email`") == (
        "Duplicate entry '…' for key `email`"
    )
    error = pymysql.err.IntegrityError(1062, "Duplicate entry 'x' for key 'p'")
    assert str(database_error(error, "loading")) == (
        "loading failed: [1062] Duplicate entry '…' for key '…'"
    )
    assert str(database_error(pymysql.err.InterfaceError(), "loading")) == (
        "loading failed: InterfaceError"
    )
    message = tool_error("mysql", 1, "mysql: [Warning] insecure\nERROR 1 'secret'\n")
    assert str(message) == "mysql exited with status 1: ERROR 1 '…'"
    assert str(tool_error("mysql", 2, "")) == "mysql exited with status 2: no output"


def test_fields() -> None:
    assert field(None) == b"\\N"
    assert field(b"plain") == b"plain"
    assert field("tab\there") == b"tab\\there"
    assert field(b"back\\slash\nline") == b"back\\\\slash\\nline"
    assert field(12) == b"12"


def test_urls() -> None:
    assert parse_url("mysql://u%40x:p%2Fw@db.internal/shop") == (
        {"host": "db.internal", "port": "3306", "user": "u@x", "password": "p/w"},
        "shop",
    )
    for bad in ("postgres://h/db", "mysql:///db", "mysql://h/"):
        with pytest.raises(EngineError, match="mysql url must look like"):
            parse_url(bad)


def test_kinds_and_placeholders() -> None:
    assert kind_of("INT") is ColumnKind.INTEGER
    assert kind_of("enum") is ColumnKind.OTHER
    assert (
        placeholder(T, Column(name=C("n"), type="varchar(5)", kind=ColumnKind.TEXT, nullable=False))
        == "''"
    )
    with pytest.raises(EngineError, match="no placeholder for g"):
        placeholder(T, Column(name=C("g"), type="point", kind=ColumnKind.OTHER, nullable=False))


def test_dumps_lose_their_definers() -> None:
    dump = "CREATE DEFINER=`root`@`%` VIEW v AS SELECT 1;\nCREATE DEFINER=`a`@`b` TRIGGER t"
    assert without_definers(dump) == "CREATE VIEW v AS SELECT 1;\nCREATE TRIGGER t"


@dataclass
class ScriptedRuntime:
    """Answers every container with ``reply``."""

    reply: Completed

    def run(self, container: Container) -> Completed:
        return self.reply

    def serve(self, container: Container) -> AbstractContextManager[Server]:
        raise NotImplementedError

    def release(self, owner: Path) -> None:
        raise NotImplementedError


def tools(stdout: str = "", stderr: str = "", status: int = 0) -> Tools:
    return Tools(ScriptedRuntime(Completed(status, stdout, stderr)), "mysql:8.4.13")


def test_tools(tmp_path: Path) -> None:
    reply = "/usr/sbin/mysqld  Ver 8.4.13 for Linux on x86_64 (MySQL Community Server - GPL)\n"
    assert tools(stdout=reply).version(tmp_path) == "8.4.13"
    with pytest.raises(EngineError, match="cannot read the MySQL version"):
        tools(stdout="nothing").version(tmp_path)
    with pytest.raises(EngineError, match="mysqld exited with status 3: boom"):
        tools(stderr="boom", status=3).version(tmp_path)
    server = MysqlServer(tools(), tmp_path / "db" / "mysql", tmp_path / "db")
    with pytest.raises(EngineError, match="not running"):
        _ = server.server
    assert default_image("8.4.13") == "mysql:8.4.13"
    script = create_user("x")
    assert "'x'@'%%' IDENTIFIED BY '%s'" in script
    assert '"$DATADOUBLE_PASSWORD"' in script


def test_engine(tmp_path: Path) -> None:
    engine = installed().engine("mysql")
    assert isinstance(engine, MysqlEngine)
    capabilities = engine.capabilities()
    assert (capabilities.nested, capabilities.hooks, capabilities.expressions) == (True, True, True)
    (tmp_path / "database.json").write_text(
        DatabaseInfo(database="shop", server_version="8.4.13").model_dump_json(), "utf-8"
    )
    plan = engine.server(tmp_path, None)
    assert (plan.image, plan.port, plan.database, plan.user, plan.owner) == (
        "mysql:8.4.13",
        3306,
        "shop",
        "datadouble",
        "999:999",
    )
    assert engine.server(tmp_path, "mirror/mysql:8").image == "mirror/mysql:8"
