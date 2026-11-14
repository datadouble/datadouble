# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The Elasticsearch adapter's parts that need no server."""

import datetime as dt
import gzip
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

import pytest

from datadouble.catalog import ColumnKind
from datadouble.engine import EngineError
from datadouble.names import ColumnName, ColumnRef, TableName
from datadouble.planner import PlannedRule
from datadouble.programs import Completed
from datadouble.runtime import Container, Server
from datadouble.settings import SettingsError, parse_settings
from datadouble.spec import FakerKind, FakerRule, NullOptions, NullRule, Rule
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
from datadouble_elasticsearch import ElasticConnection, ElasticEngine, engine
from datadouble_elasticsearch.client import Client, as_list, document, items, text
from datadouble_elasticsearch.documents import (
    covered,
    entry,
    paths,
    read_entries,
    rewrite,
    values,
    write_entries,
)
from datadouble_elasticsearch.indices import operations, portable
from datadouble_elasticsearch.search import parse, query
from datadouble_elasticsearch.server import (
    ServerSettings,
    Tools,
    default_image,
    ready_command,
    start_command,
)
from datadouble_elasticsearch.source import is_open, keys, mapped, table_columns
from datadouble_elasticsearch.working import changer, serve_command

AS_OF = dt.datetime(2026, 1, 2, tzinfo=dt.UTC)
CITY = ColumnName("address.city")


def test_paths_reach_nested_dotted_and_array_fields() -> None:
    source = {
        "address": {"city": "Riverton"},
        "address.postal_code": "10001",
        "actors": [{"id": 1}, {"id": 2, "tags": ["a", "b"]}],
        "empty": {},
        "gone": None,
    }
    assert list(values(source, ["address", "city"])) == ["Riverton"]
    assert list(values(source, ["address", "postal_code"])) == ["10001"]
    assert list(values(source, ["actors", "id"])) == [1, 2]
    assert list(values(source, ["actors", "tags"])) == ["a", "b"]
    assert list(values(source, ["gone"])) == []
    assert list(values("text", ["address"])) == []
    assert sorted(set(paths(source))) == [
        "actors.id",
        "actors.tags",
        "address.city",
        "address.postal_code",
        "empty",
        "gone",
    ]
    assert covered("client.ip", {"client"})
    assert not covered("client.ip", {"client.agent"})


def test_rewrite_replaces_each_field_on_a_path() -> None:
    source = {"a": {"b": "x"}, "a.c": "y", "list": [{"b": "x"}, "plain"]}
    assert rewrite(source, ["a", "b"], lambda value: str(value).upper()) == {
        "a": {"b": "X"},
        "a.c": "y",
        "list": [{"b": "x"}, "plain"],
    }
    assert rewrite(source, ["list", "b"], lambda _: None) == {
        "a": {"b": "x"},
        "a.c": "y",
        "list": [{"b": None}, "plain"],
    }
    assert rewrite(source, ["a", "c"], lambda _: "z") == {**source, "a.c": "z"}


def test_dump_lines_round_trip(tmp_path: Path) -> None:
    hits: list[dict[str, object]] = [
        {"_id": "1", "_source": {"a": 1}, "_routing": "7"},
        {"_id": "2", "_source": None},
    ]
    file = tmp_path / "00000.ndjson.gz"
    assert write_entries(file, (entry(hit) for hit in hits)) == 2
    lines = list(read_entries(file))
    assert lines == [
        {"_id": "1", "_source": {"a": 1}, "_routing": "7"},
        {"_id": "2", "_source": {}},
    ]
    assert operations("films", lines[0]) == [
        {"index": {"_index": "films", "_id": "1", "routing": "7"}},
        {"a": 1},
    ]
    with gzip.open(file, "wt") as out:
        out.write('{"no": "id"}\n')
    with pytest.raises(EngineError, match="line 1 is not a dump line"):
        list(read_entries(file))


