# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Cassettes: recorded HTTP interactions with an org, scrubbed, and replayed.

:class:`RecordingTransport` passes each request on and keeps the request and
response. :func:`scrub` removes what must not be committed: credentials and
cookies (only the headers replay needs are kept), access tokens and session
IDs, the org's instance URL and other Salesforce hosts, which become
``org.example.com``, and org IDs, which become ``00D000000000000AAA``.
:class:`ReplayTransport` answers requests from a cassette, matching method,
path, query and body, but not the host. Requests that repeat, such as a Bulk
API job's status, get their recorded responses in order, then the last one
again.
"""

import re
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict

EXAMPLE_HOST = "org.example.com"
EXAMPLE_ORG_ID = "00D000000000000AAA"
REDACTED = "REDACTED"
_KEPT_HEADERS = {"content-type", "sforce-locator"}
_SESSION_ID = re.compile(r"\b00D[A-Za-z0-9]{12,15}![A-Za-z0-9._\-]+")
_SECRET_FIELD = re.compile(
    r'("(?:access_token|accessToken|refresh_token|refreshToken|id_token|sessionId|signature)"'
    r'\s*:\s*)"[^"]*"'
)
_SALESFORCE_HOST = re.compile(
    r"\b[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?:salesforce|force|visualforce|salesforce-sites"
    r"|cloudforce|database)\.com\b"
)
# Alone, or URL-encoded in a query (%2700D...%27).
_ORG_ID = re.compile(
    r"(?:(?<![A-Za-z0-9])|(?<=%[0-9A-Fa-f]{2}))00D[A-Za-z0-9]{12}(?:[A-Za-z0-9]{3})?(?![A-Za-z0-9])"
)


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class Interaction(_Model):
    """One request and its response."""

    method: str
    url: str
    """The path and query: replay ignores the host."""
    body: str
    status: int
    headers: dict[str, str]
    response: str


class Cassette(_Model):
    """Interactions, in the order they happened."""

    format: Literal[1] = 1
    interactions: tuple[Interaction, ...]

    def text(self) -> str:
        """The cassette as indented JSON, as it is committed."""
        return self.model_dump_json(indent=1) + "\n"

    @classmethod
    def read(cls, path: Path) -> "Cassette":
        """Read a cassette."""
        return cls.model_validate_json(path.read_bytes())


class CassetteError(httpx.TransportError):
    """A request the cassette has no response for."""


def _target(request: httpx.Request) -> str:
    parts = urlsplit(str(request.url))
    return parts.path + (f"?{parts.query}" if parts.query else "")


class RecordingTransport(httpx.BaseTransport):
    """Passes requests to another transport and records each interaction."""

    def __init__(self, inner: httpx.BaseTransport) -> None:
        """Record what ``inner`` answers."""
        self.inner = inner
        self.interactions: list[Interaction] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Pass the request on and keep it with its response."""
        response = self.inner.handle_request(request)
        content = response.read()
        # The content is decoded now: its encoding and length no longer apply.
        headers = {
            k: v
            for k, v in response.headers.items()
            if k.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
        }
        self.interactions.append(
            Interaction(
                method=request.method,
                url=_target(request),
                body=request.read().decode(),
                status=response.status_code,
                headers=headers,
                response=content.decode(),
            )
        )
        return httpx.Response(response.status_code, headers=headers, content=content)

    def cassette(self) -> Cassette:
        """What has been recorded so far, not yet scrubbed."""
        return Cassette(interactions=tuple(self.interactions))


def _scrub_text(text: str, instance_url: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    text = _SESSION_ID.sub(REDACTED, text)
    text = _SECRET_FIELD.sub(rf'\1"{REDACTED}"', text)
    host = urlsplit(instance_url).hostname
    if host:
        text = text.replace(host, EXAMPLE_HOST)
    text = _SALESFORCE_HOST.sub(EXAMPLE_HOST, text)
    return _ORG_ID.sub(lambda m: EXAMPLE_ORG_ID[: len(m[0])], text)


def scrub(cassette: Cassette, instance_url: str, secrets: Iterable[str] = ()) -> Cassette:
    """The cassette without credentials, session IDs, instance URLs or org IDs.

    ``secrets`` are values to remove wherever they appear, such as the access
    token the recording used.
    """
    known = tuple(secrets)

    def clean(text: str) -> str:
        return _scrub_text(text, instance_url, known)

    return Cassette(
        interactions=tuple(
            Interaction(
                method=i.method,
                url=clean(i.url),
                body=clean(i.body),
                status=i.status,
                headers={k: clean(v) for k, v in i.headers.items() if k.lower() in _KEPT_HEADERS},
                response=clean(i.response),
            )
            for i in cassette.interactions
        )
    )


class ReplayTransport(httpx.BaseTransport):
    """Answers requests from a cassette."""

    def __init__(self, cassette: Cassette) -> None:
        """Replay ``cassette``."""
        self.recorded: dict[tuple[str, str, str], deque[Interaction]] = {}
        for interaction in cassette.interactions:
            key = (interaction.method, interaction.url, interaction.body)
            self.recorded.setdefault(key, deque()).append(interaction)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """The recorded response to a request like this one."""
        key = (request.method, _target(request), request.read().decode())
        found = self.recorded.get(key)
        if not found:
            message = f"the cassette has no response to {request.method} {key[1]}"
            raise CassetteError(message)
        interaction = found.popleft() if len(found) > 1 else found[0]
        return httpx.Response(
            interaction.status, headers=interaction.headers, content=interaction.response.encode()
        )
