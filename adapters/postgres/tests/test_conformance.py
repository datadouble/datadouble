# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The engine conformance suite against Pagila, in the Compose postgres service."""

import json
import os
import socket
import uuid
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import JsonValue

from datadouble.catalog import CatalogLock, RequirementKind
from datadouble.engine import Tooling
from datadouble.names import ColumnName, ColumnRef, TableName
from datadouble.registry import Registry, installed
from datadouble.run import Run, RunOptions, Stage
from datadouble.spec import parse_spec
from datadouble.storage import filesystem_store
from datadouble_conformance.engine import (
    EngineConformance,
    EngineHarness,
    runtime_for_tests,
)
from datadouble_postgres import PostgresEngine
from datadouble_postgres.cluster import Cluster, Tools

pytestmark = pytest.mark.integration

DSN_VARIABLE = "DATADOUBLE_TEST_POSTGRES_DSN"


def by_address(conninfo: str) -> str:
    """``conninfo`` with its host name replaced by the address it resolves to.

    ``pg_dump`` runs in containers on the Docker-in-Docker daemon, where the
    Compose service names do not resolve.
    """
    params = conninfo_to_dict(conninfo)
    params["host"] = socket.gethostbyname(str(params["host"]))
    return make_conninfo(**{k: str(v) for k, v in params.items()})


if DSN_VARIABLE in os.environ:
    os.environ[DSN_VARIABLE] = by_address(os.environ[DSN_VARIABLE])

# Pagila's film_embedding table needs pgvector, which the plain image lacks.
PGVECTOR = (
    "pgvector/pgvector:0.8.7-pg18-trixie"
    "@sha256:ab49524d18df7200b74ac9a475e1db6297fc1eb57da4c214c63d10ecc4b754fa"
)

# Customers 1-40 with their rentals, those rentals' payments, referrals and
# notes, and everything they refer to. Pagila declares payment's references
# only on some partitions, so they are given here as virtual references. The
# references are arranged so the nested strategy applies: payments come
# through rentals, and a payment whose customer is not chosen is left out
# rather than pulling one in.
SAMPLE_SPEC = """
name: pagila
source:
  engine: postgres
  connection:
    dsn:
      env: DATADOUBLE_TEST_POSTGRES_DSN
  image: PGVECTOR
exclude: ["dba.*"]
subset:
  unselected: none
  seeds:
    customer: "customer_id <= 40 AND create_date <= :as_of"
  follow:
    down:
      - customer -> rental
      - rental -> payment
      - customer -> customer_referral
      - customer -> customer_note
  edges:
    payment.customer_id -> customer:
      on_missing: drop
    customer_referral.referred_by -> customer_referral:
      on_missing: null
  virtual_fks:
    payment.rental_id -> rental: {}
    payment.customer_id -> customer: {}
    payment.staff_id -> staff: {}
    customer_note.subject_id -> customer:
      when: "subject_type = 'customer'"
anonymize:
  customer:
    first_name:
      faker: first_name
    last_name:
      faker: last_name
    email:
      faker: email
      unique: true
  staff:
    first_name:
      faker: first_name
    last_name:
      faker: last_name
    email:
      faker: email
    username:
      hash:
        length: 12
    password:
      hash:
        length: 40
    picture:
      null: {}
  address:
    address:
      faker: street_address
    address2:
      null: {}
    phone:
      faker: phone_number
    postal_code:
      faker: postcode
  customer_note:
    body:
      expression: "'note removed'"
      where: "subject_type = 'customer'"
  customer_referral:
    note:
      null: {}
verify:
  assertions:
    - name: active is a flag
      sql: "SELECT 1 FROM customer WHERE active NOT IN (0, 1)"
store:
  - filesystem:
      path: STORE
"""


