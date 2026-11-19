# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The Salesforce adapter's parts, against the fake org and recorded cassettes."""

import datetime as dt
import io
import json
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

from datadouble.catalog import Catalog, Column, ColumnKind, Table
from datadouble.engine import EngineError, Tooling, Transformation
from datadouble.names import ColumnName, ColumnRef, EngineName, GroupName, TableName
from datadouble.planner import Fixup, Plan, PlannedGroup, PlannedRule, PlanOptions, plan
from datadouble.registry import installed
from datadouble.settings import SettingsError
from datadouble.spec import (
    ExpressionRule,
    Hook,
    ImageFormat,
    NullOptions,
    NullRule,
    StrictLevel,
    TransformHook,
    parse_spec,
)
from datadouble.subset import (
    AllOf,
    AllRows,
    AnyNull,
    AnyOf,
    Condition,
    Edge,
    EdgeMode,
    NoRows,
    Predicate,
    SelectedBy,
    ValuesIn,
)
from datadouble_salesforce import FAKE_URL, SalesforceEngine
from datadouble_salesforce import engine as engine_entry
from datadouble_salesforce import record as recorder
from datadouble_salesforce import source as source_module
from datadouble_salesforce.cassettes import (
    EXAMPLE_HOST,
    EXAMPLE_ORG_ID,
    Cassette,
    CassetteError,
    Interaction,
    RecordingTransport,
    ReplayTransport,
    scrub,
)
from datadouble_salesforce.fake import FakeField, FakeOrg, FakeTransport, sample_org
from datadouble_salesforce.org import ObjectSummary, RestOrg
from datadouble_salesforce.soql import (
    SoqlError,
    datetime_literal,
    literal,
    translate,
    where,
    wheres,
)
from datadouble_salesforce.source import (
    SalesforceSource,
    StoredColumn,
    included,
    kind,
    typed,
)
from datadouble_salesforce.working import SalesforceWorking

if TYPE_CHECKING:
    from pydantic import JsonValue

CASSETTE = Path(__file__).parent / "cassettes" / "org.json"
AS_OF = dt.datetime(2026, 1, 2, 3, 4, 5, 6000, tzinfo=dt.UTC)
ACCOUNT = TableName.of("", "Account")
CONTACT = TableName.of("", "Contact")


@contextmanager
def _org(path: Path, transport: httpx.BaseTransport | None = None) -> Generator[RestOrg]:
    headers = {"Authorization": "Bearer fake"}
    used = transport or FakeTransport(FakeOrg(path))
    with httpx.Client(base_url=FAKE_URL, headers=headers, transport=used) as client:
        yield RestOrg(client, poll_seconds=0.0)


@pytest.fixture
def org(tmp_path: Path) -> Iterator[RestOrg]:
    """A client of the sample fake org."""
    sample_org(tmp_path / "org.sqlite")
    with _org(tmp_path / "org.sqlite") as client:
        yield client


# SOQL


def test_literals() -> None:
    assert literal(None) == "null"
    assert literal(value=True) == "true"
    assert literal(value=False) == "false"
    assert literal(7) == "7"
    assert literal(1.5) == "1.500000"
    assert literal("O'Brien \\ co") == "'O\\'Brien \\\\ co'"
    assert datetime_literal(AS_OF) == "2026-01-02T03:04:05.006Z"


