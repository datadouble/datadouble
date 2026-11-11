# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

import datetime as dt
import json
from pathlib import Path

import pytest

from datadouble.anonymize import Anonymizer
from datadouble.catalog import CatalogLock, ColumnKind
from datadouble.engine import EngineError
from datadouble.images import ImageSummary, unpack
from datadouble.names import ColumnName, ColumnRef, Generation, TableName
from datadouble.planner import PlannedRule, PlanOptions
from datadouble.registry import Registry
from datadouble.run import Run, RunError, RunOptions, RunResult, Stage, fingerprint, value_mapper
from datadouble.secrets import SecretError
from datadouble.spec import (
    HashOptions,
    HashRule,
    ImageFormat,
    NullOptions,
    NullRule,
    Strategy,
    parse_spec,
)
from datadouble.storage import FilesystemStore, filesystem_store
from datadouble.verify import CheckKind
from fake_engine import FakeEngine, check_protocols
from fakes import FakeSource, shop

ALL = tuple(Stage)
T = TableName.of


def dataset() -> FakeSource:
    return FakeSource(
        rows={
            T("public", "users"): [
                {
                    "id": i,
                    "email": f"user{i}@realmail.net",
                    "name": f"Name {i}",
                    "created": i,
                    "note": None,
                }
                for i in range(1, 6)
            ],
            T("public", "categories"): [{"id": 1, "name": "news"}, {"id": 2, "name": "misc"}],
            T("public", "posts"): [
                {"id": 10, "user_id": 1, "category_id": 1, "title": "a"},
                {"id": 11, "user_id": 2, "category_id": None, "title": "b"},
                {"id": 12, "user_id": 4, "category_id": 2, "title": "c"},
            ],
            T("public", "comments"): [
                {"id": 100, "post_id": 10, "author_id": 3, "editor_id": None, "body": "hi"},
                {
                    "id": 101,
                    "post_id": 12,
                    "author_id": 5,
                    "editor_id": 1,
                    "body": "call 617-222-3344",
                },
            ],
        }
    )


SPEC = """
name: shop
source: {engine: fake, connection: {where: here}}
exclude: ["store", "staff", "dba_*", "audit.*", "events", "film_actor"]
subset:
  seeds: {users: "id <= 2", comments: "id = 101"}
  follow: {down: ["users -> posts", "posts -> comments"]}
  edges:
    comments.author_id -> users: {on_missing: null}
    comments.editor_id -> users: {on_missing: null}
anonymize:
  users: {email: {faker: email, unique: true}, name: {faker: name}}
  comments: {body: {expression: "'redacted'"}}
store:
  - filesystem: {path: STORE}
"""


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine(catalog=shop(), source=dataset())


@pytest.fixture
def registry(engine: FakeEngine) -> Registry:
    return Registry(
        engines={"fake": lambda: check_protocols(engine)}, stores={"filesystem": filesystem_store}
    )


def _clock() -> dt.datetime:
    return dt.datetime(2026, 11, 11, 9, 0, tzinfo=dt.UTC)


def run(
    tmp_path: Path,
    registry: Registry,
    *,
    spec: str = SPEC,
    stages: tuple[Stage, ...] = ALL,
    resume: Generation | None = None,
    lock: bool = True,
    plan: PlanOptions | None = None,
) -> RunResult:
    parsed = parse_spec(spec.replace("STORE", str(tmp_path / "store")))
    options = RunOptions(
        stages=stages, work=tmp_path / "work", resume=resume, plan=plan or PlanOptions()
    )
    lock_value = CatalogLock.of(shop()) if lock else None
    return Run(parsed, lock_value, registry, options, _clock).execute()


def statuses(result: RunResult) -> dict[str, str]:
    return {f"{r.stage}{'/' + r.group if r.group else ''}": r.status.value for r in result.stages}


def stored_rows(
    tmp_path: Path, result: RunResult, group: str = "all"
) -> dict[str, list[dict[str, object]]]:
    (summary,) = [s for s in result.images if s.identity.group == group]
    archive = next(a for a in summary.archives if a.format is ImageFormat.LOGICAL)
    image, manifest = unpack(tmp_path / "store" / archive.key, tmp_path / "unpacked" / group)
    assert json.loads(manifest)["identity"]["group"] == group
    rows: dict[str, list[dict[str, object]]] = json.loads((image / "logical.json").read_text())
    return rows


