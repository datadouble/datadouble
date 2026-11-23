# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The engine conformance suite against the sample fake org."""

import tempfile
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import JsonValue

from datadouble.names import ColumnName, ColumnRef, TableName
from datadouble_conformance.engine import (
    EngineConformance,
    EngineHarness,
    ScratchSource,
    runtime_for_tests,
)
from datadouble_salesforce import SalesforceEngine
from datadouble_salesforce.fake import FakeField, FakeOrg, sample_org

pytestmark = pytest.mark.integration

# Technology accounts created by the anchor time, with their contacts,
# opportunities, cases and ratings, and every account those refer to. Tasks'
# references are polymorphic, so they are virtual; leads are left out.
SAMPLE_SPEC = """
name: crm
source:
  engine: salesforce
  connection:
    fake: ORG
exclude: [Lead]
subset:
  unselected: none
  seeds:
    Account: "Industry = 'technology' AND CreatedDate <= :as_of"
  follow:
    down:
      - Account -> Contact
      - Account -> Opportunity
      - Account -> Case
      - Account -> dd_ratings__Rating__c
      - Contact -> Task.WhoId
  edges:
    Case.ContactId -> Contact:
      on_missing: null
  virtual_fks:
    Task.WhoId -> Contact: {}
anonymize:
  Contact:
    FirstName:
      faker: first_name
    LastName:
      faker: last_name
    Email:
      faker: email
      unique: true
    Phone:
      faker: phone_number
    Birthdate:
      null: {}
  Account:
    Name:
      hash:
        length: 12
verify:
  assertions:
    - name: no real email addresses
      sql: "SELECT Id FROM Contact WHERE NOT (Email LIKE '%@example.%') AND Email != null"
store:
  - filesystem:
      path: STORE
"""


@dataclass
class FakeScratch:
    """A throwaway fake org, holding ``items``."""

    org: FakeOrg

    @property
    def connection(self) -> Mapping[str, JsonValue]:
        """Settings pointing at the fake."""
        return {"fake": str(self.org.path)}

    @property
    def items(self) -> TableName:
        """The scratch object."""
        return TableName.of("items")

    def add_table(self, name: str) -> None:
        """Add another object."""
        self.org.add_object(name)

    def add_column(self, column: str) -> None:
        """Add a text field to ``items``."""
        self.org.add_field("items", FakeField(column))

    def insert(self, item_id: int) -> None:
        """Insert an item, whose Id is ``item_id``."""
        self.org.insert("items", {"Id": str(item_id)})


@contextmanager
def scratch() -> Generator[ScratchSource]:
    """A fake org with one object, removed afterwards."""
    with tempfile.TemporaryDirectory() as directory:
        org = FakeOrg(Path(directory) / "scratch.sqlite")
        org.add_object("items")
        yield FakeScratch(org)


@pytest.fixture
def harness(tmp_path: Path) -> EngineHarness:
    """The Salesforce harness, on a fresh sample org."""
    sample_org(tmp_path / "org.sqlite")
    return EngineHarness(
        engine=SalesforceEngine(),
        sample_spec=SAMPLE_SPEC.replace("ORG", str(tmp_path / "org.sqlite")),
        sample_column=ColumnRef(table=TableName.of("Contact"), column=ColumnName("Email")),
        scratch=scratch,
        runtime=runtime_for_tests(),
        snapshot=False,
    )


class TestConformance(EngineConformance):
    """Salesforce, against the fake, passes the engine conformance suite."""
