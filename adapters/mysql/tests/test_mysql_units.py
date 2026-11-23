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
from datadouble_mysql.introspect import kind_of, require_view_usage
from datadouble_mysql.render import Renderer, native, quote
from datadouble_mysql.server import MysqlServer, Tools, create_user, default_image
from datadouble_mysql.source import field
from datadouble_mysql.working import (
    DatabaseInfo,
    create_database,
    placeholder,
    without_definers,
)

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
        "loading failed: [1062] Duplicate entry '…' for key 'p'"
    )
    assert str(database_error(pymysql.err.InterfaceError(), "loading")) == (
        "loading failed: InterfaceError"
    )
    message = tool_error("mysql", 1, "mysql: [Warning] insecure\nERROR 1 'secret'\n")
    assert str(message) == "mysql exited with status 1: ERROR 1 '…'"
    assert str(tool_error("mysql", 2, "")) == "mysql exited with status 2: no output"


def test_redaction() -> None:
    # Data values are hidden, in every shape listed.
    hidden = {
        "Duplicate entry 'alice@example.com' for key 'users.email'": (
            "Duplicate entry '…' for key 'users.email'"
        ),
        "Duplicate entry 'O'Brien-1' for key 'PRIMARY'": "Duplicate entry '…' for key 'PRIMARY'",
        "Incorrect integer value: 'abc' for column 'age' at row 1": (
            "Incorrect integer value: '…' for column 'age' at row 1"
        ),
        "Incorrect DATE value: '2026-13-01'": "Incorrect DATE value: '…'",
        "Truncated incorrect DOUBLE value: '1x'": "Truncated incorrect DOUBLE value: '…'",
        "Variable 'sql_mode' can't be set to the value of 'NOPE'": (
            "Variable '…' can't be set to the value of '…'"
        ),
        "You have an error in your SQL syntax; check the manual near"
        " 'INSERT INTO t VALUES (' SECRET', 2)' at line 1": (
            "You have an error in your SQL syntax; check the manual near '…' at line 1"
        ),
        "Invalid utf8mb4 character string: 'F09F'": "Invalid utf8mb4 character string: '…'",
        "Cannot convert string 'x\\xF0' from binary to utf8mb4": (
            "Cannot convert string '…' from binary to utf8mb4"
        ),
    }
    for message, redacted in hidden.items():
        assert redact(message) == redacted
    # Object names are kept.
    for message in (
        "Table 'shop.orders' doesn't exist",
        "Unknown column 'nope' in 'field list'",
        "Access denied for user 'app'@'%' to database 'shop'",
        "Data too long for column 'name' at row 1",
        "Check constraint 'orders_chk_1' is violated.",
    ):
        assert redact(message) == message
    # A quoted string nothing classifies is hidden, as before, with its introducer.
    assert redact("Can't connect to MySQL server on 'db' (111)") == (
        "Can't connect to MySQL server on '…' (111)"
    )
    assert redact("x = _utf8mb4'secret' or N'more'") == "x = _utf8mb4'…' or N'…'"
    # An option file line split by a password: a variable is not an object.
    assert redact("mysqldump: [ERROR] unknown variable 'foo=secret'.") == (
        "mysqldump: [ERROR] unknown variable '…'."
    )
    # A user-raised message (SIGNAL) hides every quoted string.
    signal = pymysql.err.OperationalError(1644, "unknown user 'a@b.c' for table 'users'")
    assert str(database_error(signal, "loading")) == (
        "loading failed: [1644] unknown user '…' for table '…'"
    )
    # A trigger can SIGNAL with any number, a real one included: only the
    # server's own text for that number keeps names.
    for errno in (5001, 1062):
        custom = pymysql.err.OperationalError(errno, "unknown user 'x' for table 'users'")
        assert str(database_error(custom, "loading")) == (
            f"loading failed: [{errno}] unknown user '…' for table '…'"
        )
    duplicate = pymysql.err.IntegrityError(1062, "Duplicate entry 'a@b.c' for key 'users.email'")
    assert str(database_error(duplicate, "loading")) == (
        "loading failed: [1062] Duplicate entry '…' for key 'users.email'"
    )
    unknown = pymysql.err.OperationalError(1054, "Unknown column 'nope' in 'field list'")
    assert str(database_error(unknown, "loading")) == (
        "loading failed: [1054] Unknown column 'nope' in 'field list'"
    )
    genuine = tool_error("mysql", 1, "ERROR 1146 (42S02) at line 3: Table 'shop.t' doesn't exist")
    assert str(genuine) == (
        "mysql exited with status 1: ERROR 1146 (42S02) at line 3: Table 'shop.t' doesn't exist"
    )
    signalled = tool_error("mysql", 1, "ERROR 1062 (23000) at line 1: no user 'x' in table 'u'")
    assert str(signalled) == (
        "mysql exited with status 1: ERROR 1062 (23000) at line 1: no user '…' in table '…'"
    )
    for errno, message in (
        (1064, "You have an error in your SQL syntax; unknown user 'SECRET' near 'x' at line 1"),
        (1452, "Cannot add or update a child row: a foreign key constraint fails (table 'SECRET')"),
    ):
        forged = pymysql.err.OperationalError(errno, message)
        assert "SECRET" not in str(database_error(forged, "loading"))
    syntax = pymysql.err.ProgrammingError(
        1064,
        "You have an error in your SQL syntax; check the manual that corresponds to your MySQL"
        " server version for the right syntax to use near 'x' at line 1",
    )
    assert str(database_error(syntax, "loading")).endswith("near '…' at line 1")
    child = pymysql.err.IntegrityError(
        1452,
        "Cannot add or update a child row: a foreign key constraint fails"
        " (`shop`.`orders`, CONSTRAINT `fk` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`))",
    )
    assert "`fk`" in str(database_error(child, "loading"))
    continued = tool_error(
        "mysql",
        1,
        "ERROR 1054 (42S22) at line 1: Unknown column 'a' in 'field list'\nuser 'SECRET'\n",
    )
    assert "SECRET" not in str(continued)
    assert "'a'" not in str(continued)
    dumped = tool_error(
        "mysqldump",
        2,
        "mysqldump: Got error: 1054: unknown user 'SECRET' when using LOCK TABLES\n",
    )
    assert "SECRET" not in str(dumped)
    locked = tool_error(
        "mysqldump",
        2,
        "mysqldump: Got error: 1146: Table 'd.t' doesn't exist when using LOCK TABLES\n",
    )
    assert str(locked) == (
        "mysqldump exited with status 2:"
        " mysqldump: Got error: 1146: Table 'd.t' doesn't exist when using LOCK TABLES"
    )
    two = tool_error(
        "mysql",
        1,
        "ERROR 1146 (42S02) at line 1: Table 'shop.t' doesn't exist\n"
        "ERROR 1049 (42000): Unknown database 'shop'\n",
    )
    assert "'shop.t'" in str(two)
    assert "'shop'" in str(two)
    classed = tool_error("mysql", 1, "ERROR 5001 (45001) at line 1: Table 'x' doesn't exist")
    assert "'x'" not in str(classed)
    raised = tool_error("mysql", 1, "ERROR 1644 (45000) at line 1: no user 'a@b.c' in table 'u'")
    assert (
        str(raised)
        == "mysql exited with status 1: ERROR 1644 (45000) at line 1: no user '…' in table '…'"
    )