def test_conditions_render_as_soql() -> None:
    assert where(AllRows(), AS_OF) is None
    assert where(NoRows(), AS_OF) == "Id = null"
    assert (
        where(Predicate("CreatedDate <= :as_of"), AS_OF)
        == "CreatedDate <= 2026-01-02T03:04:05.006Z"
    )
    assert where(ValuesIn((ColumnName("Id"),), frozenset()), AS_OF) == "Id = null"
    assert (
        where(ValuesIn((ColumnName("Id"),), frozenset({("b",), ("a",)})), AS_OF)
        == "Id IN ('a', 'b')"
    )
    pair = ValuesIn((ColumnName("A"), ColumnName("B")), frozenset({(1, None), (2, True)}))
    assert where(pair, AS_OF) == "(A = 1 AND B = null) OR (A = 2 AND B = true)"
    assert where(AnyNull((ColumnName("A"), ColumnName("B"))), AS_OF) == "A = null OR B = null"
    assert where(AnyOf((Predicate("A = 1"), AllRows())), AS_OF) is None
    assert where(AnyOf((Predicate("A = 1"), Predicate("B = 2"))), AS_OF) == "(A = 1) OR (B = 2)"
    assert where(AllOf((Predicate("A = 1"), AllRows())), AS_OF) == "(A = 1)"
    assert where(AllOf(()), AS_OF) is None
    assert where(AnyOf(()), AS_OF) == "Id = null"
    with pytest.raises(EngineError, match="nested selections"):
        where(SelectedBy((ColumnName("A"),), ACCOUNT, (ColumnName("Id"),)), AS_OF)
    with pytest.raises(EngineError, match="no SOQL literal"):
        where(ValuesIn((ColumnName("Id"),), frozenset({(b"x",)})), AS_OF)


def test_long_conditions_are_split() -> None:
    values = frozenset((f"{n:018d}",) for n in range(100))
    condition = AllOf(
        (Predicate("A = 1"), AnyOf((ValuesIn((ColumnName("Id"),), values), NoRows())))
    )
    parts = wheres(condition, AS_OF, 1000)
    assert len(parts) > 1
    assert all(part is not None and len(part) <= 1000 for part in parts)
    assert sum(str(part).count("'") for part in parts) == 200
    assert wheres(AllRows(), AS_OF, 10) == [None]
    with pytest.raises(EngineError, match="longer than SOQL allows"):
        wheres(Predicate("A = 1 AND B = 2"), AS_OF, 5)
    with pytest.raises(EngineError, match="longer than SOQL allows"):
        wheres(ValuesIn((ColumnName("Id"),), frozenset({("abcdef",)})), AS_OF, 5)


FIELDS = {"account": {"id": "Id", "name": "Name", "score": "Score", "flag": "Flag"}}


def _translate(query: str) -> tuple[str, tuple[object, ...]]:
    translated = translate(query, lambda name: FIELDS.get(name.lower()))
    return translated.sql, translated.parameters


def test_soql_translates_to_sqlite() -> None:
    sql, parameters = _translate(
        "select id, NAME from Account where (Name = 'a\\'b' or Name != null) and not Score < 1.5"
        " and Flag = TRUE and Name like 'x%' and Id in ('1', '2') and Id not in (3)"
        " order by Name desc, Id limit 5"
    )
    assert sql == (
        'SELECT "Id", "Name" FROM "Account" WHERE ("Name" = ? COLLATE NOCASE OR "Name" IS NOT NULL)'
        ' AND NOT ("Score" < ?) AND "Flag" = ? AND "Name" LIKE ? ESCAPE \'\\\' AND'
        ' "Id" COLLATE NOCASE IN (?, ?) AND "Id" NOT IN (?) ORDER BY "Name" DESC, "Id" LIMIT 5'
    )
    assert parameters == ("a'b", 1.5, 1, "x%", "1", "2", 3)
    _, parameters = _translate(
        "SELECT Id FROM Account WHERE Name = null AND Name > 2026-01-02T03:04:05+01:00"
        " AND Name < 2026-01-02 AND Name = 'tab\\there' AND Flag = false"
    )
    assert parameters == ("2026-01-02T02:04:05.000Z", "2026-01-02", "tab\there", 0)


