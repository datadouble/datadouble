# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Record a cassette of the adapter's calls to an org, scrubbed before it is written.

``make record-salesforce ORG=<alias>`` runs this with the JSON ``sf org
display --json`` and ``sf org auth show-access-token --json`` print for an org
you have logged in to with ``sf org login web``; the access token never leaves
this process. It writes the cassette to standard output. ``--fake`` records
against :func:`sample_org` instead. ``--hidden`` lists fields field-level
security may hide, and ``--catalog-only`` records only the API versions and
the catalog, even when the catalog fails, which is how the cassette of hidden
fields is made. The calls are
:func:`exercise`'s: the API versions, the catalog of the chosen objects with
the installed packages, and each object's records read both ways the adapter
reads them, a REST query and a Bulk API export.
"""

import argparse
import json
import sys
import tempfile
from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field

from datadouble.catalog import Catalog
from datadouble.engine import EngineError
from datadouble_salesforce.cassettes import RecordingTransport, scrub
from datadouble_salesforce.fake import FakeTransport, sample_org
from datadouble_salesforce.org import DEFAULT_API_VERSION, RestOrg
from datadouble_salesforce.source import ID, SalesforceSource


class _Login(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="ignore")

    access_token: str = Field(alias="accessToken")
    instance_url: str = Field(alias="instanceUrl")
    id: str = ""


def _login(text: str) -> _Login:
    """The login from ``sf`` commands' JSON, one document after another.

    ``sf org display`` redacts the access token, which ``sf org auth
    show-access-token`` prints; later documents' fields win.
    """
    decoder = json.JSONDecoder()
    merged: dict[str, object] = {}
    position = 0
    text = text.strip()
    while position < len(text):
        document, position = decoder.raw_decode(text, position)
        merged |= document["result"]
        position = len(text) - len(text[position:].lstrip())
    return _Login.model_validate(merged)


class Exercised(BaseModel):
    """What :func:`exercise` read: the catalog and each object's record count, both ways."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    catalog: Catalog
    queried: dict[str, int]
    exported: dict[str, int]


def exercise(source: SalesforceSource, *, catalog_only: bool = False) -> Exercised:
    """Make the calls a cassette records, and summarize what they returned.

    With ``catalog_only``, only the API versions and the catalog are read.
    """
    source.server_version()
    catalog = source.catalog()
    queried: dict[str, int] = {}
    exported: dict[str, int] = {}
    for table in () if catalog_only else catalog.tables:
        queried[table.name.name] = sum(
            1 for _ in source.org.query(f"SELECT {ID} FROM {table.name}")
        )
        fields = ", ".join(column.name for column in table.columns)
        exported[table.name.name] = sum(
            1 for _ in source.org.export(f"SELECT {fields} FROM {table.name}")
        )
    return Exercised(catalog=catalog, queried=queried, exported=exported)


@dataclass(frozen=True, slots=True)
class Target:
    """The org a recording reads: its instance URL, access token and API version."""

    instance_url: str
    token: str
    api_version: str


def record(
    transport: httpx.BaseTransport,
    target: Target,
    objects: tuple[str, ...],
    hidden: frozenset[str] = frozenset(),
    *,
    catalog_only: bool = False,
) -> tuple[RecordingTransport, Exercised | EngineError]:
    """Exercise an org through ``transport``, recording every interaction.

    With ``catalog_only``, a catalog that fails is recorded too, and its
    error returned, since the cassette of a failing catalog is the point.
    """
    recording = RecordingTransport(transport)
    headers = {"Authorization": f"Bearer {target.token}", "Accept": "application/json"}
    with httpx.Client(base_url=target.instance_url, headers=headers, transport=recording) as client:
        org = RestOrg(client, target.api_version, poll_seconds=0.0)
        source = SalesforceSource(org, target.api_version, objects, hidden)
        try:
            exercised: Exercised | EngineError = exercise(source, catalog_only=catalog_only)
        except EngineError as error:
            if not catalog_only:
                raise
            exercised = error
    return recording, exercised


def main(arguments: Sequence[str] | None = None) -> int:
    """Record a cassette; the org's login is read from standard input unless ``--fake``."""
    parser = argparse.ArgumentParser(
        description="Record a cassette of the adapter's calls to an org."
    )
    parser.add_argument("--objects", default="Account,Contact,Opportunity,Case")
    parser.add_argument("--fake", action="store_true")
    parser.add_argument("--hidden", default="", help="Object.Field names, comma-separated")
    parser.add_argument("--catalog-only", action="store_true")
    options = parser.parse_args(arguments)
    objects = tuple(name for name in options.objects.split(",") if name)
    hidden = frozenset(name for name in options.hidden.split(",") if name)
    with ExitStack() as stack:
        secrets: tuple[str, ...]
        if options.fake:
            url, token, version, secrets = (
                "https://fake.example.com",
                "fake",
                DEFAULT_API_VERSION,
                (),
            )
            scratch = stack.enter_context(tempfile.TemporaryDirectory())
            transport: httpx.BaseTransport = FakeTransport(sample_org(Path(scratch) / "org.sqlite"))
        else:
            try:
                login = _login(sys.stdin.read())
            except (ValueError, LookupError, TypeError):
                # Its message would quote the input, which can hold the token.
                sys.stderr.write(
                    "expected what `sf org display --json` and"
                    " `sf org auth show-access-token --json` print for a logged-in org\n"
                )
                return 2
            # The adapter's own API version, not the org's newest, so replay matches.
            url, token, version = login.instance_url, login.access_token, DEFAULT_API_VERSION
            secrets = (login.access_token, login.id)
            transport = httpx.HTTPTransport(retries=2)
        recording, exercised = record(
            transport,
            Target(url, token, version),
            objects,
            hidden,
            catalog_only=options.catalog_only,
        )
        sys.stdout.write(scrub(recording.cassette(), url, (token, *secrets)).text())
        if isinstance(exercised, EngineError):
            sys.stderr.write(f"recorded the catalog failing: {exercised}\n")
        else:
            sys.stderr.write(
                f"recorded {len(recording.interactions)} interactions over"
                f" {len(exercised.catalog.tables)} objects\n"
            )
        return 0


if __name__ == "__main__":
    sys.exit(main())
