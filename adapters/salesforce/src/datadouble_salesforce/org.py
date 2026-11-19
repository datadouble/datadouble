# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The adapter's only contact with Salesforce: a narrow client of its REST APIs.

:class:`Org` is what the rest of the adapter uses: the objects an org has,
each object's fields, SOQL queries through the REST and Tooling APIs, and
exports through Bulk API 2.0. :class:`RestOrg` implements it over HTTP with
``httpx``; the transport it is given decides where requests go: the org
itself, the fake (:mod:`datadouble_salesforce.fake`) or a recorded cassette
(:mod:`datadouble_salesforce.cassettes`). Loading records into an org is not
offered yet.
"""

import csv
import io
import time
from collections.abc import Iterator
from typing import Protocol
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from datadouble.engine import EngineError

DEFAULT_API_VERSION = "62.0"
_PAGE = 50_000
"""Records per page of a Bulk API export's results."""


class _Model(BaseModel):
    # Salesforce's responses carry many more keys than the adapter reads.
    model_config = ConfigDict(strict=True, frozen=True, extra="ignore")


class ObjectSummary(_Model):
    """One object in the org's list of objects."""

    name: str
    custom: bool = False
    queryable: bool = False
    retrieveable: bool = False
    createable: bool = False
    replicateable: bool = False


class FieldDescription(_Model):
    """One field of an object."""

    name: str
    type: str
    nillable: bool = True
    reference_to: tuple[str, ...] = Field(default=(), alias="referenceTo")


class ObjectDescription(_Model):
    """An object's fields."""

    name: str
    fields: tuple[FieldDescription, ...]


class _Objects(_Model):
    sobjects: tuple[ObjectSummary, ...]


class _Version(_Model):
    version: str


class _Page(_Model):
    done: bool
    records: tuple[dict[str, JsonValue], ...]
    next_records_url: str | None = Field(default=None, alias="nextRecordsUrl")


class _Job(_Model):
    id: str
    state: str


class _Failure(_Model):
    error_code: str = Field(default="UNKNOWN", alias="errorCode")


_VERSIONS: TypeAdapter[tuple[_Version, ...]] = TypeAdapter(tuple[_Version, ...])
_FAILURES: TypeAdapter[tuple[_Failure, ...]] = TypeAdapter(tuple[_Failure, ...])

type Record = dict[str, JsonValue]


class Org(Protocol):
    """What the adapter asks of an org."""

    def versions(self) -> tuple[str, ...]:
        """The API versions the org offers."""
        ...

    def objects(self) -> tuple[ObjectSummary, ...]:
        """Every object in the org."""
        ...

    def describe(self, name: str) -> ObjectDescription:
        """One object's fields."""
        ...

    def query(self, soql: str) -> Iterator[Record]:
        """The records a REST API query returns, with their fields as JSON values."""
        ...

    def tooling_query(self, soql: str) -> Iterator[Record]:
        """The records a Tooling API query returns."""
        ...

    def export(self, soql: str) -> Iterator[dict[str, str]]:
        """The records a Bulk API 2.0 query returns, as text; empty text is null."""
        ...


def _failure(response: httpx.Response, action: str) -> EngineError:
    """An error naming Salesforce's error codes, not its messages, which can quote data."""
    try:
        codes = sorted(
            {failure.error_code for failure in _FAILURES.validate_json(response.content)}
        )
    except ValidationError:
        codes = []
    detail = f": {', '.join(codes)}" if codes else ""
    return EngineError(f"{action} failed with HTTP {response.status_code}{detail}")


class RestOrg:
    """An org reached over its REST APIs."""

    def __init__(
        self,
        client: httpx.Client,
        api_version: str = DEFAULT_API_VERSION,
        poll_seconds: float = 2.0,
    ) -> None:
        """Send requests through ``client``, whose base URL is the org's instance URL."""
        self.client = client
        self.api_version = api_version
        self.base = f"/services/data/v{api_version}"
        self.poll_seconds = poll_seconds

    def _send(self, method: str, url: str, action: str, json: JsonValue = None) -> httpx.Response:
        try:
            response = self.client.request(method, url, json=json)
        except httpx.HTTPError as error:
            message = f"{action} failed: {type(error).__name__}"
            raise EngineError(message) from None
        if not response.is_success:
            raise _failure(response, action)
        return response

    def versions(self) -> tuple[str, ...]:
        """The API versions the org offers."""
        response = self._send("GET", "/services/data/", "listing the API versions")
        return tuple(v.version for v in _VERSIONS.validate_json(response.content))

    def objects(self) -> tuple[ObjectSummary, ...]:
        """Every object in the org."""
        response = self._send("GET", f"{self.base}/sobjects/", "listing the objects")
        return _Objects.model_validate_json(response.content).sobjects

    def describe(self, name: str) -> ObjectDescription:
        """One object's fields."""
        response = self._send(
            "GET", f"{self.base}/sobjects/{quote(name)}/describe/", f"describing {name}"
        )
        return ObjectDescription.model_validate_json(response.content)

    def _pages(self, url: str, action: str) -> Iterator[Record]:
        next_url: str | None = url
        while next_url is not None:
            page = _Page.model_validate_json(self._send("GET", next_url, action).content)
            for record in page.records:
                yield {key: value for key, value in record.items() if key != "attributes"}
            next_url = None if page.done else page.next_records_url

    def query(self, soql: str) -> Iterator[Record]:
        """The records a REST API query returns."""
        return self._pages(f"{self.base}/query/?q={quote(soql)}", "querying")

    def tooling_query(self, soql: str) -> Iterator[Record]:
        """The records a Tooling API query returns."""
        return self._pages(
            f"{self.base}/tooling/query/?q={quote(soql)}", "querying the Tooling API"
        )

    def export(self, soql: str) -> Iterator[dict[str, str]]:
        """The records a Bulk API 2.0 query job returns, page by page."""
        jobs = f"{self.base}/jobs/query"
        created = self._send(
            "POST", jobs, "starting an export", {"operation": "query", "query": soql}
        )
        job = _Job.model_validate_json(created.content)
        while job.state not in {"JobComplete", "Failed", "Aborted"}:
            time.sleep(self.poll_seconds)
            polled = self._send("GET", f"{jobs}/{job.id}", "waiting for an export")
            job = _Job.model_validate_json(polled.content)
        if job.state != "JobComplete":
            message = f"the export ended {job.state}"
            raise EngineError(message)
        locator: str | None = ""
        while locator is not None:
            url = f"{jobs}/{job.id}/results?maxRecords={_PAGE}"
            if locator:
                url += f"&locator={quote(locator)}"
            response = self._send("GET", url, "reading an export")
            yield from csv.DictReader(io.StringIO(response.text))
            found = response.headers.get("Sforce-Locator", "null")
            locator = None if found in {"", "null"} else found