@pytest.mark.parametrize(
    ("query", "problem"),
    [
        ("SELECT Id FROM Account WHERE Name = ~", "at character"),
        ("SELECT Id FROM Nothing", "no object Nothing"),
        ("SELECT Owner.Name FROM Account", "relationship fields"),
        ("SELECT Missing FROM Account", "no field Missing"),
        ("SELECT Id FROM Account WHERE Name = )", "expected a value"),
        ("SELECT Id FROM Account WHERE Name NOT = 'x'", "NOT must come before"),
        ("SELECT Id FROM Account WHERE Name < null", "null compares only"),
        ("SELECT Id FROM Account LIMIT 5 extra", "unexpected text"),
        ("SELECT Id FROM Account WHERE", "expected name"),
        ("SELECT FROM Account", "expected FROM"),
    ],
)
def test_soql_outside_the_subset(query: str, problem: str) -> None:
    with pytest.raises(SoqlError, match=problem):
        _translate(query)


# The fake org and the client


def test_the_client_reads_the_fake(org: RestOrg) -> None:
    assert "62.0" in org.versions()
    names = {summary.name for summary in org.objects()}
    assert {"Account", "AccountHistory", "AppMenuItem"} <= names
    contact = org.describe("Contact")
    assert {f.name for f in contact.fields} >= {"Id", "AccountId", "Email"}
    rows = list(org.query("SELECT Id, HasOptedOutOfEmail FROM Contact"))
    assert len(rows) == 60
    assert {type(row["HasOptedOutOfEmail"]) for row in rows} == {bool}
    exported = list(org.export("SELECT Id, Phone FROM Contact WHERE Phone = null"))
    assert exported == []


def test_queries_and_exports_page(tmp_path: Path) -> None:
    sample_org(tmp_path / "org.sqlite")
    transport = FakeTransport(FakeOrg(tmp_path / "org.sqlite"), page=7)
    with _org(tmp_path / "org.sqlite", transport) as client:
        assert len(list(client.query("SELECT Id FROM Contact"))) == 60
        exported = list(client.export("SELECT Id, AnnualRevenue FROM Account"))
    assert len(exported) == 30
    assert exported[1]["AnnualRevenue"] == "10000.5"


def test_the_fake_fails_as_salesforce_does(tmp_path: Path, org: RestOrg) -> None:
    with pytest.raises(EngineError, match="describing Nothing failed with HTTP 404: NOT_FOUND"):
        org.describe("Nothing")
    with pytest.raises(EngineError, match="HTTP 400: MALFORMED_QUERY"):
        list(org.query("SELECT Id FROM Account WHERE ~"))
    with pytest.raises(EngineError, match="HTTP 400: INVALID_TYPE"):
        list(org.tooling_query("SELECT Id FROM ApexClass"))
    with pytest.raises(EngineError, match="HTTP 404: NOT_FOUND"):
        org.describe("Account/limits")
    transport = FakeTransport(FakeOrg(tmp_path / "org.sqlite"))
    authorized = {"Authorization": "Bearer fake"}
    with httpx.Client(base_url=FAKE_URL, headers=authorized, transport=transport) as client:
        assert client.get("/elsewhere").status_code == 404
    with (
        httpx.Client(base_url=FAKE_URL, transport=transport) as client,
        pytest.raises(EngineError, match="HTTP 401: INVALID_SESSION_ID"),
    ):
        RestOrg(client).versions()


def _canned(*responses: httpx.Response) -> httpx.MockTransport:
    queue = list(responses)
    return httpx.MockTransport(lambda _: queue.pop(0))


def test_client_failures() -> None:
    def broken(_: httpx.Request) -> httpx.Response:
        message = "refused"
        raise httpx.ConnectError(message)

    with (
        httpx.Client(base_url=FAKE_URL, transport=httpx.MockTransport(broken)) as client,
        pytest.raises(EngineError, match="listing the API versions failed: ConnectError"),
    ):
        RestOrg(client).versions()
    plain = httpx.Response(500, text="not json")
    started = httpx.Response(200, json={"id": "j1", "state": "UploadComplete"})
    failed = httpx.Response(200, json={"id": "j1", "state": "Failed"})
    transport = _canned(plain, started, failed)
    with httpx.Client(base_url=FAKE_URL, transport=transport) as client:
        org = RestOrg(client, poll_seconds=0.0)
        with pytest.raises(EngineError, match=r"failed with HTTP 500$"):
            org.versions()
        with pytest.raises(EngineError, match="the export ended Failed"):
            list(org.export("SELECT Id FROM Account"))


