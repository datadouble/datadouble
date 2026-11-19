# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""A fake org: a SQLite file, served over the REST APIs the adapter uses.

A spec reaches it with ``connection: {fake: /path/to/org.sqlite}``, so tests,
demonstrations and the conformance suite run the whole adapter, its HTTP
client included, without an org. :class:`FakeOrg` builds the file: objects and
their fields, records, and installed packages. :class:`FakeTransport` answers
``httpx`` requests from it, as Salesforce would: the API versions, the object
list, describes, REST and Tooling queries with paging, and Bulk API 2.0 query
jobs, whose status reads ``InProgress`` once before ``JobComplete``.

Queries go through :func:`datadouble_salesforce.soql.translate`, so the fake
answers the SOQL that module translates. Each request opens the file afresh,
so records another connection adds are seen at once: like an org, the fake
has no snapshots.
"""

import csv
import datetime as dt
import io
import json
import re
import sqlite3
import uuid
from collections.abc import Generator, Iterable, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from pydantic import JsonValue

from datadouble_salesforce.soql import SoqlError, Translated, Value, datetime_literal, translate

VERSIONS = ("60.0", "61.0", "62.0", "63.0", "64.0")
_PAGE = 2000
_BOOLEAN = {"boolean"}
_INTEGER = {"int", "long"}
_NUMBER = {"double", "currency", "percent"}
_PREFIXES = {"Account": "001", "Contact": "003", "Opportunity": "006", "User": "005", "Case": "500"}
_VERSION_PATH = re.compile(r"^/services/data/v(?P<version>\d+\.\d+)(?P<rest>/.*)$")


@dataclass(frozen=True, slots=True)
class FakeField:
    """A field of a fake object, with its Salesforce type."""

    name: str
    type: str = "string"
    nillable: bool = True
    reference_to: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Object:
    name: str
    prefix: str
    custom: bool
    queryable: bool
    createable: bool
    fields: tuple[FakeField, ...]


def _affinity(field_type: str) -> str:
    if field_type in _BOOLEAN | _INTEGER:
        return "INTEGER"
    return "REAL" if field_type in _NUMBER else "TEXT"


class FakeOrg:
    """A fake org's file: create it, then add objects, records and packages."""

    def __init__(self, path: Path) -> None:
        """Open the fake at ``path``, creating its tables if they are not there."""
        self.path = path
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS _objects (
                    name TEXT PRIMARY KEY COLLATE NOCASE, prefix TEXT, custom INTEGER,
                    queryable INTEGER, createable INTEGER);
                CREATE TABLE IF NOT EXISTS _fields (
                    object TEXT COLLATE NOCASE, name TEXT, type TEXT, nillable INTEGER,
                    reference_to TEXT, position INTEGER);
                CREATE TABLE IF NOT EXISTS _packages (namespace TEXT, name TEXT, version TEXT);
                """
            )

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            yield connection

    def add_object(
        self,
        name: str,
        fields: Iterable[FakeField] = (),
        *,
        custom: bool = False,
        queryable: bool = True,
        createable: bool = True,
    ) -> None:
        """Add an object, with an ``Id`` field before ``fields``."""
        with self._connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM _objects").fetchone()[0]
            prefix = _PREFIXES.get(name, f"a{count:02d}")
            connection.execute(
                "INSERT INTO _objects VALUES (?, ?, ?, ?, ?)",
                (name, prefix, custom, queryable, createable),
            )
            connection.execute(f'CREATE TABLE "{name}" ("Id" TEXT PRIMARY KEY)')
        for added in (FakeField("Id", "id", nillable=False), *fields):
            self.add_field(name, added)

    def add_field(self, object_name: str, added: FakeField) -> None:
        """Add a field to an object."""
        with self._connect() as connection:
            position = connection.execute(
                "SELECT COUNT(*) FROM _fields WHERE object = ?", (object_name,)
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO _fields VALUES (?, ?, ?, ?, ?, ?)",
                (
                    object_name,
                    added.name,
                    added.type,
                    added.nillable,
                    ",".join(added.reference_to),
                    position,
                ),
            )
            if added.name != "Id":
                affinity = _affinity(added.type)
                connection.execute(f'ALTER TABLE "{object_name}" ADD "{added.name}" {affinity}')

    def insert(self, object_name: str, record: Mapping[str, Value | dt.datetime]) -> str:
        """Insert a record, and return its Id, which is made up unless ``record`` has one."""
        found = self.object(object_name)
        values = {key: _stored(value) for key, value in record.items()}
        with self._connect() as connection:
            if "Id" not in values:
                number = connection.execute(f'SELECT COUNT(*) FROM "{found.name}"').fetchone()[0]
                values = {"Id": f"{found.prefix}{number + 1:012d}AAA", **values}
            columns = ", ".join(f'"{key}"' for key in values)
            marks = ", ".join("?" for _ in values)
            connection.execute(
                f'INSERT INTO "{found.name}" ({columns}) VALUES ({marks})', tuple(values.values())
            )
        return str(values["Id"])

    def add_package(self, namespace: str, name: str, version: str) -> None:
        """Record an installed package."""
        with self._connect() as connection:
            connection.execute("INSERT INTO _packages VALUES (?, ?, ?)", (namespace, name, version))

    def objects(self) -> list[_Object]:
        """Every object, with its fields."""
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM _objects ORDER BY name").fetchall()
            fields = connection.execute(
                "SELECT object, name, type, nillable, reference_to FROM _fields ORDER BY position"
            ).fetchall()
        by_object: dict[str, list[FakeField]] = {}
        for object_name, name, field_type, nillable, reference_to in fields:
            by_object.setdefault(str(object_name).lower(), []).append(
                FakeField(
                    str(name),
                    str(field_type),
                    bool(nillable),
                    tuple(str(reference_to).split(",")) if reference_to else (),
                )
            )
        return [
            _Object(
                name=str(name),
                prefix=str(prefix),
                custom=bool(custom),
                queryable=bool(queryable),
                createable=bool(createable),
                fields=tuple(by_object.get(str(name).lower(), [])),
            )
            for name, prefix, custom, queryable, createable in rows
        ]

    def object(self, name: str) -> _Object:
        """The object called ``name``, ignoring case; KeyError if there is none."""
        found = next((o for o in self.objects() if o.name.lower() == name.lower()), None)
        if found is None:
            raise KeyError(name)
        return found

    def run(self, query: str) -> tuple[tuple[str, ...], list[dict[str, JsonValue]]]:
        """The fields a SOQL query selects, and the records it returns, typed as JSON."""
        objects = {o.name.lower(): o for o in self.objects()}

        def fields(name: str) -> Mapping[str, str] | None:
            found = objects.get(name.lower())
            return None if found is None else {f.name.lower(): f.name for f in found.fields}

        translated: Translated = translate(query, fields)
        queried = objects[translated.object.lower()]
        types = {f.name: f.type for f in queried.fields}
        with self._connect() as connection:
            rows = connection.execute(translated.sql, translated.parameters).fetchall()
        return translated.fields, [
            {
                name: _json(value, types[name])
                for name, value in zip(translated.fields, row, strict=True)
            }
            for row in rows
        ]

    def packages(self) -> list[tuple[str, str, str]]:
        """The installed packages: namespace, name and version."""
        with self._connect() as connection:
            return [
                (str(a), str(b), str(c))
                for a, b, c in connection.execute("SELECT * FROM _packages ORDER BY namespace")
            ]


def _stored(value: Value | dt.datetime) -> Value:
    return datetime_literal(value) if isinstance(value, dt.datetime) else value


def _json(value: object, field_type: str) -> JsonValue:
    if value is None:
        return None
    if field_type in _BOOLEAN:
        return bool(value)
    if field_type in _INTEGER:
        return int(str(value))
    if field_type in _NUMBER:
        return float(str(value))
    return str(value)


def _text(value: JsonValue) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _failure(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json=[{"errorCode": code, "message": code}])


@dataclass
class _Job:
    rows: list[dict[str, JsonValue]]
    fields: tuple[str, ...]
    polls: int = 0


@dataclass
class FakeTransport(httpx.BaseTransport):
    """Answers the adapter's requests from a :class:`FakeOrg`."""

    org: FakeOrg
    page: int = _PAGE
    _cursors: dict[str, list[dict[str, JsonValue]]] = field(
        default_factory=dict[str, list[dict[str, JsonValue]]]
    )
    _jobs: dict[str, _Job] = field(default_factory=dict[str, _Job])

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Answer one request."""
        if not request.headers.get("Authorization", "").startswith("Bearer "):
            return _failure(401, "INVALID_SESSION_ID")
        path = urlsplit(str(request.url)).path
        if path == "/services/data/":
            return httpx.Response(
                200, json=[{"version": v, "url": f"/services/data/v{v}"} for v in VERSIONS]
            )
        matched = _VERSION_PATH.match(path)
        if matched is None:
            return _failure(404, "NOT_FOUND")
        try:
            return self._answer(request, matched["version"], matched["rest"])
        except SoqlError:
            return _failure(400, "MALFORMED_QUERY")
        except KeyError:
            return _failure(404, "NOT_FOUND")

    def _answer(self, request: httpx.Request, version: str, rest: str) -> httpx.Response:
        query = {key: values[0] for key, values in parse_qs(request.url.query.decode()).items()}
        answer: JsonValue = None
        match request.method, rest.strip("/").split("/"):
            case "GET", ["sobjects"]:
                answer = {"sobjects": self._summaries()}
            case "GET", ["sobjects", name, "describe"]:
                answer = self._describe(name)
            case "GET", ["query"]:
                _, rows = self.org.run(query["q"])
                answer = self._page(version, str(uuid.uuid4()), rows, 0)
            case "GET", ["query", cursor]:
                key, offset = cursor.rsplit("-", 1)
                answer = self._page(version, key, self._cursors[key], int(offset))
            case "GET", ["tooling", "query"]:
                if "installedsubscriberpackage" not in query["q"].lower():
                    return _failure(400, "INVALID_TYPE")
                answer = self._packages()
            case "POST", ["jobs", "query"]:
                body = json.loads(request.content)
                fields, rows = self.org.run(str(body["query"]))
                job = str(uuid.uuid4())
                self._jobs[job] = _Job(rows, fields)
                answer = {"id": job, "state": "UploadComplete"}
            case "GET", ["jobs", "query", job]:
                found = self._jobs[job]
                found.polls += 1
                answer = {"id": job, "state": "InProgress" if found.polls == 1 else "JobComplete"}
            case "GET", ["jobs", "query", job, "results"]:
                return self._results(self._jobs[job], query)
            case _:
                raise KeyError(rest)
        return httpx.Response(200, json=answer)

    def _summaries(self) -> list[JsonValue]:
        return [
            {
                "name": o.name,
                "custom": o.custom,
                "queryable": o.queryable,
                "retrieveable": o.queryable,
                "createable": o.createable,
                "replicateable": o.createable,
                "keyPrefix": o.prefix,
            }
            for o in self.org.objects()
        ]

    def _describe(self, name: str) -> JsonValue:
        found = self.org.object(name)
        return {
            "name": found.name,
            "fields": [
                {
                    "name": f.name,
                    "type": f.type,
                    "nillable": f.nillable,
                    "referenceTo": list(f.reference_to),
                }
                for f in found.fields
            ],
        }

    def _page(
        self, version: str, key: str, rows: list[dict[str, JsonValue]], offset: int
    ) -> JsonValue:
        end = offset + self.page
        done = end >= len(rows)
        body: dict[str, JsonValue] = {
            "totalSize": len(rows),
            "done": done,
            "records": [{"attributes": {"type": "record"}, **row} for row in rows[offset:end]],
        }
        if not done:
            self._cursors[key] = rows
            body["nextRecordsUrl"] = f"/services/data/v{version}/query/{key}-{end}"
        return body

    def _packages(self) -> JsonValue:
        records: list[JsonValue] = []
        for namespace, name, version in self.org.packages():
            major, minor, patch = [*version.split("."), "0", "0"][:3]
            records.append(
                {
                    "SubscriberPackage": {"NamespacePrefix": namespace or None, "Name": name},
                    "SubscriberPackageVersion": {
                        "MajorVersion": int(major),
                        "MinorVersion": int(minor),
                        "PatchVersion": int(patch),
                    },
                }
            )
        return {"totalSize": len(records), "done": True, "records": records}

    def _results(self, job: _Job, query: Mapping[str, str]) -> httpx.Response:
        start = int(query.get("locator") or 0)
        end = start + min(int(query.get("maxRecords", self.page)), self.page)
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(job.fields)
        for row in job.rows[start:end]:
            writer.writerow([_text(row[name]) for name in job.fields])
        locator = str(end) if end < len(job.rows) else "null"
        return httpx.Response(
            200,
            text=out.getvalue(),
            headers={"Content-Type": "text/csv", "Sforce-Locator": locator},
        )


_INDUSTRIES = ("Technology", "Retail", "Energy")
_STAGES = ("Prospecting", "Negotiation", "Closed Won")
_FIRST = ("Ada", "Grace", "Alan", "Edsger", "Barbara", "Donald", "Frances", "Ken")
_LAST = ("Lovelace", "Hopper", "Turing", "Dijkstra", "Liskov", "Knuth", "Allen", "Thompson")


def sample_org(path: Path) -> FakeOrg:
    """A small fake org with made-up records, for tests and demonstrations.

    Accounts (some with parent accounts), their contacts, opportunities and
    cases; tasks whose references are polymorphic; leads; history and
    non-queryable objects the catalog leaves out; and a managed package,
    ``dd_ratings``, whose object rates accounts. Phone numbers are in the
    fictional 555-01xx range and email addresses at example.com.
    """
    org = FakeOrg(path)
    created = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
    org.add_object(
        "Account",
        (
            FakeField("Name", nillable=False),
            FakeField("Industry", "picklist"),
            FakeField("Phone", "phone"),
            FakeField("Website", "url"),
            FakeField("AnnualRevenue", "currency"),
            FakeField("NumberOfEmployees", "int"),
            FakeField("BillingAddress", "address"),
            FakeField("BillingCity"),
            FakeField("ParentId", "reference", reference_to=("Account",)),
            FakeField("CreatedDate", "datetime", nillable=False),
        ),
    )
    org.add_object(
        "Contact",
        (
            FakeField("AccountId", "reference", reference_to=("Account",)),
            FakeField("FirstName"),
            FakeField("LastName", nillable=False),
            FakeField("Email", "email"),
            FakeField("Phone", "phone"),
            FakeField("Birthdate", "date"),
            FakeField("HasOptedOutOfEmail", "boolean", nillable=False),
            FakeField("CreatedDate", "datetime", nillable=False),
        ),
    )
    org.add_object(
        "Opportunity",
        (
            FakeField("AccountId", "reference", reference_to=("Account",)),
            FakeField("Name", nillable=False),
            FakeField("StageName", "picklist", nillable=False),
            FakeField("Amount", "currency"),
            FakeField("Probability", "percent"),
            FakeField("CloseDate", "date", nillable=False),
        ),
    )
    org.add_object(
        "Case",
        (
            FakeField("AccountId", "reference", reference_to=("Account",)),
            FakeField("ContactId", "reference", reference_to=("Contact",)),
            FakeField("Subject"),
            FakeField("Status", "picklist"),
            FakeField("Description", "textarea"),
        ),
    )
    org.add_object(
        "Task",
        (
            FakeField("WhoId", "reference", reference_to=("Contact", "Lead")),
            FakeField("WhatId", "reference", reference_to=("Account", "Opportunity")),
            FakeField("Subject"),
        ),
    )
    org.add_object(
        "Lead",
        (
            FakeField("FirstName"),
            FakeField("LastName", nillable=False),
            FakeField("Email", "email"),
            FakeField("Company", nillable=False),
        ),
    )
    org.add_object(
        "dd_ratings__Rating__c",
        (
            FakeField("dd_ratings__Account__c", "reference", reference_to=("Account",)),
            FakeField("dd_ratings__Score__c", "double"),
        ),
        custom=True,
    )
    org.add_object("AccountHistory", (FakeField("AccountId", "reference"),), createable=False)
    org.add_object("AppMenuItem", (FakeField("Name"),), queryable=False)
    org.add_package("dd_ratings", "Datadouble Ratings", "1.4.0")
    accounts: list[str] = []
    for number in range(30):
        accounts.append(
            org.insert(
                "Account",
                {
                    "Name": f"Account {number:02d}",
                    "Industry": _INDUSTRIES[number % 3],
                    "Phone": f"212-555-01{number % 100:02d}",
                    "Website": f"https://account{number:02d}.example.com",
                    "AnnualRevenue": 10_000.5 * number,
                    "NumberOfEmployees": 10 * number,
                    "BillingCity": "Springfield",
                    "ParentId": accounts[number // 4 - 1] if number and number % 4 == 0 else None,
                    "CreatedDate": created + dt.timedelta(days=number),
                },
            )
        )
    contacts: list[str] = []
    for number in range(60):
        first, last = _FIRST[number % 8], _LAST[(number // 8) % 8]
        contacts.append(
            org.insert(
                "Contact",
                {
                    "AccountId": accounts[number % 30] if number % 10 else None,
                    "FirstName": first,
                    "LastName": last,
                    "Email": f"{first}.{last}{number}@example.com".lower(),
                    "Phone": f"312-555-01{number % 100:02d}",
                    "Birthdate": f"19{60 + number % 30}-0{1 + number % 9}-1{number % 10}",
                    "HasOptedOutOfEmail": number % 3 == 0,
                    "CreatedDate": created + dt.timedelta(hours=number),
                },
            )
        )
    opportunities = [
        org.insert(
            "Opportunity",
            {
                "AccountId": accounts[(number * 7) % 30],
                "Name": f"Deal {number:02d}",
                "StageName": _STAGES[number % 3],
                "Amount": 1500.25 * (number + 1),
                "Probability": 10.0 * (number % 10),
                "CloseDate": f"2025-0{1 + number % 9}-15",
            },
        )
        for number in range(40)
    ]
    for number in range(25):
        org.insert(
            "Case",
            {
                "AccountId": accounts[(number * 3) % 30],
                "ContactId": contacts[number * 2],
                "Subject": f"Question {number:02d}",
                "Status": "New" if number % 2 else "Closed",
                "Description": "The customer asked about an invoice.",
            },
        )
    leads = [
        org.insert(
            "Lead",
            {
                "FirstName": _FIRST[n % 8],
                "LastName": _LAST[n % 8],
                "Email": f"lead{n}@example.com",
                "Company": f"Prospect {n:02d}",
            },
        )
        for n in range(10)
    ]
    for number in range(20):
        org.insert(
            "Task",
            {
                "WhoId": contacts[number] if number % 2 else leads[number % 10],
                "WhatId": opportunities[number] if number % 3 else accounts[number],
                "Subject": f"Call {number:02d}",
            },
        )
    for number in range(15):
        org.insert(
            "dd_ratings__Rating__c",
            {"dd_ratings__Account__c": accounts[number * 2], "dd_ratings__Score__c": number / 3},
        )
    return org
