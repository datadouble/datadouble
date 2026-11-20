# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Record a cassette of the adapter's calls to an org, scrubbed before it is written.

``make record-salesforce ORG=<alias>`` runs this with the JSON ``sf org
display --json`` and ``sf org auth show-access-token --json`` print for an org
you have logged in to with ``sf org login web``; the access token never leaves
this process. It writes the cassette to
standard output. ``--fake`` records against :func:`sample_org` instead, which
is how the committed cassette was first made. The calls are
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
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field

from datadouble.catalog import Catalog
from datadouble_salesforce.cassettes import RecordingTransport, scrub
from datadouble_salesforce.fake import FakeTransport, sample_org
from datadouble_salesforce.org import DEFAULT_API_VERSION, RestOrg
from datadouble_salesforce.source import ID, SalesforceSource


class _Login(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="ignore")

    access_token: str = Field(alias="accessToken")
    instance_url: str = Field(alias="instanceUrl")
    id: str = ""
    api_version: str = Field(default=DEFAULT_API_VERSION, alias="apiVersion")


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


def exercise(source: SalesforceSource) -> Exercised:
    """Make the calls a cassette records, and summarize what they returned."""
    source.server_version()
    catalog = source.catalog()
    queried: dict[str, int] = {}
    exported: dict[str, int] = {}
    for table in catalog.tables:
        queried[table.name.name] = sum(
            1 for _ in source.org.query(f"SELECT {ID} FROM {table.name}")
        )
        fields = ", ".join(column.name for column in table.columns)
        exported[table.name.name] = sum(
            1 for _ in source.org.export(f"SELECT {fields} FROM {table.name}")
        )
    return Exercised(catalog=catalog, queried=queried, exported=exported)


def record(
    transport: httpx.BaseTransport,
    instance_url: str,
    token: str,
    api_version: str,
    objects: tuple[str, ...],
) -> tuple[RecordingTransport, Exercised]:
    """Exercise an org through ``transport``, recording every interaction."""
    recording = RecordingTransport(transport)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    with httpx.Client(base_url=instance_url, headers=headers, transport=recording) as client:
        source = SalesforceSource(
            RestOrg(client, api_version, poll_seconds=0.0), api_version, objects
        )
        exercised = exercise(source)
    return recording, exercised


def main(arguments: Sequence[str] | None = None) -> int:
    """Record a cassette; the org's login is read from standard input unless ``--fake``."""
    parser = argparse.ArgumentParser(
        description="Record a cassette of the adapter's calls to an org."
    )
    parser.add_argument("--objects", default="Account,Contact,Opportunity,Case")
    parser.add_argument("--fake", action="store_true")
    options = parser.parse_args(arguments)
    objects = tuple(name for name in options.objects.split(",") if name)
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
            url, token, version = login.instance_url, login.access_token, login.api_version
            secrets = (login.access_token, login.id)
            transport = httpx.HTTPTransport(retries=2)
        recording, exercised = record(transport, url, token, version, objects)
        sys.stdout.write(scrub(recording.cassette(), url, (token, *secrets)).text())
        sys.stderr.write(
            f"recorded {len(recording.interactions)} interactions over"
            f" {len(exercised.catalog.tables)} objects\n"
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