# Cassettes


def test_recordings_replay(tmp_path: Path) -> None:
    sample_org(tmp_path / "org.sqlite")
    recording = RecordingTransport(FakeTransport(FakeOrg(tmp_path / "org.sqlite"), page=40))
    with _org(tmp_path / "org.sqlite", recording) as client:
        first = list(client.export("SELECT Id FROM Contact"))
    cassette = scrub(recording.cassette(), FAKE_URL)
    cassette.write(tmp_path / "cassette.json")
    replay = ReplayTransport(Cassette.read(tmp_path / "cassette.json"))
    with _org(tmp_path / "org.sqlite", replay) as replayed:
        assert list(replayed.export("SELECT Id FROM Contact")) == first
        # Requests that repeat get their last recorded response again.
        assert list(replayed.export("SELECT Id FROM Contact")) == first
        with pytest.raises(EngineError, match="listing the API versions failed: CassetteError"):
            replayed.versions()
    with pytest.raises(CassetteError, match="no response to GET /services/data/"):
        ReplayTransport(cassette).handle_request(httpx.Request("GET", f"{FAKE_URL}/services/data/"))


def test_the_committed_cassette_replays() -> None:
    replay = ReplayTransport(Cassette.read(CASSETTE))
    with httpx.Client(base_url="https://" + EXAMPLE_HOST, transport=replay) as client:
        org = RestOrg(client, poll_seconds=0.0)
        exercised = recorder.exercise(
            SalesforceSource(org, "62.0", ("Account", "Contact", "Opportunity", "Case"))
        )
    assert exercised.catalog.tables
    for table in exercised.catalog.tables:
        assert table.primary_key == (ColumnName("Id"),)
        assert exercised.queried[table.name.name] == exercised.exported[table.name.name]


def test_the_scrubber_removes_credentials_hosts_and_org_ids() -> None:
    session = "00D5e000000AbCdEAG!AQ4AQNotARealSessionJustAShape.x_y-z"
    instance = "https://example-dev-ed.my.salesforce.com"
    login = json.dumps(
        {
            "access_token": session,
            "instance_url": instance,
            "id": "https://login.salesforce.com/id/00D5e000000AbCdEAG/0055e000001XyZaAAK",
            "signature": "c2lnbmF0dXJl",
            "refresh_token": "5Aep861NotARealRefreshToken",
        }
    )
    synthetic = Cassette(
        interactions=(
            Interaction(
                method="POST",
                url="/services/oauth2/token",
                body="grant_type=refresh_token&refresh_token=5Aep861NotARealRefreshToken",
                status=200,
                headers={
                    "content-type": "application/json",
                    "set-cookie": "sid=00D5e000000AbCd!AQ4AQOther; domain=.salesforce.com",
                    "authorization": f"Bearer {session}",
                },
                response=login,
            ),
            Interaction(
                method="GET",
                url="/services/data/v62.0/query/?q=SELECT+Id+FROM+Organization+WHERE+Id+%3D+%2700D5e000000AbCd%27",
                body="",
                status=200,
                headers={"Sforce-Locator": "null"},
                response=json.dumps(
                    {
                        "records": [
                            {
                                "attributes": {
                                    "url": f"{instance}/services/data/v62.0/sobjects"
                                    "/Organization/00D5e000000AbCdEAG"
                                },
                                "Id": "00D5e000000AbCdEAG",
                                "sessionId": "00D5e000000AbCd!AQ4AQOther",
                                "LoginUrl": "https://example-dev-ed.lightning.force.com/one",
                            }
                        ]
                    }
                ),
            ),
        )
    )
    cleaned = scrub(synthetic, instance, ("5Aep861NotARealRefreshToken",))
    text = cleaned.model_dump_json()
    for secret in (
        session,
        "AQ4AQ",
        "5Aep861",
        "c2lnbmF0dXJl",
        "example-dev-ed",
        "salesforce.com",
        "force.com",
        "00D5e000000AbCd",
        "sid=",
    ):
        assert secret not in text, secret
    assert EXAMPLE_HOST in text
    assert EXAMPLE_ORG_ID in text
    assert EXAMPLE_ORG_ID[:15] in text
    assert cleaned.interactions[0].headers == {"content-type": "application/json"}
    assert cleaned.interactions[1].headers == {"Sforce-Locator": "null"}
    # Without an instance URL or secrets, the patterns still apply.
    assert scrub(synthetic, "", ("",)).model_dump_json().count("salesforce.com") == 0


