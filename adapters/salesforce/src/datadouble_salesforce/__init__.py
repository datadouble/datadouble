# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The Salesforce engine adapter for Datadouble.

Spec settings::

    source:
      engine: salesforce
      connection:
        instance_url:                               # by reference
          env: SHOP_SF_INSTANCE_URL
        access_token:
          env: SHOP_SF_ACCESS_TOKEN
        api_version: "62.0"                         # the default
        objects: [Account, Contact, "Acme__*"]      # optional: only these objects

or, for the fake org (:mod:`datadouble_salesforce.fake`)::

      connection:
        fake: /path/to/org.sqlite

The adapter reads the org through its REST, Tooling and Bulk 2.0 APIs, and
builds its working copies, anonymization and images locally in SQLite: the
image is logical, each object's records as JSON lines.
"""

import re
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

from datadouble.engine import EngineError, ServerPlan, SourceSession, Tooling, WorkingDatabases
from datadouble.names import EngineName
from datadouble.planner import Capabilities
from datadouble.runtime import identity
from datadouble.secrets import resolve_secret
from datadouble.settings import NoSettings, parse_settings
from datadouble.spec import ImageFormat, SecretRef
from datadouble_salesforce.fake import FakeOrg, FakeTransport
from datadouble_salesforce.org import DEFAULT_API_VERSION, RestOrg
from datadouble_salesforce.source import ENGINE, SalesforceSource
from datadouble_salesforce.working import SalesforceWorking

FAKE_URL = "https://fake.example.com"
"""The instance URL requests to the fake org name."""
_TIMEOUT = httpx.Timeout(120.0, connect=15.0)


type HiddenField = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")]
"""A field as ``Object.Field``."""


class OrgConnection(BaseModel):
    """The ``source.connection`` settings for an org."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    instance_url: SecretRef
    access_token: SecretRef
    api_version: str = Field(default=DEFAULT_API_VERSION, pattern=re.compile(r"^\d+\.\d$"))
    objects: tuple[str, ...] = ()
    """Patterns (``*``, ``?``, ``[...]``) for the objects to read; all, when empty."""
    hidden: tuple[HiddenField, ...] = ()
    """Fields field-level security may hide from the user, which copies go without."""


class FakeConnection(BaseModel):
    """The ``source.connection`` settings for the fake org."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    fake: str
    api_version: str = Field(default=DEFAULT_API_VERSION, pattern=re.compile(r"^\d+\.\d$"))
    objects: tuple[str, ...] = ()
    hidden: tuple[HiddenField, ...] = ()


class SalesforceEngine:
    """Implements :class:`datadouble.engine.EngineAdapter` for Salesforce."""

    @property
    def name(self) -> EngineName:
        """``salesforce``."""
        return ENGINE

    @property
    def owner(self) -> str:
        """Whoever runs Datadouble: copies are local files, served by no server."""
        return identity()

    def capabilities(self) -> Capabilities:
        """Logical images only; no expressions, nested strategy or hooks."""
        return Capabilities(
            formats=(ImageFormat.LOGICAL,), expressions=False, nested=False, hooks=False
        )

    @contextmanager
    def connect(self, connection: Mapping[str, JsonValue]) -> Generator[SourceSession]:
        """Open the org, or the fake."""
        settings: OrgConnection | FakeConnection
        if "fake" in connection:
            settings = parse_settings(FakeConnection, connection, "salesforce connection")
            path = Path(settings.fake)
            if not path.is_file():
                message = f"the fake org {path} does not exist"
                raise EngineError(message)
            transport: httpx.BaseTransport = FakeTransport(FakeOrg(path))
            url, token, poll = FAKE_URL, "fake", 0.0
        else:
            settings = parse_settings(OrgConnection, connection, "salesforce connection")
            transport = httpx.HTTPTransport(retries=2)
            url, token = (
                resolve_secret(settings.instance_url),
                resolve_secret(settings.access_token),
            )
            poll = 2.0
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        with httpx.Client(
            base_url=url, headers=headers, transport=transport, timeout=_TIMEOUT
        ) as client:
            org = RestOrg(client, settings.api_version, poll_seconds=poll)
            yield SalesforceSource(
                org, settings.api_version, settings.objects, frozenset(settings.hidden)
            )

    def working(self, scratch: Path, tooling: Tooling, source_version: str) -> WorkingDatabases:  # noqa: ARG002 - copies are SQLite at every API version
        """SQLite copies under ``scratch``; no container image applies."""
        parse_settings(NoSettings, tooling.server, "salesforce server")
        if tooling.image is not None:
            message = "Salesforce copies are local SQLite databases; source.image does not apply"
            raise EngineError(message)
        return SalesforceWorking(scratch)

    def server(self, image: Path, server_image: str | None) -> ServerPlan:
        """A Salesforce image has no server to start."""
        message = (
            f"{image.name}: a Salesforce image has no server to start, in"
            f" {server_image or 'any'} image; its copies are records for an org"
        )
        raise EngineError(message)


def engine() -> SalesforceEngine:
    """The entry point named in ``datadouble.engines``."""
    return SalesforceEngine()