@dataclass
class PostgresScratch:
    """A throwaway database on the test server, holding ``items(id)``."""

    admin: str
    database: str
    variable: str

    @property
    def connection(self) -> Mapping[str, JsonValue]:
        """Settings pointing at the scratch database through an environment variable."""
        return {"dsn": {"env": self.variable}}

    @property
    def items(self) -> TableName:
        """The scratch table."""
        return TableName.of(self.database, "public", "items")

    def execute(self, statement: sql.Composed | sql.SQL) -> None:
        """Run a statement in the scratch database."""
        with psycopg.connect(os.environ[self.variable], autocommit=True) as connection:
            connection.execute(statement)

    def add_table(self, name: str) -> None:
        """Create another table."""
        self.execute(
            sql.SQL("CREATE TABLE public.{} (id integer PRIMARY KEY)").format(sql.Identifier(name))
        )

    def add_column(self, column: str) -> None:
        """Add a column to ``items``."""
        self.execute(
            sql.SQL("ALTER TABLE public.items ADD COLUMN {} text").format(sql.Identifier(column))
        )

    def insert(self, item_id: int) -> None:
        """Insert and commit a row."""
        self.execute(sql.SQL("INSERT INTO public.items (id) VALUES ({})").format(item_id))


@contextmanager
def scratch() -> Generator[PostgresScratch]:
    """Create a scratch database, and drop it afterwards."""
    admin = os.environ[DSN_VARIABLE]
    database = f"dd_scratch_{uuid.uuid4().hex[:12]}"
    variable = f"DD_{database.upper()}"
    with psycopg.connect(admin, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    params = conninfo_to_dict(admin)
    params["dbname"] = database
    os.environ[variable] = make_conninfo(**{k: str(v) for k, v in params.items()})
    source = PostgresScratch(admin, database, variable)
    try:
        source.execute(sql.SQL("CREATE TABLE public.items (id integer PRIMARY KEY)"))
        yield source
    finally:
        os.environ.pop(variable, None)
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database))
            )


@pytest.fixture
def harness() -> EngineHarness:
    """The PostgreSQL harness."""
    return EngineHarness(
        engine=PostgresEngine(),
        sample_spec=SAMPLE_SPEC.replace("PGVECTOR", PGVECTOR),
        sample_column=ColumnRef(
            table=TableName.of("datadouble", "public", "customer"), column=ColumnName("email")
        ),
        scratch=scratch,
        runtime=runtime_for_tests(),
    )


class TestConformance(EngineConformance):
    """PostgreSQL passes the engine conformance suite."""


def _tooling(tmp_path: Path) -> tuple[Registry, Tools]:
    registry = Registry(
        engines={"postgres": PostgresEngine},
        stores={"filesystem": filesystem_store},
        runtimes=installed().runtimes,
    )
    ((kind, settings),) = runtime_for_tests().items()
    return registry, Tools(registry.runtime(kind, settings), PGVECTOR, tmp_path / "tools")


def test_the_image_lists_every_extension_version(tmp_path: Path) -> None:
    _, tools = _tooling(tmp_path)
    tooling = Tooling(runtime=tools.runtime, image=PGVECTOR)
    available = PostgresEngine().working(tmp_path / "scratch", tooling, "18.7").available() or ()
    assert {("hstore", "1.4"), ("hstore", "1.8"), ("vector", "0.8.7")} <= {
        (r.name, r.version) for r in available
    }
    assert {r.kind for r in available} == {RequirementKind.EXTENSION}