def test_the_recorder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sample_org(tmp_path / "org.sqlite")
    out = tmp_path / "fake.json"
    assert (
        recorder.main([str(out), "--fake", str(tmp_path / "org.sqlite"), "--objects", "Case"]) == 0
    )
    assert "over 1 objects" in capsys.readouterr().out
    assert Cassette.read(out).interactions
    login = {
        "status": 0,
        "result": {
            "accessToken": "00D5e000000AbCdEAG!AQ4AQSecret",
            "instanceUrl": "https://example-dev-ed.my.salesforce.com",
            "id": "00D5e000000AbCdEAG",
        },
    }
    monkeypatch.setattr("sys.stdin", io.StringIO('{"result": {"accessToken": "secret-ish"}}'))
    assert recorder.main([str(tmp_path / "none.json")]) == 2
    assert "secret-ish" not in capsys.readouterr().err
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(login)))
    org = FakeOrg(tmp_path / "org.sqlite")

    def transport(**_: object) -> FakeTransport:
        return FakeTransport(org)

    monkeypatch.setattr(httpx, "HTTPTransport", transport)
    assert recorder.main([str(tmp_path / "org.json"), "--objects", "Lead"]) == 0
    text = (tmp_path / "org.json").read_text()
    assert "AQ4AQSecret" not in text
    assert "00D5e000000AbCdEAG" not in text


# The source


def test_catalog_choices() -> None:
    assert kind("email") is ColumnKind.TEXT
    assert kind("reference") is ColumnKind.OTHER
    assert kind("currency") is ColumnKind.DECIMAL
    assert kind("anyType") is ColumnKind.OTHER
    every = {"queryable": True, "retrieveable": True, "createable": True, "replicateable": True}
    assert included(ObjectSummary(name="Account", **every), ())
    assert not included(ObjectSummary(name="Account", **every | {"replicateable": False}), ())
    assert not included(ObjectSummary(name="AccountFeed", **every), ())
    assert not included(ObjectSummary(name="Setting__mdt", **every), ())
    assert included(ObjectSummary(name="Acme__Thing__c", **every), ("Acme__*",))
    assert not included(ObjectSummary(name="Account", **every), ("Acme__*",))


def test_exported_values_are_typed() -> None:
    def column(kind_: ColumnKind) -> StoredColumn:
        return StoredColumn(name=ColumnName("F"), type=str(kind_), kind=kind_)

    assert typed("", column(ColumnKind.TEXT)) is None
    assert typed("TRUE", column(ColumnKind.BOOLEAN)) is True
    assert typed("12", column(ColumnKind.INTEGER)) == 12
    assert typed("1.5", column(ColumnKind.FLOAT)) == 1.5
    assert typed("1.50", column(ColumnKind.DECIMAL)) == "1.50"
    with pytest.raises(EngineError, match="an exported value of F is not a boolean"):
        typed("yes", column(ColumnKind.BOOLEAN))
    with pytest.raises(EngineError, match="is not a integer"):
        typed("x", column(ColumnKind.INTEGER))