def test_runtime_errors_kept() -> None:
    pulled = tool_error("mysqldump", 125, 'Unable to find image "mysql:8.4.6" locally\n')
    assert str(pulled) == (
        'mysqldump exited with status 125: Unable to find image "mysql:8.4.6" locally'
    )
    assert "'mysqld'" in str(tool_error("mysqld", 126, "cannot run 'mysqld': permission denied"))
    assert str(tool_error("mysql", 1, "ERROR 1062 'secret'")) == (
        "mysql exited with status 1: ERROR 1062 '…'"
    )


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
    assert parse_url("mysql://u@db.internal:3307")[1] is None
    for bad in ("postgres://h/db", "mysql:///db"):
        with pytest.raises(EngineError, match="mysql url must look like"):
            parse_url(bad)
    for broken, password in (("se%0Acret", "se\ncret"), ("se%0Dcret", "se\rcret")):
        with pytest.raises(EngineError, match="url's password holds a line break") as caught:
            parse_url(f"mysql://u:{broken}@db.internal/shop")
        assert password not in str(caught.value)
        assert "cret" not in str(caught.value)


def test_kinds_and_placeholders() -> None:
    assert kind_of("INT") is ColumnKind.INTEGER
    assert kind_of("enum") is ColumnKind.OTHER
    assert (
        placeholder(T, Column(name=C("n"), type="varchar(5)", kind=ColumnKind.TEXT, nullable=False))
        == "''"
    )
    with pytest.raises(EngineError, match="no placeholder for g"):
        placeholder(T, Column(name=C("g"), type="point", kind=ColumnKind.OTHER, nullable=False))


def test_default_databases_keep_their_character_sets() -> None:
    script = (
        "USE `shop`;\n"
        "CREATE DATABASE /*!32312 IF NOT EXISTS*/ `shop_eu` /*!40100 CHARACTER SET latin1 */;\n"
        "CREATE DATABASE /*!32312 IF NOT EXISTS*/ `shop` /*!40100 CHARACTER SET utf8mb4 */;\n"
    )
    assert create_database(script, "shop") == (
        "CREATE DATABASE /*!32312 IF NOT EXISTS*/ `shop` /*!40100 CHARACTER SET utf8mb4 */"
    )
    assert create_database("", "shop") == "CREATE DATABASE IF NOT EXISTS `shop`"


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


def test_connections_name_a_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DD_NO_DATABASE", "mysql://u@db.internal")
    with (
        pytest.raises(EngineError, match="name a database in the mysql url"),
        MysqlEngine().connect({"url": {"env": "DD_NO_DATABASE"}}),
    ):
        pass


def test_engine(tmp_path: Path) -> None:
    engine = installed().engine("mysql")
    assert isinstance(engine, MysqlEngine)
    capabilities = engine.capabilities()
    assert (capabilities.nested, capabilities.hooks, capabilities.expressions) == (True, True, True)
    assert capabilities.version_line == 2  # 8.0 and 8.4 are separate lines
    (tmp_path / "database.json").write_text(
        DatabaseInfo(
            databases=("billing", "shop"), default="shop", server_version="8.4.13"
        ).model_dump_json(),
        "utf-8",
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


def test_servers_older_than_8_0_13_are_refused() -> None:
    require_view_usage(("8.4.13", 1))
    for row in (("5.7.44", 0), None):
        with pytest.raises(EngineError, match=r"needs MySQL 8\.0\.13 or later"):
            require_view_usage(row)
