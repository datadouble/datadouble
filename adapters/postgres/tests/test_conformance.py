# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The engine conformance suite against Pagila, in the Compose postgres service."""

import os
import socket
import uuid
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import JsonValue

from datadouble.names import ColumnName, ColumnRef, TableName
from datadouble_conformance.engine import (
    EngineConformance,
    EngineHarness,
    ScratchSource,
    runtime_for_tests,
)
from datadouble_postgres import PostgresEngine

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
    "pgvector/pgvector:0.8.2-pg18-trixie"
    "@sha256:b7337db8fe39d12fe8ecb0003c72680f24479813a744b43154eee6f2eab5a5f3"
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
  connection: {dsn: {env: DATADOUBLE_TEST_POSTGRES_DSN}}
  image: PGVECTOR
exclude: ["dba.*"]
subset:
  seeds:
    customer: "customer_id <= 40 AND create_date <= :as_of"
  follow:
    down:
      - customer -> rental
      - rental -> payment
      - customer -> customer_referral
      - customer -> customer_note
  edges:
    payment.customer_id -> customer: {on_missing: drop}
    customer_referral.referred_by -> customer_referral: {on_missing: null}
  virtual_fks:
    payment.rental_id -> rental: {}
    payment.customer_id -> customer: {}
    payment.staff_id -> staff: {}
    customer_note.subject_id -> customer: {when: "subject_type = 'customer'"}
anonymize:
  customer:
    first_name: {faker: first_name}
    last_name: {faker: last_name}
    email: {faker: email, unique: true}
  staff:
    first_name: {faker: first_name}
    last_name: {faker: last_name}
    email: {faker: email}
    username: {hash: {length: 12}}
    password: {hash: {length: 40}}
    picture: {null: {}}
  address:
    address: {faker: street_address}
    address2: {null: {}}
    phone: {faker: phone_number}
    postal_code: {faker: postcode}
  customer_note:
    body: {expression: "'note removed'", where: "subject_type = 'customer'"}
  customer_referral:
    note: {null: {}}
verify:
  assertions:
    - name: active is a flag
      sql: "SELECT 1 FROM customer WHERE active NOT IN (0, 1)"
store:
  - filesystem: {path: STORE}
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
        return TableName.of("public", "items")

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
def scratch() -> Generator[ScratchSource]:
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
            table=TableName.of("public", "customer"), column=ColumnName("email")
        ),
        scratch=scratch,
        runtime=runtime_for_tests(),
    )


class TestConformance(EngineConformance):
    """PostgreSQL passes the engine conformance suite."""
