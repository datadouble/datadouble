# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The engine conformance suite against Sakila, in the Compose oracle service."""

import os
import uuid
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import quote as url_quote
from urllib.parse import urlsplit, urlunsplit

import pytest
from pydantic import JsonValue

from datadouble.names import ColumnName, ColumnRef, TableName
from datadouble_conformance.engine import (
    EngineConformance,
    EngineHarness,
    ScratchSource,
    runtime_for_tests,
)
from datadouble_oracle import OracleEngine, parse_url
from datadouble_oracle.driver import Connection, Value, connect
from datadouble_oracle.render import quote

# Whole builds start an Oracle container (about 38 s) for each stage and image
# format, and the strategies test runs two builds: twice the usual limit.
pytestmark = [pytest.mark.integration, pytest.mark.timeout(1200)]

URL_VARIABLE = "DATADOUBLE_TEST_ORACLE_URL"

# Customers 1-40 by the anchor time, their rentals and payments, referrals
# and notes, and everything they refer to, as in the MySQL suite. The two
# stores are taken whole, which breaks the store-staff cycle.
SAMPLE_SPEC = """
name: sakila
source:
  engine: oracle
  connection: {url: {env: DATADOUBLE_TEST_ORACLE_URL}, schemas: [SAKILA]}
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
    body: {expression: "TO_CLOB('note removed')", where: "subject_type = 'customer'"}
  customer_referral:
    note: {null: {}}
verify:
  assertions:
    - name: active is a flag
      sql: "SELECT 1 FROM customer WHERE active NOT IN ('0', '1')"
store:
  - filesystem: {path: STORE}
"""


def open_connection(url: str) -> Connection:
    """A connection to the test database as the URL's user."""
    options = parse_url(url)
    return connect(options["user"], options["password"], options["dsn"])


@dataclass
class OracleScratch:
    """A throwaway schema on the test database, holding ``items("id")``."""

    schema: str
    variable: str

    @property
    def connection(self) -> Mapping[str, JsonValue]:
        """Settings pointing at the scratch schema through an environment variable."""
        return {"url": {"env": self.variable}}

    @property
    def items(self) -> TableName:
        """The scratch table."""
        return TableName.of(self.schema, "ITEMS")

    def execute(self, statement: str, params: list[Value] | None = None) -> None:
        """Run a statement as the scratch schema's user, and commit."""
        with (
            open_connection(os.environ[self.variable]) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(statement, params)
            connection.commit()

    def add_table(self, name: str) -> None:
        """Create another table."""
        self.execute(f"CREATE TABLE {quote(name.upper())} (id NUMBER PRIMARY KEY)")

    def add_column(self, column: str) -> None:
        """Add a column to ``items``."""
        self.execute(f"ALTER TABLE items ADD ({quote(column.upper())} VARCHAR2(100))")

    def insert(self, item_id: int) -> None:
        """Insert and commit a row."""
        self.execute('INSERT INTO items ("id") VALUES (:1)', [item_id])


def admin(statement: str) -> None:
    """Run a statement as the test database's administrator."""
    with open_connection(os.environ[URL_VARIABLE]) as connection, connection.cursor() as cursor:
        cursor.execute(statement)


@contextmanager
def scratch() -> Generator[ScratchSource]:
    """Create a scratch schema (an Oracle user), and drop it afterwards."""
    schema = f"DD_SCRATCH_{uuid.uuid4().hex[:12].upper()}"
    password = f"Sc{uuid.uuid4().hex}"
    variable = f"DD_{schema}"
    parts = urlsplit(os.environ[URL_VARIABLE])
    host = parts.netloc.rsplit("@", 1)[1]
    os.environ[variable] = urlunsplit(
        (parts.scheme, f"{schema}:{url_quote(password)}@{host}", parts.path, "", "")
    )
    admin(
        f'CREATE USER {quote(schema)} IDENTIFIED BY "{password}"'
        " DEFAULT TABLESPACE sakila_data QUOTA UNLIMITED ON sakila_data"
    )
    admin(
        "GRANT CREATE SESSION, CREATE TABLE, CREATE VIEW, CREATE TRIGGER, CREATE PROCEDURE,"
        f" CREATE SEQUENCE, SELECT_CATALOG_ROLE TO {quote(schema)}"
    )
    source = OracleScratch(schema, variable)
    try:
        # Quoted: the suite names the column in lower case, which Oracle
        # would otherwise fold to upper.
        source.execute('CREATE TABLE items ("id" NUMBER PRIMARY KEY)')
        yield source
    finally:
        os.environ.pop(variable, None)
        admin(f"DROP USER {quote(schema)} CASCADE")


@pytest.fixture
def harness() -> EngineHarness:
    """The Oracle harness."""
    return EngineHarness(
        engine=OracleEngine(),
        sample_spec=SAMPLE_SPEC,
        sample_column=ColumnRef(
            table=TableName.of("SAKILA", "CUSTOMER"), column=ColumnName("EMAIL")
        ),
        scratch=scratch,
        runtime=runtime_for_tests(),
    )


class TestConformance(EngineConformance):
    """Oracle passes the engine conformance suite."""