def test_extensions_keep_their_versions_and_omitted_ones_go(tmp_path: Path) -> None:
    registry, tools = _tooling(tmp_path)
    with scratch() as source:
        source.execute(sql.SQL("CREATE EXTENSION hstore VERSION '1.4'"))
        source.execute(sql.SQL("CREATE EXTENSION cube"))
        source.execute(sql.SQL("CREATE TABLE public.tags (id integer PRIMARY KEY, a hstore)"))
        source.execute(sql.SQL("INSERT INTO public.tags VALUES (1, 'k=>v')"))
        settings = json.dumps(dict(source.connection))
        spec = parse_spec(
            f"name: scratch\nsource: {{engine: postgres, connection: {settings},"
            f" image: {json.dumps(PGVECTOR)}, omit: [cube],"
            " server: {shared_preload_libraries: [pg_stat_statements]}}\n"
            "subset: {seeds: {items: 'true', tags: 'true'}, unselected: none}\n"
            f"runtime: {json.dumps(runtime_for_tests())}\n"
        )
        with PostgresEngine().connect(source.connection) as session:
            catalog = session.catalog()
        assert {str(r) for r in catalog.requirements} == {
            "extension hstore 1.4",
            "extension cube 1.5",
        }
        options = RunOptions(stages=(Stage.PLAN, Stage.EXTRACT, Stage.LOAD), work=tmp_path / "work")
        result = Run(spec, CatalogLock.of(catalog), registry, options).execute()
        assert not result.failed()
    datadir = next((tmp_path / "work").rglob("pgdata"))
    cluster = Cluster(tools, datadir, datadir.parent)
    with cluster.running(), cluster.connect(source.database) as database:
        rows = database.execute("SELECT extname, extversion FROM pg_extension").fetchall()
        installed_now = {str(name): str(version) for name, version in rows}
        preload = database.execute("SHOW shared_preload_libraries").fetchone()
    assert installed_now == {"plpgsql": "1.0", "hstore": "1.4"}
    assert preload == ("pg_stat_statements",)


def test_the_catalog_lists_pagilas_objects_and_their_dependencies() -> None:
    with PostgresEngine().connect({"dsn": {"env": DSN_VARIABLE}}) as session:
        catalog = session.catalog()
    objects = {str(item.ref): item for item in catalog.objects}

    def edges(name: str) -> set[str]:
        return {str(edge) for edge in objects[name].depends_on}

    assert {
        "type datadouble.public.mpaa_rating",
        "type datadouble.public.year",
        "sequence datadouble.public.actor_actor_id_seq",
        "view datadouble.public.staff_list",
    } <= set(objects)
    # Exact edges from pg_depend: a trigger's table and function, a view's tables.
    assert edges("trigger datadouble.public.actor.last_updated") == {
        "table datadouble.public.actor",
        "function datadouble.public.last_updated()",
    }
    assert {"table datadouble.public.actor", "table datadouble.public.film"} <= edges(
        "view datadouble.public.actor_info"
    )
    assert {"table datadouble.public.rental", "table datadouble.public.category"} <= edges(
        "materialized-view datadouble.public.rental_by_category"
    )
    assert edges("type datadouble.public.rating_note") == {"type datadouble.public.mpaa_rating"}

    def needs(name: str) -> set[str]:
        table = catalog.table(TableName.of("datadouble", "public", name))
        assert table is not None
        return {str(edge) for edge in table.depends_on}

    # Tables need their defaults' sequences and their columns' types, exactly.
    assert "sequence datadouble.public.actor_actor_id_seq" in needs("actor")
    assert {"type datadouble.public.mpaa_rating", "type datadouble.public.year"} <= needs("film")
    # PL/pgSQL bodies are parsed: their edges are inferred.
    balance = (
        "function datadouble.public.get_customer_balance"
        "(p_customer_id integer, p_effective_date timestamp with time zone)"
    )
    assert {
        "table datadouble.public.film (inferred)",
        "table datadouble.public.rental (inferred)",
        "table datadouble.public.payment (inferred)",
    } <= edges(balance)
    # A function's signature is PostgreSQL's identity argument list, which names a
    # SQL function's OUT parameters too.
    in_stock = (
        "function datadouble.public.film_in_stock"
        "(p_film_id integer, p_store_id integer, OUT p_film_count integer)"
    )
    assert (
        "function datadouble.public.inventory_in_stock(p_inventory_id integer) (inferred)"
        in edges(in_stock)
    )
    # The same source gives the same objects, so locks and snapshots agree.
    with PostgresEngine().connect({"dsn": {"env": DSN_VARIABLE}}) as session:
        assert session.catalog().fingerprint() == catalog.fingerprint()
