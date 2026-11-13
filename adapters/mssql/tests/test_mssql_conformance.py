# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The engine conformance suite against Sakila, in the Compose mssql service."""

import os
import uuid
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import pymssql
import pytest
from pydantic import JsonValue

from datadouble.names import ColumnName, ColumnRef, TableName
from datadouble_conformance.engine import (
    EngineConformance,
    EngineHarness,
    ScratchSource,
    runtime_for_tests,
)
from datadouble_mssql import MssqlEngine, parse_url
from datadouble_mssql.render import quote

pytestmark = pytest.mark.integration

URL_VARIABLE = "DATADOUBLE_TEST_MSSQL_URL"

# Customers 1-40 by the anchor time, their rentals and payments, referrals
# and notes, and everything they refer to, as in the MySQL suite. Sakila's
# references are all declared; the note's polymorphic subject is given as a
# virtual one. The two stores are taken whole, which breaks the store-staff
# cycle, and bring their sales targets, in a second schema.
SAMPLE_SPEC = """
name: sakila
source:
  engine: mssql
  connection: {url: {env: DATADOUBLE_TEST_MSSQL_URL}}
  server: {accept_eula: true}
exclude: ["dba_*"]
subset:
  seeds:
    customer: "customer_id <= 40 AND create_date <= :as_of"
  follow:
    down:
      - customer -> rental
      - rental -> payment
      - customer -> customer_referral
      - customer -> customer_note
      - store -> sales.region_target
  edges:
    payment.customer_id -> customer: {on_missing: drop}
    staff.store_id -> store: {follow: all}
    customer_referral.referred_by -> customer_referral: {on_missing: null}
  virtual_fks:
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
    body: {expression: "N'note removed'", where: "subject_type = 'customer'"}
  customer_referral:
    note: {null: {}}
  sales.region_target:
    note: {faker: text}
verify:
  assertions:
    - name: active is a flag
      sql: "SELECT 1 FROM customer WHERE active NOT IN ('0', '1')"
store:
  - filesystem: {path: STORE}
"""


def connect(url: str, database: str | None = None) -> pymssql.Connection:
    """A connection to the test server, to ``database`` or the URL's."""
    options, named = parse_url(url)
    return pymssql.connect(
        server=options["server"],
        port=options["port"],
        user=options["user"],
        password=options["password"],
        database=database or named,
        autocommit=True,
    )


@dataclass
class MssqlScratch:
    """A throwaway database on the test server, holding ``dbo.items(id)``."""

    database: str
    variable: str

    @property
    def connection(self) -> Mapping[str, JsonValue]:
        """Settings pointing at the scratch database through an environment variable."""
        return {"url": {"env": self.variable}}

    @property
    def items(self) -> TableName:
        """The scratch table."""
        return TableName.of("dbo", "items")

    def execute(self, statement: str, params: tuple[int, ...] | None = None) -> None:
        """Run a statement in the scratch database."""
        with connect(os.environ[self.variable]) as connection, connection.cursor() as cursor:
            cursor.execute(statement, params)

    def add_table(self, name: str) -> None:
        """Create another table."""
        self.execute(f"CREATE TABLE {quote(name)} (id INT PRIMARY KEY)")

    def add_column(self, column: str) -> None:
        """Add a column to ``items``."""
        self.execute(f"ALTER TABLE items ADD {quote(column)} NVARCHAR(MAX)")

    def insert(self, item_id: int) -> None:
        """Insert and commit a row."""
        self.execute("INSERT INTO items (id) VALUES (%d)", (item_id,))


def admin(statement: str) -> None:
    """Run a statement in ``master``."""
    with (
        connect(os.environ[URL_VARIABLE], "master") as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(statement)


@contextmanager
def scratch() -> Generator[ScratchSource]:
    """Create a scratch database, and drop it afterwards."""
    database = f"dd_scratch_{uuid.uuid4().hex[:12]}"
    variable = f"DD_{database.upper()}"
    parts = urlsplit(os.environ[URL_VARIABLE])
    os.environ[variable] = urlunsplit(
        (parts.scheme, parts.netloc, f"/{database}", parts.query, parts.fragment)
    )
    admin(f"CREATE DATABASE {quote(database)}")
    admin(f"ALTER DATABASE {quote(database)} SET ALLOW_SNAPSHOT_ISOLATION ON")
    source = MssqlScratch(database, variable)
    try:
        source.execute("CREATE TABLE items (id INT PRIMARY KEY)")
        yield source
    finally:
        os.environ.pop(variable, None)
        admin(f"ALTER DATABASE {quote(database)} SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
        admin(f"DROP DATABASE {quote(database)}")


@pytest.fixture
def harness() -> EngineHarness:
    """The SQL Server harness."""
    return EngineHarness(
        engine=MssqlEngine(),
        sample_spec=SAMPLE_SPEC,
        sample_column=ColumnRef(table=TableName.of("dbo", "customer"), column=ColumnName("email")),
        scratch=scratch,
        runtime=runtime_for_tests(),
    )


class TestConformance(EngineConformance):
    """SQL Server passes the engine conformance suite."""
