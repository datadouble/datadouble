# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The Elasticsearch engine adapter for Datadouble.

Spec settings::

    source:
      engine: elasticsearch
      connection:
        url: {env: SEARCH_URL}      # http[s]://[user:password@]host[:port], by reference
        api_key: {env: SEARCH_KEY}  # optional; sent instead of the URL's credentials
        indices: ["*", "-logs-*"]   # index patterns; the default is every open index
        sample: 1000                # documents sampled for fields mappings do not declare
      server:
        heap: 512m                  # the Java heap of working nodes

The source is Elasticsearch 7.17 or later. Extract reads over HTTP. Working
nodes and restores run in the official Elasticsearch image of the source's
exact version (``elasticsearch:<version>``), or in ``source.image``, through
the spec's container runtime.
"""

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from datadouble.engine import ServerPlan, SourceSession, Tooling, WorkingDatabases
from datadouble.names import EngineName
from datadouble.planner import Capabilities
from datadouble.secrets import resolve_secret
from datadouble.settings import parse_settings
from datadouble.spec import ImageFormat, SecretRef
from datadouble_elasticsearch.client import Client
from datadouble_elasticsearch.product import ELASTICSEARCH, Product
from datadouble_elasticsearch.server import PORT, ServerSettings, Tools, ready_command
from datadouble_elasticsearch.source import ElasticSource, Layout, server_version
from datadouble_elasticsearch.working import DatabaseInfo, ElasticWorking, serve_command

MOUNT = "/var/lib/datadouble"
"""Where container servers mount a copy."""


class ElasticConnection(BaseModel):
    """The ``source.connection`` settings."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    url: SecretRef
    api_key: SecretRef | None = None
    indices: tuple[str, ...] = Field(default=("*",), min_length=1)
    sample: int = Field(default=1000, ge=1)


class ElasticEngine:
    """Implements :class:`datadouble.engine.EngineAdapter` for Elasticsearch, or another product."""

    def __init__(self, product: Product = ELASTICSEARCH) -> None:
        """Serve ``product``."""
        self.product = product

    @property
    def name(self) -> EngineName:
        """The product's engine name, such as ``elasticsearch``."""
        return self.product.engine

    @property
    def owner(self) -> str:
        """The user the product's official image serves as, such as ``1000:0``."""
        return self.product.owner

    def capabilities(self) -> Capabilities:
        """Both formats and Painless expressions; no nested strategy or hooks."""
        return Capabilities(
            formats=(ImageFormat.PHYSICAL, ImageFormat.LOGICAL),
            expressions=True,
            nested=False,
            hooks=False,
            # A snapshot from 8.15 cannot be restored on 8.12: each minor is a line.
            version_line=2,
        )

    @contextmanager
    def connect(self, connection: Mapping[str, JsonValue]) -> Generator[SourceSession]:
        """Connect to the source."""
        settings = parse_settings(ElasticConnection, connection, f"{self.name} connection")
        api_key = resolve_secret(settings.api_key) if settings.api_key else None
        client = Client.from_url(resolve_secret(settings.url), self.product, api_key)
        try:
            server_version(client)
            yield ElasticSource(client, Layout(indices=settings.indices, sample=settings.sample))
        finally:
            client.close()

    def working(self, scratch: Path, tooling: Tooling, source_version: str) -> WorkingDatabases:
        """Single-node clusters under ``scratch``, in the image for the source's version."""
        settings = parse_settings(ServerSettings, tooling.server, f"{self.name} server")
        image = tooling.image or self.product.image(source_version)
        return ElasticWorking(Tools(self.product, tooling.runtime, image, settings.heap), scratch)

    def server(self, image: Path, server_image: str | None) -> ServerPlan:
        """The product's official image of the image's version, on the copy.

        The first start restores the image's snapshot into the copy; the
        copy's password is the administrator's.
        """
        info = DatabaseInfo.read(image)
        command, marker = serve_command(self.product, MOUNT, ServerSettings().heap)
        return ServerPlan(
            image=server_image or self.product.image(info.server_version),
            mount=MOUNT,
            owner=self.owner,
            command=("sh", "-c", command),
            ready=ready_command(self.product, marker),
            port=PORT,
            database="",
            user=self.product.user,
        )


def engine() -> ElasticEngine:
    """The entry point named in ``datadouble.engines``."""
    return ElasticEngine()