def test_the_source(org: RestOrg) -> None:
    source = SalesforceSource(org, "62.0", ())
    assert source.server_version() == "62.0"
    with pytest.raises(EngineError, match=r"does not offer API version 9\.0; its latest is 64\.0"):
        SalesforceSource(org, "9.0", ()).server_version()
    with source.snapshot(AS_OF) as snapshot:
        assert snapshot.project(ACCOUNT, NoRows(), (ColumnName("Id"),)) == set()
        pairs = set(
            snapshot.project(CONTACT, AllRows(), (ColumnName("accountid"), ColumnName("Id")))
        )
        assert len(pairs) == 60
        assert any(pair[0] is None for pair in pairs)


def test_packages_without_a_namespace(tmp_path: Path) -> None:
    fake = FakeOrg(tmp_path / "org.sqlite")
    fake.add_package("", "Unmanaged Things", "2")
    fake.add_package("", "", "1")
    with _org(tmp_path / "org.sqlite") as org:
        requirements = SalesforceSource(org, "62.0", ()).catalog().requirements
    assert [(r.name, r.version) for r in requirements] == [("Unmanaged Things", "2.0.0")]


# The engine


def test_connections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = SalesforceEngine()
    assert engine.owner
    monkeypatch.setenv("SF_URL", "https://org.example.com")
    monkeypatch.setenv("SF_TOKEN", "not-a-token")
    real: dict[str, JsonValue] = {
        "instance_url": {"env": "SF_URL"},
        "access_token": {"env": "SF_TOKEN"},
    }
    with engine.connect(real) as session:
        assert isinstance(session, SalesforceSource)
    with pytest.raises(SettingsError, match="access_token"):
        engine.connect({"instance_url": {"env": "SF_URL"}}).__enter__()
    with pytest.raises(SettingsError, match="api_version"):
        engine.connect({"fake": "x", "api_version": "v62"}).__enter__()
    with pytest.raises(EngineError, match="does not exist"):
        engine.connect({"fake": str(tmp_path / "missing.sqlite")}).__enter__()
    tooling = Tooling(runtime=installed().runtime("docker", {}), image="vendor/x:1")
    with pytest.raises(EngineError, match=r"source\.image does not apply"):
        engine.working(tmp_path, tooling, "62.0")
    with pytest.raises(EngineError, match="no server to start"):
        engine.server(tmp_path, None)
    assert isinstance(installed().engines["salesforce"](), SalesforceEngine)
    assert isinstance(engine_entry(), SalesforceEngine)


# Working copies


def _working(tmp_path: Path) -> tuple[SalesforceWorking, Path]:
    """A working copy of Account and Contact, with a contact whose account is missing."""
    extract = tmp_path / "extract"
    (extract / "records").mkdir(parents=True)
    columns = [
        {"name": "Id", "type": "id", "kind": "other"},
        {"name": "AccountId", "type": "reference", "kind": "other"},
        {"name": "Kind", "type": "string", "kind": "text"},
        {"name": "Score", "type": "currency", "kind": "decimal"},
    ]
    objects = {
        "api_version": "62.0",
        "objects": [
            {"table": {"namespace": "", "name": "Account"}, "columns": columns[:1], "records": 1},
            {"table": {"namespace": "", "name": "Contact"}, "columns": columns, "records": 3},
        ],
    }
    (extract / "objects.json").write_text(json.dumps(objects))
    (extract / "records" / "Account.jsonl").write_text('{"Id":"a1"}\n')
    (extract / "records" / "Contact.jsonl").write_text(
        '{"Id":"c1","AccountId":"a1","Kind":"x","Score":"1.50"}\n'
        '{"Id":"c2","AccountId":"a2","Kind":"y","Score":null}\n'
        '{"Id":"c3","AccountId":"a3","Kind":"x","Score":"2"}\n'
    )
    return SalesforceWorking(tmp_path / "scratch"), extract