def test_a_whole_build(tmp_path: Path, registry: Registry, engine: FakeEngine) -> None:
    result = run(tmp_path, registry)
    assert not result.failed()
    assert set(statuses(result).values()) == {"ran"}
    assert result.warnings == ()
    assert engine.connections == [{"where": "here"}]
    assert engine.snapshots == 1
    (report,) = result.verification
    assert report.passed()
    kinds = {check.kind for check in report.checks()}
    assert kinds == {
        CheckKind.RESTORE,
        CheckKind.ROW_COUNTS,
        CheckKind.REFERENCES,
        CheckKind.PII_SCAN,
    }
    (summary,) = result.images
    assert summary.identity.engine_version == "1"
    assert summary.verification.passed
    assert [a.format for a in summary.archives] == [ImageFormat.PHYSICAL, ImageFormat.LOGICAL]
    store = FilesystemStore(tmp_path / "store")
    keys = store.list("shop/all/fake-1/")
    assert keys == sorted(
        [a.key for a in summary.archives]
        + [
            summary.identity.summary_key(),
            summary.identity.summary_key().replace("summary", "verify"),
        ]
    )
    assert (
        ImageSummary.model_validate_json(store.get_bytes(summary.identity.summary_key()) or b"")
        == summary
    )
    assert result.stored == (f"filesystem:{tmp_path / 'store'}/{summary.identity.summary_key()}",)
    rows = stored_rows(tmp_path, result)
    # Comment 101 is seeded, so its post (12) and that post's author (4) come too;
    # comment authors are not followed, so their references are nulled.
    assert sorted(int(str(u["id"])) for u in rows["public.users"]) == [1, 2, 4]
    assert {c["id"]: c["author_id"] for c in rows["public.comments"]} == {100: None, 101: None}
    assert all(
        str(u["email"]).endswith(("example.com", "example.net", "example.org"))
        for u in rows["public.users"]
    )
    assert {c["body"] for c in rows["public.comments"]} == {"redacted"}
    assert (Path(result.work) / "key").stat().st_mode & 0o777 == 0o600


def test_both_strategies_store_the_same_rows(tmp_path: Path, registry: Registry) -> None:
    nested = run(tmp_path / "a", registry, plan=PlanOptions(strategy=Strategy.NESTED))
    keys = run(tmp_path / "b", registry, plan=PlanOptions(strategy=Strategy.KEYS))
    rows_a = stored_rows(tmp_path / "a", nested)
    rows_b = stored_rows(tmp_path / "b", keys)
    assert {t: sorted(str(r["id"]) for r in rows) for t, rows in rows_a.items()} == {
        t: sorted(str(r["id"]) for r in rows) for t, rows in rows_b.items()
    }


def test_resume_after_a_failure(tmp_path: Path, registry: Registry, engine: FakeEngine) -> None:
    engine.fail_load = True
    with pytest.raises(EngineError, match="told to fail"):
        run(tmp_path, registry)
    (generation,) = [p.name for p in (tmp_path / "work" / "shop").iterdir()]
    engine.fail_load = False
    resumed = run(tmp_path, registry, resume=Generation(generation))
    assert resumed.generation == generation
    assert statuses(resumed)["plan"] == "reused"
    assert statuses(resumed)["extract"] == "reused"
    assert statuses(resumed)["load/all"] == "ran"
    assert engine.released == ["all"]
    assert engine.snapshots == 1
    assert not resumed.failed()


def test_partial_runs_and_resume(tmp_path: Path, registry: Registry) -> None:
    first = run(tmp_path, registry, stages=(Stage.PLAN, Stage.EXTRACT))
    assert statuses(first) == {"plan": "ran", "extract": "ran"}
    assert first.verification == ()
    again = run(tmp_path, registry, resume=first.generation)
    assert statuses(again)["extract"] == "reused"
    assert statuses(again)["store/all"] == "ran"
    same = run(tmp_path, registry, resume=first.generation)
    assert set(statuses(same).values()) == {"reused"}
    assert same.images == again.images


def test_a_changed_rule_reruns_from_transform(
    tmp_path: Path, registry: Registry, engine: FakeEngine
) -> None:
    first = run(tmp_path, registry)
    changed = SPEC.replace("name: {faker: name}", "name: {faker: first_name}")
    second = run(tmp_path, registry, spec=changed, resume=first.generation)
    status = statuses(second)
    assert status["plan"] == "ran"
    assert status["extract"] == "reused"
    assert status["load/all"] == "reused"
    assert status["transform/all"] == "ran"
    assert engine.transforms == 2
    with pytest.raises(RunError, match="was not requested"):
        run(
            tmp_path, registry, spec=SPEC, resume=first.generation, stages=(Stage.PLAN, Stage.STORE)
        )


def test_stages_need_their_predecessors(tmp_path: Path, registry: Registry) -> None:
    with pytest.raises(RunError, match="has no plan"):
        run(tmp_path, registry, stages=(Stage.EXTRACT,))
    first = run(tmp_path, registry, stages=(Stage.PLAN,))
    with pytest.raises(RunError, match="stage extract has to run"):
        run(tmp_path, registry, stages=(Stage.LOAD,), resume=first.generation)
    with pytest.raises(RunError, match="there is no generation nope"):
        run(tmp_path, registry, resume=Generation("nope"))


