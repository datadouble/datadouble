# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The engine conformance suite against the generated keyspace, in the Compose redis service."""

import itertools
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
from datadouble_redis import RedisEngine
from datadouble_redis.driver import Client

pytestmark = pytest.mark.integration

URL = "DATADOUBLE_TEST_REDIS_URL"
IMAGE = "redis:8.10.3@sha256:1f88291b4c57bdf249f02eb085a8669889b2d2954e91d4de5118a5da6b78dd53"

# Users whose IDs start with 1, with their session sets and orders; the
# sessions, items and products those refer to; the leaderboard and its users;
# and the rest whole, but for maintenance runs. Session tokens look like
# names, so their pattern is declared.
SAMPLE_SPEC = f"""
name: shop
source:
  engine: redis
  connection:
    url: {{env: {URL}}}
    databases: [0, 1]
    patterns: ["session:{{}}"]
  image: {IMAGE}
exclude: ["maint:run:{{}}"]
subset:
  seeds:
    "user:{{}}": "1*"
    leaderboard: "*"
    audit: "*"
    "note:{{}}": "*"
    "profile:{{}}": "*"
    "flash:{{}}": "*"
    config: "*"
    "cache:{{}}": "*"
  follow:
    down:
      - "user:{{}} -> user:{{}}:sessions._key"
      - "user:{{}} -> order:{{}}.user_id"
      - "order:{{}} -> order:{{}}:items._key"
  virtual_fks:
    "user:{{}}:sessions._key -> user:{{}}": {{}}
    "user:{{}}:sessions.members -> session:{{}}": {{}}
    "session:{{}}.value -> user:{{}}": {{}}
    "order:{{}}.user_id -> user:{{}}": {{}}
    "order:{{}}:items._key -> order:{{}}": {{}}
    "order:{{}}:items.members -> product:{{}}": {{}}
    "leaderboard.members -> user:{{}}": {{}}
anonymize:
  "user:{{}}":
    name: {{faker: name}}
    email: {{faker: email, unique: true}}
    phone: {{faker: phone_number}}
  audit:
    ip: {{faker: ipv4}}
  "profile:{{}}":
    first.name: {{faker: first_name}}
  "order:{{}}":
    placed: {{expression: "redis.call('HSET', KEYS[1], ARGV[1], '2024-01-01T00:00:00Z')"}}
store:
  - filesystem: {{path: STORE}}
"""

_DATABASES = itertools.cycle(range(2, 16))


@dataclass
class RedisScratch:
    """An emptied database on the test server, holding ``items:{}`` hashes."""

    url: str
    database: int

    @property
    def connection(self) -> Mapping[str, JsonValue]:
        """Settings pointing at the scratch database."""
        return {"url": {"env": URL}, "databases": [self.database], "patterns": ["items:{}"]}

    @property
    def items(self) -> TableName:
        """The scratch table."""
        return TableName.of(f"db{self.database}", "items:{}")

    def command(self, *arguments: str | int) -> None:
        """Run one command on the scratch database."""
        client = Client.from_url(self.url, self.database)
        try:
            client.command(*arguments)
        finally:
            client.close()

    def add_table(self, name: str) -> None:
        """Create another table, a single key."""
        self.command("SET", name, "1")

    def add_column(self, column: str) -> None:
        """Give an item a new field."""
        self.command("HSET", "items:999", "id", 999, column, "x")

    def insert(self, item_id: int) -> None:
        """Insert an item."""
        self.command("HSET", f"items:{item_id}", "id", item_id)


@contextmanager
def scratch() -> Generator[ScratchSource]:
    """Empty a database of the test server for the block, and again afterwards."""
    source = RedisScratch(os.environ[URL], next(_DATABASES))
    source.command("FLUSHDB")
    try:
        yield source
    finally:
        source.command("FLUSHDB")


@pytest.fixture
def harness() -> EngineHarness:
    """The Redis harness."""
    return EngineHarness(
        engine=RedisEngine(),
        sample_spec=SAMPLE_SPEC,
        sample_column=ColumnRef(table=TableName.of("db0", "user:{}"), column=ColumnName("email")),
        scratch=scratch,
        runtime=runtime_for_tests(),
    )


class TestConformance(EngineConformance):
    """Redis passes the engine conformance suite."""