def _plan() -> Plan:
    """A plan, which loading takes but does not need."""
    columns = (Column(name=ColumnName("Id"), type="id", kind=ColumnKind.OTHER, nullable=False),)
    table = Table(name=ACCOUNT, columns=columns, primary_key=(ColumnName("Id"),))
    catalog = Catalog(engine=EngineName("salesforce"), tables=(table,))
    spec = parse_spec(
        "name: crm\nsource: {engine: salesforce, connection: {fake: x}}\n"
        "subset: {seeds: {Account: 'Id != null'}}\nstore: [{filesystem: {path: /tmp/store}}]\n"
    )
    options = PlanOptions(strict=StrictLevel.OFF)
    return plan(spec, catalog, None, SalesforceEngine().capabilities(), options)


def _group(*fixups: Fixup) -> PlannedGroup:
    return PlannedGroup(
        name=GroupName("all"),
        tables=(ACCOUNT, CONTACT),
        requires=(),
        omitted_references=(),
        fixups=fixups,
    )


def _edge(when: str | None = None, columns: tuple[str, ...] = ("AccountId",)) -> Edge:
    return Edge(
        name="Contact.AccountId",
        child=CONTACT,
        columns=tuple(map(ColumnName, columns)),
        parent=ACCOUNT,
        parent_columns=(ColumnName("Id"),),
        when=when,
    )


def test_fixups(tmp_path: Path) -> None:
    working, extract = _working(tmp_path)
    assert working.server_version() == "1.0"
    assert working.available() is None
    working.close()
    planned = _plan()
    nulled = _group(Fixup(edge=_edge("Kind = 'y'"), mode=EdgeMode.NULL, parent_present=True))
    counts = working.load(planned, nulled, extract, tmp_path / "nulled", ())
    assert {c.table.name: c.rows for c in counts} == {"Account": 1, "Contact": 3}
    stubbed = _group(Fixup(edge=_edge(), mode=EdgeMode.STUB, parent_present=False))
    counts = working.load(planned, stubbed, extract, tmp_path / "stubbed", ())
    assert {c.table.name: c.rows for c in counts} == {"Account": 3, "Contact": 3}
    transformation = Transformation(rules=(), mapper=lambda _r, _v: {}, hooks=())
    working.transform(stubbed, tmp_path / "stubbed", tmp_path / "copy", transformation)
    working.package(tmp_path / "copy", ImageFormat.LOGICAL, tmp_path / "image")
    with working.restore(tmp_path / "image", ImageFormat.LOGICAL, tmp_path / "restored") as image:
        assert image.counts([ACCOUNT, CONTACT, TableName.of("", "Lead")]) == {
            ACCOUNT: 3,
            CONTACT: 3,
            TableName.of("", "Lead"): 0,
        }
        assert image.orphans(_edge()) == 0
        assert image.orphans(_edge("Kind = 'x'")) == 0
        assert sorted(image.text_values(ColumnRef(CONTACT, ColumnName("Score")))) == ["1.5", "2"]
        assert image.failing_rows("SELECT Id FROM Contact WHERE kind = 'X'") == 2
        with pytest.raises(EngineError, match="an assertion: no object Lead"):
            image.failing_rows("SELECT Id FROM Lead")
        with pytest.raises(EngineError, match="a `when` predicate on Contact"):
            image.orphans(_edge("Missing = 1"))
    lines = (tmp_path / "image" / "records" / "Contact.jsonl").read_text().splitlines()
    assert json.loads(lines[0]) == {"Id": "c1", "AccountId": "a1", "Kind": "x", "Score": "1.5"}


