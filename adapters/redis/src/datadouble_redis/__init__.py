# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The Redis engine adapter for Datadouble.

Spec settings::

    source:
      engine: redis
      connection:
        url: {env: SESSIONS_URL}   # redis:// or rediss://, by reference
        databases: [0]             # the database numbers to copy
        separator: ":"             # between a key's segments
        patterns: ["session:{}"]   # key patterns inference would get wrong
        sample: 1000               # keys sampled per pattern for its columns

Each key pattern is a table (see :mod:`datadouble_redis.keys`). Extract reads
through the client; working databases, restores and served copies run in the
official Redis image of the source's exact version (``redis:<version>``), or
in ``source.image``, through the spec's container runtime.
"""

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from datadouble.engine import ServerPlan, SourceSession, Tooling, WorkingDatabases
from datadouble.names import EngineName
from datadouble.planner import Capabilities
from datadouble.secrets import resolve_secret
from datadouble.settings import NoSettings, parse_settings
from datadouble.spec import ImageFormat, SecretRef
from datadouble_redis.driver import Client, Error, failure
from datadouble_redis.keys import Pattern
from datadouble_redis.server import PORT, Tools, default_image, ready_command, start_command
from datadouble_redis.source import ENGINE, Layout, RedisSource
from datadouble_redis.working import DatabaseInfo, RedisWorking, server_databases

MOUNT = "/var/lib/datadouble"
"""Where container servers mount a copy."""


class RedisConnection(BaseModel):
    """The ``source.connection`` settings."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    url: SecretRef
    databases: tuple[int, ...] = Field(default=(0,), min_length=1)
    separator: str = Field(default=":", min_length=1)
    patterns: tuple[str, ...] = ()
    sample: int = Field(default=1000, ge=1)

    def layout(self) -> Layout:
        """How the source's keys are grouped into tables; checks the patterns."""
        for pattern in self.patterns:
            Pattern.parse(pattern)
        return Layout(
            databases=tuple(sorted(set(self.databases))),
            separator=self.separator,
            patterns=self.patterns,
        )


class RedisEngine:
    """Implements :class:`datadouble.engine.EngineAdapter` for Redis."""

    @property
    def name(self) -> EngineName:
        """``redis``."""
        return ENGINE

    @property
    def owner(self) -> str:
        """``999:999``, the user the official Redis image serves as."""
        return "999:999"

    def capabilities(self) -> Capabilities:
        """Physical images and Lua expressions; no nested strategy or hooks."""
        return Capabilities(
            formats=(ImageFormat.PHYSICAL,), expressions=True, nested=False, hooks=False
        )

    @contextmanager
    def connect(self, connection: Mapping[str, JsonValue]) -> Generator[SourceSession]:
        """Connect to each configured database of the source."""
        settings = parse_settings(RedisConnection, connection, "redis connection")
        layout = settings.layout()
        url = resolve_secret(settings.url)
        clients = {database: Client.from_url(url, database) for database in layout.databases}
        source = RedisSource(clients, layout, settings.sample)
        try:
            source.first.command("PING")
        except Error as error:
            source.close()
            raise failure(error, "connecting to the source") from None
        try:
            yield source
        finally:
            source.close()

    def working(self, scratch: Path, tooling: Tooling, source_version: str) -> WorkingDatabases:
        """``redis-server`` processes under ``scratch``, in the image of the source's version."""
        parse_settings(NoSettings, tooling.server, "redis server")
        image = tooling.image or default_image(source_version)
        return RedisWorking(Tools(tooling.runtime, image), scratch)

    def server(self, image: Path, server_image: str | None) -> ServerPlan:
        """The official Redis image of the image's version, on the copy.

        The copy's password is the default user's; the server saves to the
        copy as Redis normally does.
        """
        info = DatabaseInfo.model_validate_json((image / "database.json").read_bytes())
        data = f"{MOUNT}/data"
        return ServerPlan(
            image=server_image or default_image(info.server_version),
            mount=MOUNT,
            owner=self.owner,
            command=("sh", "-c", start_command(data, server_databases(info.layout), persist=True)),
            ready=ready_command(),
            port=PORT,
            database=str(info.layout.databases[0]),
            user="default",
        )


def engine() -> RedisEngine:
    """The entry point named in ``datadouble.engines``."""
    return RedisEngine()
