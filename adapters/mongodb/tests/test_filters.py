# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Filters, field sampling and the server plan, without a server."""

import datetime as dt
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

import pytest
from bson import Decimal128, Int64, ObjectId

from datadouble.catalog import ColumnKind
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
from datadouble_mongodb import MongoEngine
from datadouble_mongodb.filters import as_document, as_list, keys, lookup, parse, query
from datadouble_mongodb.server import MongoServer, Tools, default_image, upsert_user
from datadouble_mongodb.source import sampled_columns, schema_columns
from datadouble_mongodb.working import DatabaseInfo

AS_OF = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
C = ColumnName


def test_queries() -> None:
    assert query(AllRows(), AS_OF) == {}
    assert query(NoRows(), AS_OF) == {"$expr": False}
    assert query(ValuesIn((C("_id"),), frozenset()), AS_OF) == {"$expr": False}
    assert query(ValuesIn((C("_id"),), frozenset({(1,)})), AS_OF) == {"_id": {"$in": [1]}}
    assert query(ValuesIn((C("a"), C("b")), frozenset({(1, 2)})), AS_OF) == {
        "$or": [{"a": 1, "b": 2}]
    }
    assert query(AnyNull((C("a"),)), AS_OF) == {"$or": [{"a": None}]}
    seed = Predicate('{"created": {"$lte": ":as_of"}, "tags": ["x", ":as_of"]}')
    assert query(AnyOf((seed, AllOf((AllRows(),)))), AS_OF) == {
        "$or": [{"created": {"$lte": AS_OF}, "tags": ["x", AS_OF]}, {"$and": [{}]}]
    }
    with pytest.raises(EngineError, match="cannot evaluate nested selections"):
        query(SelectedBy((C("a"),), TableName.of("db", "t"), (C("b"),)), AS_OF)


def test_parsing() -> None:
    assert parse('{"_id": {"$oid": "aa0000000000000000000001"}}', AS_OF, "x") == {
        "_id": ObjectId("aa0000000000000000000001")
    }
    with pytest.raises(EngineError, match="x is not valid MongoDB Extended JSON"):
        parse("{nope", AS_OF, "x")
    with pytest.raises(EngineError, match="x must be a JSON object"):
        parse("[1]", AS_OF, "x")
    assert as_document({1: "x"}) is None
    assert as_document([1]) is None
    assert as_list((1,)) is None


def test_lookups_and_keys() -> None:
    document = {"a": {"b": 1}, "refs": [1, 2], "x": None, "odd": [{"y": 1}]}
    assert lookup(document, "a.b") == 1
    assert lookup(document, "a.b.c") is None
    assert sorted(keys(document, ("refs",))) == [(1,), (2,)]
    assert list(keys(document, ("a.b", "refs"))) == [(1, 1), (1, 2)]
    assert list(keys(document, ("x",))) == [(None,)]
    assert list(keys(document, ("odd",))) == []


def test_sampled_fields() -> None:
    columns = sampled_columns(
        [
            {
                "_id": 1,
                "name": "a",
                "address": {"city": "x"},
                "n": Int64(3),
                "d": Decimal128("1.5"),
            },
            {"_id": 2, "name": None, "mixed": 1, "when": AS_OF, "tags": ["a"], "empty": {}},
            {"_id": 3, "mixed": "two", "blob": b"x", "flag": True, "odd": complex(1, 1)},
        ]
    )
    found = {str(c.name): (c.kind, c.type, c.nullable) for c in columns}
    assert columns[0].name == "_id"
    assert found["_id"] == (ColumnKind.INTEGER, "int", False)
    assert found["address.city"] == (ColumnKind.TEXT, "string", True)
    assert found["name"] == (ColumnKind.TEXT, "string", True)
    assert found["mixed"] == (ColumnKind.OTHER, "int|string", True)
    assert found["n"][0] is ColumnKind.INTEGER
    assert found["d"][0] is ColumnKind.DECIMAL
    assert found["when"][0] is ColumnKind.TIMESTAMP
    assert found["tags"][0] is ColumnKind.ARRAY
    assert found["empty"][0] is ColumnKind.JSON
    assert found["blob"][0] is ColumnKind.BINARY
    assert found["flag"][0] is ColumnKind.BOOLEAN
    assert found["odd"][:2] == (ColumnKind.OTHER, "complex")


def test_validator_fields() -> None:
    assert schema_columns({"bsonType": "object"}) is None
    columns = schema_columns(
        {
            "required": ["email"],
            "properties": {
                "email": {"bsonType": "string"},
                "_id": {"bsonType": "objectId"},
                "age": {"bsonType": ["int", "null"]},
                "loose": True,
            },
        }
    )
    assert columns is not None
    assert [(c.name, c.type, c.kind, c.nullable) for c in columns] == [
        ("_id", "objectId", ColumnKind.OTHER, False),
        ("age", "int|null", ColumnKind.OTHER, True),
        ("email", "string", ColumnKind.TEXT, False),
        ("loose", "object", ColumnKind.JSON, True),
    ]


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
    return Tools(ScriptedRuntime(Completed(status, stdout, stderr)), "mongo:9.0")


def test_tools(tmp_path: Path) -> None:
    assert tools(stdout="db version v9.0.8\n").version(tmp_path) == "9.0.8"
    with pytest.raises(EngineError, match="cannot read the MongoDB version"):
        tools(stdout="db version vX").version(tmp_path)
    with pytest.raises(EngineError, match="mongod failed: fatal: no"):
        tools(stderr="fatal: no\n", status=5).version(tmp_path)
    with pytest.raises(EngineError, match="mongod failed: status 6"):
        tools(status=6).version(tmp_path)
    with pytest.raises(EngineError, match="not running"):
        _ = MongoServer(tools(), tmp_path / "db", tmp_path).server
    assert default_image("9.0.8") == "mongo:9.0.8"
    assert "updateUser('x'" in upsert_user("x")
    assert "createUser({user: 'x'" in upsert_user("x")


def test_engine(tmp_path: Path) -> None:
    engine = installed().engine("mongodb")
    assert isinstance(engine, MongoEngine)
    capabilities = engine.capabilities()
    assert (capabilities.nested, capabilities.hooks, capabilities.expressions) == (
        False,
        False,
        True,
    )
    (tmp_path / "database.json").write_text(
        DatabaseInfo(database="cinema", server_version="9.0.8").model_dump_json(), "utf-8"
    )
    plan = engine.server(tmp_path, None)
    assert (plan.image, plan.port, plan.database, plan.user) == (
        "mongo:9.0.8",
        27017,
        "cinema",
        "datadouble",
    )
    assert engine.server(tmp_path, "mongo:9.0.8").image == "mongo:9.0.8"
