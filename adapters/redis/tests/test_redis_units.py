# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The Redis adapter's parts that need no server."""

import io
from pathlib import Path

import pytest
from redis.exceptions import ResponseError

from datadouble.engine import EngineError
from datadouble.names import ColumnName, TableName
from datadouble.settings import parse_settings
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
from datadouble_redis import RedisConnection, RedisEngine
from datadouble_redis.driver import failure, items, number, raw, text, texts
from datadouble_redis.keys import (
    KEY,
    Literals,
    Pattern,
    glob,
    matches,
    needed,
    place,
    projected,
)
from datadouble_redis.server import default_image, start_command
from datadouble_redis.source import database_of, namespace, read_records, write_record
from datadouble_redis.working import fill, server_databases

USER = ColumnName("user_id")


def test_keys_are_placed_in_patterns() -> None:
    session = Pattern.parse("session:{}")
    assert place("session:abc:Q9", [session], ":").values == ("abc:Q9",)
    inferred = place("user:42:sessions", [session], ":")
    assert (inferred.pattern, inferred.values, inferred.inferred) == (
        "user:{}:sessions",
        ("42",),
        True,
    )
    assert inferred.key(":") == "42"
    uuid = "0f8fad5b-d9cb-469f-a165-70867728950e"
    assert place(f"cart:{uuid}", [], ":").pattern == "cart:{}"
    assert place("blob:deadbeefcafe", [], ":").pattern == "blob:{}"
    assert place("user:a.b@example.com", [], ":").pattern == "user:{}"
    assert place("config", [], ":").pattern == "config"
    assert place("config", [], ":").key(":") == ""
    with pytest.raises(EngineError, match=r"no \{\} placeholder"):
        Pattern.parse("session:*")


def test_too_many_literals_fail_without_naming_them() -> None:
    literals = Literals(":")
    for name in ("alice", "bob", "carol"):
        literals.add(place(f"user:{name}", [], ":"))
    literals.add(place("session:x", [Pattern.parse("session:{}")], ":"))
    literals.check()
    for number_ in range(20):
        literals.add(place(f"user:name{chr(97 + number_)}x", [], ":"))
    with pytest.raises(EngineError, match="segment 2 after 'user'") as raised:
        literals.check()
    assert "alice" not in str(raised.value)
    first = Literals(":")
    for number_ in range(70):
        first.add(place(f"kind{chr(97 + number_ % 26)}{chr(97 + number_ // 26)}", [], ":"))
    with pytest.raises(EngineError, match="the first segment"):
        first.check()


def test_globs_escape_glob_characters() -> None:
    assert glob("user:{}:sessions") == "user:*:sessions"
    assert glob("odd[1]*?:{}") == r"odd\[1\]\*\?:*"


def test_conditions_are_evaluated_against_keys() -> None:
    row = {KEY: ["42"], USER: ["7"], ColumnName("members"): ["a", "b"]}
    chosen = ValuesIn((USER,), frozenset({("7",)}))
    assert matches(AllRows(), row)
    assert not matches(NoRows(), row)
    assert matches(Predicate("4*"), row)
    assert not matches(Predicate("5*"), {})
    assert matches(chosen, row)
    assert matches(ValuesIn((ColumnName("members"),), frozenset({("b",)})), row)
    assert matches(AnyNull((ColumnName("missing"),)), row)
    assert not matches(AnyNull((USER,)), row)
    assert matches(AnyOf((NoRows(), chosen)), row)
    assert not matches(AllOf((AllRows(), NoRows())), row)
    with pytest.raises(EngineError, match="strategy B"):
        matches(SelectedBy((USER,), TableName.of("db0", "user:{}"), (KEY,)), row)
    assert needed(AnyOf((Predicate("1*"), AllOf((chosen, AnyNull((USER,))))))) == {KEY, USER}
    assert needed(AllRows()) == set()
    assert projected([row, {KEY: ["43"]}], (KEY, USER)) == {("42", "7"), ("43", None)}


def test_replies_and_errors() -> None:
    assert text(b"caf\xc3\xa9") == "café"
    assert raw(text(b"\xff")) == b"\xff"
    assert text(7) == "7"
    assert number(b"12") == 12
    assert number(3) == 3
    assert items((b"a",)) == [b"a"]
    assert texts([b"a", b"b"]) == ["a", "b"]
    with pytest.raises(EngineError, match="array reply"):
        items(b"a")
    coded = failure(ResponseError("WRONGTYPE alice@example.com is a hash"), "reading")
    assert str(coded) == "reading failed: ResponseError (WRONGTYPE)"
    plain = failure(ResponseError("user script: alice@example.com"), "anonymizing")
    assert str(plain) == "anonymizing failed: ResponseError"


def test_extract_records_round_trip(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    write_record(buffer, b"user:1", b"\x00dump")
    write_record(buffer, b"user:2", b"")
    path = tmp_path / "00000.keys"
    path.write_bytes(buffer.getvalue())
    assert list(read_records(path)) == [(b"user:1", b"\x00dump"), (b"user:2", b"")]
    path.write_bytes(buffer.getvalue()[:-3])
    with pytest.raises(EngineError, match="truncated"):
        list(read_records(path))
    path.write_bytes(buffer.getvalue() + b"\x00")
    with pytest.raises(EngineError, match="truncated"):
        list(read_records(path))


def test_names_settings_and_servers() -> None:
    assert namespace(3) == "db3"
    assert database_of(TableName.of("db12", "user:{}")) == 12
    assert fill("user:{}:sessions", "42", ":") == "user:42:sessions"
    assert fill("pair:{}:{}", "1:2:3", ":") == "pair:1:2:3"
    with pytest.raises(EngineError, match="needs 2 values"):
        fill("pair:{}:{}", "1", ":")
    connection = parse_settings(
        RedisConnection,
        {"url": {"env": "X"}, "databases": [2, 0, 2], "patterns": ["s:{}"]},
        "redis connection",
    )
    assert connection.layout().databases == (0, 2)
    with pytest.raises(EngineError, match="placeholder"):
        parse_settings(RedisConnection, {"url": {"env": "X"}, "patterns": ["s:*"]}, "c").layout()
    assert server_databases(connection.layout()) == 16
    assert default_image("8.10.3") == "redis:8.10.3"
    assert '--save ""' in start_command("/d", 16, persist=False)
    assert "--save" not in start_command("/d", 16, persist=True)
    engine = RedisEngine()
    assert (engine.name, engine.owner) == ("redis", "999:999")
    assert engine.capabilities().nested is False