def test_conditions_become_query_dsl() -> None:
    fields = {ColumnName("body"): "text", CITY: "keyword"}
    ids = ValuesIn((ColumnName("_id"),), frozenset({("2",), ("1",)}))
    assert query(AllRows(), AS_OF, fields) == {"match_all": {}}
    assert query(NoRows(), AS_OF, fields) == {"match_none": {}}
    assert query(ids, AS_OF, fields) == {"ids": {"values": ["1", "2"]}}
    assert query(ValuesIn((CITY,), frozenset()), AS_OF, fields) == {"match_none": {}}
    pair = ValuesIn((CITY, ColumnName("n")), frozenset({("x", True)}))
    assert query(pair, AS_OF, fields) == {
        "bool": {"filter": [{"terms": {CITY: ["x"]}}, {"terms": {"n": ["true"]}}]}
    }
    many = ValuesIn((CITY,), frozenset((str(n),) for n in range(10001)))
    assert str(query(many, AS_OF, fields)).count("'terms'") == 2
    seed = Predicate('{"range": {"at": {"lte": ":as_of"}}, "x": [":as_of"]}')
    assert query(AnyOf((seed, AnyNull((CITY,)))), AS_OF, fields) == {
        "bool": {
            "should": [
                {"range": {"at": {"lte": AS_OF.isoformat()}}, "x": [AS_OF.isoformat()]},
                {"bool": {"should": [{"bool": {"must_not": {"exists": {"field": CITY}}}}]}},
            ]
        }
    }
    assert query(AllOf((AllRows(),)), AS_OF, fields) == {"bool": {"filter": [{"match_all": {}}]}}
    with pytest.raises(EngineError, match="text field, which no query matches exactly"):
        query(ValuesIn((ColumnName("body"),), frozenset({("x",)})), AS_OF, fields)
    with pytest.raises(EngineError, match="keys strategy"):
        query(SelectedBy((CITY,), TableName.of("", "x"), (CITY,)), AS_OF, fields)
    with pytest.raises(EngineError, match="not valid JSON"):
        parse("{", AS_OF, "a seed")
    with pytest.raises(EngineError, match="must be a JSON object"):
        parse("[]", AS_OF, "a seed")


def test_mappings_become_columns() -> None:
    properties: dict[str, object] = {
        "name": {"type": "text", "fields": {"raw": {"type": "keyword"}}},
        "nick": {"type": "alias", "path": "name"},
        "address": {"properties": {"city": {"type": "keyword"}}},
        "tags": {"type": "nested", "properties": {"id": {"type": "long"}}},
        "blob": {"type": "object", "enabled": False},
        "shape": {"type": "geo_point"},
        "meta": {"dynamic": "false", "properties": {"a": {"type": "keyword"}}},
    }
    mappings = {"dynamic": "strict", "properties": properties}
    assert dict(mapped(properties)) == {
        "name": "text",
        "address.city": "keyword",
        "tags.id": "long",
        "blob": "object",
        "shape": "geo_point",
        "meta.a": "keyword",
    }
    assert is_open(mappings)
    assert not is_open({"properties": {"a": {"type": "keyword"}}})
    sampled: list[dict[str, object]] = [
        {"_source": {"meta": {"a": "x", "b": "y"}, "blob": {"anything": 1}}}
    ]
    columns = table_columns("people", mappings, sampled)
    assert [
        (c.name, c.type, str(c.kind)) for c in columns if c.name in {"_id", "meta.b", "shape"}
    ] == [
        ("_id", "_id", "text"),
        ("shape", "geo_point", "other"),
        ("meta.b", "unmapped", "other"),
    ]
    for hidden in ({"enabled": False}, {"excludes": ["secret"]}):
        with pytest.raises(EngineError, match="does not keep whole documents"):
            table_columns("people", {"_source": hidden}, [])


