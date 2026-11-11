# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The engine conformance suite against the cinema database, in the Compose mongodb service."""

import os
import uuid
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass

import pytest
from pydantic import JsonValue
from pymongo import MongoClient

from datadouble.names import ColumnName, ColumnRef, TableName
from datadouble_conformance.engine import EngineConformance, EngineHarness, ScratchSource
from datadouble_mongodb import MongoEngine

pytestmark = pytest.mark.integration

URI = "DATADOUBLE_TEST_MONGODB_URI"

# Users created by the anchor time, with their comments, watchlists, sessions
# and notes; the movies those refer to. MongoDB declares no references, so
# every one is virtual; notes refer to users or movies, by subject_type.
SAMPLE_SPEC = """
name: cinema
source:
  engine: mongodb
  connection: {uri: {env: DATADOUBLE_TEST_MONGODB_URI}, database: cinema}
exclude: ["maintenance_runs"]
subset:
  seeds:
    users: '{"created": {"$lte": ":as_of"}, "tags": "critic"}'
  follow:
    down:
      - users -> comments.user_id
      - users -> watchlists.user_id
      - users -> sessions.user_id
      - users -> notes.subject_id
  edges:
    comments.user_id -> users: {on_missing: null}
  virtual_fks:
    comments.user_id -> users: {}
    comments.movie_id -> movies: {}
    watchlists.user_id -> users: {}
    watchlists.movie_ids -> movies: {}
    sessions.user_id -> users: {}
    notes.subject_id -> users: {when: '{"subject_type": "user"}'}
anonymize:
  users:
    name: {faker: name}
    email: {faker: email, unique: true}
    phone: {faker: phone_number}
    address.street: {faker: street_address}
  sessions:
    token: {hash: {length: 24}}
  comments:
    text: {expression: '{"$concat": ["Comment ", {"$toString": "$_id"}]}'}
  notes:
    body: {expression: '"note removed"', where: '{"subject_type": "user"}'}
store:
  - filesystem: {path: STORE}
"""


@dataclass
class MongoScratch:
    """A throwaway database on the test server, holding ``items``."""

    uri: str
    database: str

    @property
    def connection(self) -> Mapping[str, JsonValue]:
        """Settings pointing at the scratch database."""
        return {"uri": {"env": URI}, "database": self.database}

    @property
    def items(self) -> TableName:
        """The scratch collection."""
        return TableName.of(self.database, "items")

    def client(self) -> MongoClient[dict[str, object]]:
        """A client for the scratch server."""
        client: MongoClient[dict[str, object]] = MongoClient(self.uri)
        return client

    def add_table(self, name: str) -> None:
        """Create another collection."""
        with self.client() as client:
            client[self.database][name].insert_one({"_id": 1})

    def add_column(self, column: str) -> None:
        """Give an item a new field."""
        with self.client() as client:
            client[self.database]["items"].insert_one({"_id": -1, "id": -1, column: "x"})

    def insert(self, item_id: int) -> None:
        """Insert an item."""
        with self.client() as client:
            client[self.database]["items"].insert_one({"_id": item_id, "id": item_id})


@contextmanager
def scratch() -> Generator[ScratchSource]:
    """Create a scratch database, and drop it afterwards."""
    uri = os.environ[URI]
    source = MongoScratch(uri, f"dd_scratch_{uuid.uuid4().hex[:12]}")
    with source.client() as client:
        schema = {"properties": {"_id": {"bsonType": "int"}, "id": {"bsonType": "int"}}}
        client[source.database].create_collection("items", validator={"$jsonSchema": schema})
    try:
        yield source
    finally:
        with source.client() as client:
            client.drop_database(source.database)


@pytest.fixture
def harness() -> EngineHarness:
    """The MongoDB harness."""
    return EngineHarness(
        engine=MongoEngine(),
        sample_spec=SAMPLE_SPEC,
        sample_column=ColumnRef(table=TableName.of("cinema", "users"), column=ColumnName("email")),
        scratch=scratch,
    )


class TestConformance(EngineConformance):
    """MongoDB passes the engine conformance suite."""
