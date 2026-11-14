# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The engine conformance suite against the generated indices in the Compose service."""

import os
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass

import pytest
from pydantic import JsonValue

from datadouble.names import ColumnName, ColumnRef, TableName
from datadouble_conformance.engine import (
    EngineConformance,
    EngineHarness,
    ScratchSource,
    runtime_for_tests,
)
from datadouble_elasticsearch import ElasticEngine
from datadouble_elasticsearch.client import Client, path
from datadouble_elasticsearch.product import ELASTICSEARCH, Product

pytestmark = pytest.mark.integration

URL = "DATADOUBLE_TEST_ELASTICSEARCH_URL"
IMAGE = (
    "elasticsearch:9.5.5@sha256:b7b0f24e9b5e90656ffe69804b3a48907499ab2b9cd67f02b7af3aff1b026a89"
)
SCRATCH = ("items", "added")
"""The scratch source's indices, which the sample leaves out."""

# Customers up to 40 with their rentals, payments and reviews; a few rentals
# of their own, one of a deleted customer; every store, and events to date.
# Parents (films, staff, stores) come in as the references need them.
SAMPLE_SPEC = f"""
name: rentals
source:
  engine: elasticsearch
  connection:
    url: {{env: {URL}}}
    indices: ["*", "-items", "-added"]
  image: {IMAGE}
subset:
  seeds:
    customers: '{{"range": {{"customer_id": {{"lte": 40}}}}}}'
    rentals: '{{"range": {{"rental_id": {{"gte": 295, "lte": 305}}}}}}'
    stores: '{{"match_all": {{}}}}'
    events: '{{"range": {{"at": {{"lte": ":as_of"}}}}}}'
    archived_customers: '{{"match_all": {{}}}}'
  follow:
    down:
      - customers -> rentals.customer_id
      - rentals -> payments.rental_id
      - customers -> reviews.subject_id
  edges:
    rentals.customer_id -> customers: {{on_missing: null}}
  virtual_fks:
    rentals.customer_id -> customers: {{}}
    rentals.film_id -> films: {{}}
    rentals.staff_id -> staff.staff_id: {{}}
    payments.rental_id -> rentals.rental_id: {{}}
    payments.customer_id -> customers.customer_id: {{}}
    staff.store_id -> stores: {{}}
    staff.manager_id -> staff.staff_id: {{}}
    stores.manager_staff_id -> staff.staff_id: {{}}
    reviews.subject_id -> customers: {{when: '{{"term": {{"subject_type": "customer"}}}}'}}
    reviews.subject_id -> films: {{when: '{{"term": {{"subject_type": "film"}}}}'}}
anonymize:
  customers:
    first_name: {{faker: first_name}}
    last_name: {{faker: last_name}}
    email: {{faker: email, unique: true}}
    address.address: {{faker: street_address}}
    address.phone: {{faker: phone_number}}
  staff:
    first_name: {{faker: first_name}}
    last_name: {{faker: last_name}}
    email: {{faker: email, unique: true}}
    username: {{hash: {{length: 12}}}}
  stores:
    address.address: {{faker: street_address}}
    address.phone: {{faker: phone_number}}
  reviews:
    reviewer.name: {{faker: name}}
    reviewer.email: {{faker: email}}
    body: {{expression: "ctx._source.body = 'review removed'"}}
  events:
    client.ip: {{faker: ipv4}}
store:
  - filesystem: {{path: STORE}}
"""


@dataclass
class ElasticScratch:
    """The ``items`` index on a test cluster, emptied for each use."""

    client: Client
    url: str
    """The environment variable with the cluster's URL."""

    @property
    def connection(self) -> Mapping[str, JsonValue]:
        """Settings naming the scratch indices."""
        return {"url": {"env": self.url}, "indices": list(SCRATCH)}

    @property
    def items(self) -> TableName:
        """The scratch table."""
        return TableName.of("", "items")

    def add_table(self, name: str) -> None:
        """Create another index."""
        self.client.request("PUT", path(name), "creating an index")

    def add_column(self, column: str) -> None:
        """Map a new field in ``items``."""
        self.client.request(
            "PUT",
            path("items", "_mapping"),
            "mapping a field",
            body={"properties": {column: {"type": "keyword"}}},
        )

    def insert(self, item_id: int) -> None:
        """Index an item, visible to searches at once."""
        self.client.request(
            "PUT",
            path("items", "_doc", str(item_id)),
            "indexing an item",
            body={"id": item_id},
            params={"refresh": True},
        )

    def drop(self) -> None:
        """Delete the scratch indices."""
        self.client.request(
            "DELETE",
            path(",".join(SCRATCH)),
            "deleting the scratch indices",
            params={"ignore_unavailable": True},
        )


@contextmanager
def scratch(url: str = URL, product: Product = ELASTICSEARCH) -> Generator[ElasticScratch]:
    """A fresh ``items`` index on the cluster at ``$url`` for the block, deleted afterwards."""
    source = ElasticScratch(Client.from_url(os.environ[url], product), url)
    source.drop()
    source.client.request(
        "PUT",
        path("items"),
        "creating items",
        body={"mappings": {"properties": {"id": {"type": "long"}}}},
    )
    try:
        yield source
    finally:
        source.drop()
        source.client.close()


@contextmanager
def _scratch() -> Generator[ScratchSource]:
    with scratch() as source:
        yield source


@pytest.fixture
def harness() -> EngineHarness:
    """The Elasticsearch harness."""
    return EngineHarness(
        engine=ElasticEngine(),
        sample_spec=SAMPLE_SPEC,
        sample_column=ColumnRef(table=TableName.of("", "customers"), column=ColumnName("email")),
        scratch=_scratch,
        runtime=runtime_for_tests(),
    )


class TestConformance(EngineConformance):
    """Elasticsearch passes the engine conformance suite."""
