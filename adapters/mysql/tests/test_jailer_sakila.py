# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Jailer and Datadouble choose the same rows from Sakila (milestone 4).

The Compose ``jailer`` service builds Jailer's data model of the mysql
service's Sakila and exports ``dev/jailer/sakila.jm`` as JSON into the
``jailer`` volume, mounted here at ``/jailer``. Datadouble runs the same
extraction model through its Jailer front end, with both strategies, and the
primary-key sets of every table must be identical.
"""

import json
import os
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter

from datadouble.catalog import Catalog, CatalogLock
from datadouble.planner import PlanOptions
from datadouble.registry import Registry, installed
from datadouble.run import Run, RunOptions, Stage
from datadouble.spec import Strategy, parse_spec
from datadouble.storage import filesystem_store
from datadouble_conformance.engine import runtime_for_tests
from datadouble_mysql import MysqlEngine
from datadouble_mysql.source import ExtractIndex
from test_mysql_conformance import URL_VARIABLE

pytestmark = pytest.mark.integration

JAILER = Path("/jailer")
_ROWS: TypeAdapter[list[dict[str, JsonValue]]] = TypeAdapter(list[dict[str, JsonValue]])
_UNESCAPE = {"\\": "\\", "t": "\t", "n": "\n", "r": "\r", "0": "\0"}

type KeySets = dict[str, set[tuple[str, ...]]]


def _field(raw: str) -> str:
    characters = iter(raw)
    return "".join(
        _UNESCAPE.get(next(characters, ""), "") if character == "\\" else character
        for character in characters
    )


def jailer_keys(catalog: Catalog) -> KeySets:
    """Jailer's exported rows, by table.

    The export does not name tables, and leaves NULL columns out; a row
    belongs to the table whose columns hold all of the row's, including its
    primary key, with the fewest to spare.
    """
    columns = {table: {str(c.name) for c in table.columns} for table in catalog.tables}
    found: KeySets = {}
    for row in _ROWS.validate_json((JAILER / "export.json").read_bytes()):
        candidates = [
            table
            for table, names in columns.items()
            if set(row) <= names and {str(k) for k in table.primary_key} <= set(row)
        ]
        table = min(candidates, key=lambda candidate: len(columns[candidate]) - len(row))
        key = tuple(str(row[str(column)]) for column in table.primary_key)
        found.setdefault(table.name.name, set()).add(key)
    return found


def datadouble_keys(catalog: Catalog, extract: Path) -> KeySets:
    """Datadouble's extracted rows, by table."""
    index = ExtractIndex.model_validate_json((extract / "index.json").read_bytes())
    found: KeySets = {}
    for extracted in index.tables:
        table = catalog.table(extracted.table)
        assert table is not None
        positions = [extracted.columns.index(column) for column in table.primary_key]
        keys = found.setdefault(extracted.table.name, set())
        for line in (extract / extracted.file).read_text("utf-8").splitlines():
            fields = line.split("\t")
            keys.add(tuple(_field(fields[i]) for i in positions))
    return {name: keys for name, keys in found.items() if keys}


def test_jailer_and_datadouble_choose_the_same_rows(tmp_path: Path) -> None:
    """Sakila's store and staff refer to each other, so the closure needs the keys strategy."""
    if not (JAILER / "export.json").is_file():
        pytest.skip("no Jailer export; run in the Compose default group")
    spec = parse_spec(
        "name: sakila\n"
        "source: {engine: mysql, connection: {url: {env: DATADOUBLE_TEST_MYSQL_URL}}}\n"
        f"subset: {{jailer: {{model: {JAILER / 'sakila.jm'},"
        f" datamodel: {JAILER / 'datamodel'}}}}}\n"
        f"runtime: {json.dumps(runtime_for_tests())}\n"
    )
    engine = MysqlEngine()
    with engine.connect(spec.source.connection) as session:
        catalog = session.catalog()
    registry = Registry(
        engines={"mysql": MysqlEngine},
        stores={"filesystem": filesystem_store},
        runtimes=installed().runtimes,
    )
    options = RunOptions(
        stages=(Stage.PLAN, Stage.EXTRACT),
        work=tmp_path / "work",
        plan=PlanOptions(strategy=Strategy.KEYS),
    )
    result = Run(spec, CatalogLock.of(catalog), registry, options).execute()
    ours = datadouble_keys(catalog, Path(result.work) / "extract" / "data")
    theirs = jailer_keys(catalog)
    differing = {
        name: (sorted(ours.get(name, set()) - keys)[:5], sorted(keys - ours.get(name, set()))[:5])
        for name, keys in {**ours, **theirs}.items()
        if ours.get(name, set()) != theirs.get(name, set())
    }
    assert not differing, differing
    assert sum(len(keys) for keys in theirs.values()) == 8501
    assert os.environ.get(URL_VARIABLE)