def test_two_groups_share_a_generation_and_agree(tmp_path: Path, registry: Registry) -> None:
    spec = (
        SPEC
        + """
images:
  people: {include: ["users"]}
  content: {include: ["posts", "comments", "categories", "users"]}
"""
    )
    result = run(tmp_path, registry, spec=spec)
    assert not result.failed()
    assert {s.identity.generation for s in result.images} == {result.generation}
    people = {u["id"]: u["email"] for u in stored_rows(tmp_path, result, "people")["public.users"]}
    content = {
        u["id"]: u["email"] for u in stored_rows(tmp_path, result, "content")["public.users"]
    }
    assert people == content


def test_verification_failures_stop_store(
    tmp_path: Path, registry: Registry, engine: FakeEngine
) -> None:
    spec = (
        SPEC.replace("  users: {email: {faker: email, unique: true}, name: {faker: name}}\n", "")
        .replace("  comments: {body: {expression: \"'redacted'\"}}\n", "")
        .replace("anonymize:\n", "")
        + """
verify:
  pii_scan: {allow: {users.name: ["Name %"]}}
  assertions: [{name: no early users, sql: "rows public.users where id <= 1"}]
"""
    )
    engine.corrupt.add(ImageFormat.PHYSICAL)
    result = run(tmp_path, registry, spec=spec)
    assert result.failed()
    assert result.stored == ()
    (report,) = result.verification
    failed = {(c.kind, c.name): c.detail for c in report.checks() if not c.passed}
    assert failed[(CheckKind.RESTORE, "restore")].startswith("cannot restore")
    assert failed[(CheckKind.PII_SCAN, "public.users.email")] == "3 value(s) look like email"
    assert failed[(CheckKind.PII_SCAN, "public.comments.body")] == "1 value(s) look like phone"
    assert failed[(CheckKind.ASSERTION, "no early users")] == "1 row(s) returned"
    assert not (tmp_path / "store").exists()


def test_hooks_and_row_counts(tmp_path: Path, registry: Registry, engine: FakeEngine) -> None:
    spec = (
        SPEC
        + """
hooks:
  before_load: [{sql: "set things"}]
  after_transform: [{sql: "delete public.comments where id = 100"}]
"""
    )
    result = run(tmp_path, registry, spec=spec)
    assert engine.hooks == ["set things"]
    assert not result.failed()
    assert [c["id"] for c in stored_rows(tmp_path, result)["public.comments"]] == [101]


def test_fixups_and_stubs(tmp_path: Path, registry: Registry) -> None:
    spec = SPEC.replace(
        "comments.author_id -> users: {on_missing: null}",
        "comments.author_id -> users: {on_missing: stub}",
    )
    result = run(tmp_path, registry, spec=spec)
    assert not result.failed()
    users = stored_rows(tmp_path, result)["public.users"]
    stubs = [u for u in users if u["email"] is None]
    assert [u["id"] for u in stubs] == [3, 5]


def test_store_problems(tmp_path: Path, registry: Registry) -> None:
    no_stores = SPEC.replace("store:\n  - filesystem: {path: STORE}\n", "")
    with pytest.raises(RunError, match="lists no stores"):
        run(tmp_path, registry, spec=no_stores)
    built = run(
        tmp_path,
        registry,
        spec=no_stores,
        stages=(
            Stage.PLAN,
            Stage.EXTRACT,
            Stage.LOAD,
            Stage.TRANSFORM,
            Stage.PACKAGE,
            Stage.VERIFY,
        ),
    )
    assert not built.failed()
    assert built.stored == ()


def test_the_anonymization_key(
    tmp_path: Path, registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyed = SPEC + "secrets: {anonymization_key: {env: SHOP_KEY}}\n"
    with pytest.raises(SecretError):
        run(tmp_path, registry, spec=keyed)
    monkeypatch.setenv("SHOP_KEY", "a fixed key")
    one = stored_rows(tmp_path, run(tmp_path, registry, spec=keyed))
    (tmp_path / "unpacked").rename(tmp_path / "first")
    two = stored_rows(tmp_path, run(tmp_path, registry, spec=keyed))
    assert one["public.users"] == two["public.users"]


def test_value_rules_only_reach_the_mapper() -> None:
    mapper = value_mapper(Anonymizer(b"k" * 32))
    column = ColumnRef(table=T("public", "users"), column=ColumnName("note"))
    hashed = PlannedRule(
        column=column, kind=ColumnKind.TEXT, type="text", rule=HashRule(hash=HashOptions())
    )
    assert set(mapper(hashed, ["a", "b"])) == {"a", "b"}
    nulled = PlannedRule(
        column=column, kind=ColumnKind.TEXT, type="text", rule=NullRule(null=NullOptions())
    )
    with pytest.raises(RunError, match="NullRule is applied by the engine"):
        mapper(nulled, ["x"])


def test_fingerprint_is_canonical() -> None:
    assert fingerprint({"b": 1, "a": [T("x", "y")]}) == fingerprint({"a": [T("x", "y")], "b": 1})
    assert fingerprint({"a": 1}) != fingerprint({"a": 2})
