# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Record a cassette of the adapter's calls to an org, scrubbed before it is written.

``make record-salesforce ORG=<alias>`` runs this with the JSON ``sf org
display --json`` prints for an org you have logged in to with ``sf org login
web``; the access token never leaves this process. ``--fake PATH`` records
against a fake org instead, which is how the committed cassette was first
made; both paths must be under the current directory. The calls are :func:`exercise`'s: the API versions, the catalog of the
chosen objects with the installed packages, and each object's records read
both ways the adapter reads them, a REST query and a Bulk API export.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from datadouble.catalog import Catalog
from datadouble_salesforce.cassettes import RecordingTransport, scrub
from datadouble_salesforce.fake import FakeOrg, FakeTransport
from datadouble_salesforce.org import DEFAULT_API_VERSION, RestOrg
from datadouble_salesforce.source import ID, SalesforceSource


class _Login(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="ignore")

    access_token: str = Field(alias="accessToken")
    instance_url: str = Field(alias="instanceUrl")
    id: str = ""
    api_version: str = Field(default=DEFAULT_API_VERSION, alias="apiVersion")


class _Display(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="ignore")

    result: _Login


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


def _here(name: str) -> Path:
    """A path under the current directory, the only place the recorder reads or writes."""
    base = Path.cwd().resolve()
    path = (base / name).resolve()
    if not path.is_relative_to(base):
        message = f"{name} is outside the current directory"
        raise argparse.ArgumentTypeError(message)
    return path


def main(arguments: Sequence[str] | None = None) -> int:
    """Record a cassette; the org's login is read from standard input unless ``--fake``."""
    parser = argparse.ArgumentParser(
        description="Record a cassette of the adapter's calls to an org."
    )
    parser.add_argument("cassette", type=_here)
    parser.add_argument("--objects", default="Account,Contact,Opportunity,Case")
    parser.add_argument("--fake", type=_here)
    options = parser.parse_args(arguments)
    objects = tuple(name for name in options.objects.split(",") if name)
    secrets: tuple[str, ...]
    if options.fake is not None:
        url, token, version, secrets = "https://fake.example.com", "fake", DEFAULT_API_VERSION, ()
        transport: httpx.BaseTransport = FakeTransport(FakeOrg(options.fake))
    else:
        try:
            login = _Display.model_validate_json(sys.stdin.read()).result
        except ValidationError:
            # Its message would quote the input, which can hold the token.
            sys.stderr.write("expected what `sf org display --json` prints for a logged-in org\n")
            return 2
        url, token, version = login.instance_url, login.access_token, login.api_version
        secrets = (login.access_token, login.id)
        transport = httpx.HTTPTransport(retries=2)
    recording, exercised = record(transport, url, token, version, objects)
    scrub(recording.cassette(), url, (token, *secrets)).write(options.cassette)
    sys.stdout.write(
        f"recorded {len(recording.interactions)} interactions over"
        f" {len(exercised.catalog.tables)} objects to {options.cassette}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