def test_keys_are_text() -> None:
    hit = {"_id": "7", "_source": {"tags": [1, True], "address.city": "x"}}
    columns = (ColumnName("_id"), ColumnName("tags"), CITY)
    assert sorted(keys(hit, columns)) == [("7", "1", "x"), ("7", "true", "x")]


def test_portable_settings() -> None:
    settings = {
        "index.number_of_shards": "2",
        "index.number_of_replicas": "1",
        "index.uuid": "abc",
        "index.routing.allocation.include._tier_preference": "data_content",
        "index.lifecycle.name": "keep",
        "index.default_pipeline": "enrich",
        "index.analysis.analyzer.folding.tokenizer": "standard",
        "index.blocks.write": "true",
        "index.hidden": None,
    }
    assert portable(settings) == {
        "index.analysis.analyzer.folding.tokenizer": "standard",
        "index.number_of_shards": "2",
        "index.number_of_replicas": "0",
    }


def test_settings_and_commands() -> None:
    connection = parse_settings(ElasticConnection, {"url": {"env": "X"}}, "c")
    assert (connection.indices, connection.sample) == (("*",), 1000)
    assert parse_settings(ServerSettings, {"heap": "2g"}, "s").heap == "2g"
    with pytest.raises(SettingsError, match="heap"):
        parse_settings(ServerSettings, {"heap": "lots"}, "s")
    assert default_image("9.5.5") == "elasticsearch:9.5.5"
    plain = start_command("/d/data", None, "512m")
    assert "-Epath.data=/d/data" in plain
    assert "path.repo" not in plain
    assert "until" not in plain
    command, marker = serve_command("/m", "512m")
    assert "-Epath.repo=/m/repo" in command
    assert "_snapshot/datadouble/golden/_restore" in command
    assert marker == "/m/restored"
    assert ready_command(marker)[2].startswith("test -f /m/restored && ")
    assert "DATADOUBLE_PASSWORD" not in ready_command()[2].replace('"$DATADOUBLE_PASSWORD"', "")
    elastic = ElasticEngine()
    assert (elastic.name, elastic.owner) == ("elasticsearch", "1000:0")
    assert elastic.capabilities().nested is False
    assert engine().name == "elasticsearch"


def test_replies() -> None:
    assert [text(flag) for flag in (True, False)] == ["true", "false"]
    assert text(4) == "4"
    assert as_list("x") is None
    with pytest.raises(EngineError, match="JSON object for a reply"):
        document([], "a reply")
    with pytest.raises(EngineError, match="JSON array for hits"):
        items({}, "hits")
    for url in ("ftp://host", "http://", "postgres://h:1/x"):
        with pytest.raises(EngineError, match="an Elasticsearch URL is"):
            Client.from_url(url)
    Client.from_url("https://user:p%40ss@search.example.com/prefix/", "key").close()
    Client.from_url("http://search.example.com:9200").close()


@dataclass
class _Failing:
    """A container runtime whose containers fail."""

    def run(self, container: Container) -> Completed:
        return Completed(1, "", f"no {container.image}")

    def serve(self, container: Container) -> AbstractContextManager[Server]:
        raise NotImplementedError

    def release(self, owner: Path) -> None:
        raise NotImplementedError


def test_tools_report_failures(tmp_path: Path) -> None:
    with pytest.raises(EngineError, match="cannot read the Elasticsearch version: 'no x:1'"):
        Tools(_Failing(), "x:1").version(tmp_path)


def test_null_and_value_changers() -> None:
    column = ColumnRef(table=TableName.of("", "t"), column=ColumnName("n"))

    def rule(chosen: Rule) -> PlannedRule:
        return PlannedRule(column=column, kind=ColumnKind.TEXT, type="keyword", rule=chosen)

    assert changer(rule(NullRule(null=NullOptions())), {})("x") is None
    swap = changer(rule(FakerRule(faker=FakerKind.NAME)), {"1": "9", "a": "b"})
    assert swap([1, "a", True, "z"]) == [9, "b", True, "z"]
    assert swap("a") == "b"
