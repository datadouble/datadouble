# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The engine conformance suite against Sakila, in the Compose mysql service."""

import os
import socket
import uuid
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import pymysql
import pytest
from pydantic import JsonValue

from datadouble.names import ColumnName, ColumnRef, TableName
from datadouble_conformance.engine import (
    EngineConformance,
    EngineHarness,
    ScratchSource,
    runtime_for_tests,
)
from datadouble_mysql import MysqlEngine, parse_url
from datadouble_mysql.render import quote

pytestmark = pytest.mark.integration

URL_VARIABLE = "DATADOUBLE_TEST_MYSQL_URL"


def by_address(url: str) -> str:
    """``url`` with its host name replaced by the address it resolves to.

    ``mysqldump`` runs in containers on the Docker-in-Docker daemon, where the
    Compose service names do not resolve.
    """
    parts = urlsplit(url)
    host = socket.gethostbyname(parts.hostname or "")
    credentials = parts.netloc.rsplit("@", 1)[0]
    netloc = f"{credentials}@{host}:{parts.port or 3306}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


if URL_VARIABLE in os.environ:
    os.environ[URL_VARIABLE] = by_address(os.environ[URL_VARIABLE])

# Customers 1-40 by the anchor time, their rentals and payments, referrals
# and notes, and everything they refer to. Sakila's references are all
# declared; the note's polymorphic subject is given as a virtual one. A
# payment whose customer is not chosen is left out rather than pulling one in,
# and the two stores are taken whole, which breaks the store-staff cycle.
SAMPLE_SPEC = """
name: sakila
source:
  engine: mysql
  connection: {url: {env: DATADOUBLE_TEST_MYSQL_URL}}
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
class MysqlScratch:
    """A throwaway database on the test server, holding ``items(id)``."""

    database: str
    variable: str

    @property
    def connection(self) -> Mapping[str, JsonValue]:
        """Settings pointing at the scratch database through an environment variable."""
        return {"url": {"env": self.variable}}

    @property
    def items(self) -> TableName:
        """The scratch table."""
        return TableName.of(self.database, "items")

    def execute(self, statement: str, params: list[object] | None = None) -> None:
        """Run a statement in the scratch database."""
        options, database = parse_url(os.environ[self.variable])
        with pymysql.connect(
            host=options["host"],
            port=int(options["port"]),
            user=options["user"],
            password=options["password"],
            database=database,
            autocommit=True,
        ) as connection:
            connection.cursor().execute(statement, params)

    def add_table(self, name: str) -> None:
        """Create another table."""
        self.execute(f"CREATE TABLE {quote(name)} (id INT PRIMARY KEY)")

    def add_column(self, column: str) -> None:
        """Add a column to ``items``."""
        self.execute(f"ALTER TABLE items ADD COLUMN {quote(column)} TEXT")

    def insert(self, item_id: int) -> None:
        """Insert and commit a row."""
        self.execute("INSERT INTO items (id) VALUES (%s)", [item_id])


def _admin(statement: str) -> None:
    options, _ = parse_url(os.environ[URL_VARIABLE])
    with pymysql.connect(
        host=options["host"],
        port=int(options["port"]),
        user=options["user"],
        password=options["password"],
        autocommit=True,
    ) as connection:
        connection.cursor().execute(statement)


@contextmanager
def scratch() -> Generator[ScratchSource]:
    """Create a scratch database, and drop it afterwards."""
    database = f"dd_scratch_{uuid.uuid4().hex[:12]}"
    variable = f"DD_{database.upper()}"
    parts = urlsplit(os.environ[URL_VARIABLE])
    os.environ[variable] = urlunsplit(
        (parts.scheme, parts.netloc, f"/{database}", parts.query, parts.fragment)
    )
    _admin(f"CREATE DATABASE {quote(database)}")
    source = MysqlScratch(database, variable)
    try:
        source.execute("CREATE TABLE items (id INT PRIMARY KEY)")
        yield source
    finally:
        os.environ.pop(variable, None)
        _admin(f"DROP DATABASE {quote(database)}")


@pytest.fixture
def harness() -> EngineHarness:
    """The MySQL harness."""
    return EngineHarness(
        engine=MysqlEngine(),
        sample_spec=SAMPLE_SPEC,
        sample_column=ColumnRef(
            table=TableName.of("sakila", "customer"), column=ColumnName("email")
        ),
        scratch=scratch,
        runtime=runtime_for_tests(),
    )


class TestConformance(EngineConformance):
    """MySQL passes the engine conformance suite."""