def test_working_copies_refuse_what_they_cannot_do(tmp_path: Path) -> None:
    working, extract = _working(tmp_path)
    planned = _plan()
    hook = Hook(sql="SELECT 1")
    with pytest.raises(EngineError, match="does not run hooks"):
        working.load(planned, _group(), extract, tmp_path / "a", (hook,))
    pair = _edge(columns=("AccountId", "Kind"))
    bad = _group(Fixup(edge=pair, mode=EdgeMode.NULL, parent_present=True))
    with pytest.raises(EngineError, match="names one field and the parent's Id"):
        working.load(planned, bad, extract, tmp_path / "b", ())
    elsewhere = Edge(
        name="Contact.OwnerId",
        child=CONTACT,
        columns=(ColumnName("AccountId"),),
        parent=TableName.of("", "User"),
        parent_columns=(ColumnName("Id"),),
    )
    stub = _group(Fixup(edge=elsewhere, mode=EdgeMode.STUB, parent_present=False))
    with pytest.raises(EngineError, match="stub parents need User"):
        working.load(planned, stub, extract, tmp_path / "c", ())
    working.load(planned, _group(), extract, tmp_path / "d", ())
    expression = PlannedRule(
        column=ColumnRef(CONTACT, ColumnName("Kind")),
        kind=ColumnKind.TEXT,
        type="string",
        rule=ExpressionRule(expression="'x'"),
    )
    nulling = expression.model_copy(update={"rule": NullRule(null=NullOptions())})
    cases: list[tuple[tuple[PlannedRule, ...], tuple[TransformHook, ...], str]] = [
        ((), (TransformHook(sql="SELECT 1"),), "does not run hooks"),
        ((nulling, expression), (), "no expression rules"),
    ]
    for rules, hooks, problem in cases:
        with pytest.raises(EngineError, match=problem):
            working.transform(
                _group(),
                tmp_path / "d",
                tmp_path / "e",
                Transformation(rules=rules, mapper=lambda _r, _v: {}, hooks=hooks),
            )
    for action in (
        lambda: working.package(tmp_path / "d", ImageFormat.PHYSICAL, tmp_path / "f"),
        lambda: working.restore(tmp_path / "d", ImageFormat.PHYSICAL, tmp_path / "g").__enter__(),
    ):
        with pytest.raises(EngineError, match="only logical images"):
            action()


def test_the_extract(tmp_path: Path, org: RestOrg, monkeypatch: pytest.MonkeyPatch) -> None:
    source = SalesforceSource(org, "62.0", ("Account", "Contact"))
    spec = parse_spec(
        "name: crm\nsource: {engine: salesforce, connection: {fake: x}}\n"
        "subset: {seeds: {Account: \"Industry = 'Retail'\"}}\n"
        "store: [{filesystem: {path: /tmp/store}}]\n"
    )
    capabilities = SalesforceEngine().capabilities()
    planned = plan(spec, source.catalog(), None, capabilities, PlanOptions(strict=StrictLevel.OFF))
    ids = frozenset((f"001{n:012d}AAA",) for n in range(1, 31))
    retail = Predicate("Industry = 'Retail'")
    overlapping = AnyOf((ValuesIn((ColumnName("Id"),), ids), retail))
    selections: dict[TableName, Condition] = {ACCOUNT: overlapping, CONTACT: NoRows()}
    monkeypatch.setattr(source_module, "BULK_LIMIT", 400)
    tooling = Tooling(runtime=installed().runtime("docker", {}))
    with source.snapshot(AS_OF) as snapshot:
        result = snapshot.extract(planned, selections, tmp_path / "extract", tooling)
    assert {c.table.name: c.rows for c in result.counts} == {"Account": 30, "Contact": 0}
    assert result.server_version == "62.0"
    FakeOrg(tmp_path / "org.sqlite").add_field("Contact", FakeField("Nickname"))
    with (
        source.snapshot(AS_OF) as snapshot,
        pytest.raises(EngineError, match="Contact's fields changed"),
    ):
        snapshot.extract(planned, selections, tmp_path / "again", tooling)
