# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Two PostgreSQL servers as two sources of one spec (Compose's second server)."""

import json
import os
import uuid
from collections.abc import Generator
from contextlib import ExitStack
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from test_postgres import registry, working_databases

from datadouble.catalog import CatalogLock
from datadouble.images import unpack
from datadouble.names import ColumnName, ColumnRef, TableName
from datadouble.planner import PlanError, merge
from datadouble.run import Run, RunOptions, Stage, catalogs
from datadouble.spec import Spec, parse_spec
from datadouble_conformance.engine import runtime_for_tests
from datadouble_postgres import PostgresEngine
from datadouble_postgres.render import trusted
from test_conformance import DSN_VARIABLE, by_address

pytestmark = pytest.mark.integration

SECOND_VARIABLE = "DATADOUBLE_TEST_POSTGRES_SECOND_DSN"

if SECOND_VARIABLE in os.environ:
    os.environ[SECOND_VARIABLE] = by_address(os.environ[SECOND_VARIABLE])

# The application's database on the first server, the warehouse's on the
# second; `PREFIX_same` exists on both, for the collision.
FIRST = {
    "app": """
CREATE TABLE public.customers (id integer PRIMARY KEY, email text NOT NULL);
INSERT INTO public.customers VALUES (1, 'ann@shop.test'), (2, 'bob@shop.test'),
    (3, 'cy@shop.test');
""",
    "same": "CREATE TABLE public.items (id integer PRIMARY KEY);",
}
SECOND = {
    "warehouse": """
CREATE TABLE public.facts (id integer PRIMARY KEY, email text NOT NULL, amount integer);
INSERT INTO public.facts VALUES (10, 'ann@shop.test', 5), (11, 'bob@shop.test', 7),
    (12, 'dee@shop.test', 9);
""",
    "same": "CREATE TABLE public.items (id integer PRIMARY KEY);",
}

SPEC = """
name: two
sources:
  app:
    engine: postgres
    connection:
      dsn:
        env: FIRST_VARIABLE
      databases: [PREFIX_app]
  warehouse:
    engine: postgres
    connection:
      dsn:
        env: SECOND_VARIABLE
      databases: [PREFIX_warehouse]
subset:
  seeds:
    customers: "id <= 2"
    facts: "amount < 9"
anonymize:
  customers:
    email:
      hash: {}
  facts:
    email:
      hash: {}
images:
  everything:
    include: ["*"]
  warehouse:
    include: ["PREFIX_warehouse.*.*"]
store:
  - filesystem:
      path: STORE
"""


@pytest.fixture
def prefix() -> Generator[str]:
    """Databases on both servers, named after a fresh prefix."""
    chosen = f"dd_two_{uuid.uuid4().hex[:8]}"
    servers = ((os.environ[DSN_VARIABLE], FIRST), (os.environ[SECOND_VARIABLE], SECOND))
    try:
        for admin, schemas in servers:
            for suffix, schema in schemas.items():
                name = f"{chosen}_{suffix}"
                with psycopg.connect(admin, autocommit=True) as connection:
                    connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
                conninfo = make_conninfo(admin, dbname=name)
                with psycopg.connect(conninfo, autocommit=True) as connection:
                    connection.execute(trusted(schema))
        yield chosen
    finally:
        for admin, schemas in servers:
            with psycopg.connect(admin, autocommit=True) as connection:
                for suffix in schemas:
                    dropped = sql.Identifier(f"{chosen}_{suffix}")
                    connection.execute(
                        sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(dropped)
                    )


def two_spec(prefix: str, tmp_path: Path, spec: str = SPEC) -> Spec:
    return parse_spec(
        spec.replace("PREFIX", prefix)
        .replace("FIRST_VARIABLE", DSN_VARIABLE)
        .replace("SECOND_VARIABLE", SECOND_VARIABLE)
        .replace("STORE", str(tmp_path / "store"))
        + f"\nruntime: {json.dumps(runtime_for_tests())}\n"
    )


def lock_of(spec: Spec) -> CatalogLock:
    engine = PostgresEngine()
    with ExitStack() as stack:
        sessions = {
            name: stack.enter_context(engine.connect(source.connection))
            for name, source in spec.sources.items()
        }
        return CatalogLock.of(merge(catalogs(sessions)))


def test_two_servers_build_one_image_and_a_slice(prefix: str, tmp_path: Path) -> None:
    spec = two_spec(prefix, tmp_path)
    options = RunOptions(stages=tuple(Stage), work=tmp_path / "work")
    result = Run(spec, lock_of(spec), registry(), options).execute()
    assert not result.failed(), result.verification
    customers = TableName.of(f"{prefix}_app", "public", "customers")
    facts = TableName.of(f"{prefix}_warehouse", "public", "facts")
    images = {str(summary.identity.group): summary for summary in result.images}
    assert set(images) == {"everything", "warehouse"}
    working = working_databases(tmp_path / "check")
    archive = images["everything"].archives[0]
    image, manifest = unpack(tmp_path / "store" / archive.key, tmp_path / "unpacked")
    built = json.loads(manifest)
    assert sorted(built["sources"]) == ["app", "warehouse"]
    for read in built["sources"].values():
        assert set(read["snapshots"])
        assert read["snapshot_at"]
    with working.restore(image, archive.format, tmp_path / "restored") as restored:
        assert restored.counts([customers, facts]) == {customers: 2, facts: 2}
        # One key per run: equal values get equal replacements in both sources.
        names = {
            table: set(restored.text_values(ColumnRef(table=table, column=ColumnName("email"))))
            for table in (customers, facts)
        }
        assert names[customers] == names[facts]
        assert "ann@shop.test" not in names[customers]
    sliced = images["warehouse"].archives[0]
    image, _ = unpack(tmp_path / "store" / sliced.key, tmp_path / "unpacked-slice")
    with working.restore(image, sliced.format, tmp_path / "restored-slice") as restored:
        assert restored.counts([facts]) == {facts: 2}


def test_a_database_on_both_servers_fails_the_plan(prefix: str, tmp_path: Path) -> None:
    spec = two_spec(
        prefix,
        tmp_path,
        SPEC.replace("[PREFIX_app]", "[PREFIX_app, PREFIX_same]").replace(
            "[PREFIX_warehouse]", "[PREFIX_warehouse, PREFIX_same]"
        ),
    )
    options = RunOptions(stages=(Stage.PLAN,), work=tmp_path / "work")
    with pytest.raises(PlanError) as caught:
        Run(spec, None, registry(), options).execute()
    assert (
        f"{prefix}_same is in sources app and warehouse; one working server holds every"
        " source, so database names must not repeat across sources"
    ) in caught.value.problems
