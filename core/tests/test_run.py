# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

import datetime as dt
import json
import uuid
from pathlib import Path

import pytest

from datadouble.anonymize import Anonymizer
from datadouble.boundary import BoundaryError
from datadouble.catalog import Catalog, CatalogLock, ColumnKind, Requirement, RequirementKind
from datadouble.engine import Account, EngineError, RequirementError, Tooling
from datadouble.images import ImageSummary, unpack
from datadouble.names import ColumnName, ColumnRef, EngineName, Generation, TableName
from datadouble.planner import AccountNames, PlannedRule, PlanOptions
from datadouble.registry import Registry
from datadouble.run import (
    Run,
    RunError,
    RunOptions,
    RunResult,
    Stage,
    StageStatus,
    fingerprint,
    value_mapper,
)
from datadouble.secrets import SecretError
from datadouble.spec import (
    AccountPrivileges,
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
from fake_engine import FakeEngine, FakeRuntime, check_protocols
from fakes import FakeSource, catalog, shop, table

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
source: {engine: fake, connection: {where: here}, image: vendor/fake:1}
runtime: {fake: {}}
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
def runtime() -> FakeRuntime:
    return FakeRuntime()


@pytest.fixture
def registry(engine: FakeEngine, runtime: FakeRuntime) -> Registry:
    return Registry(
        engines={"fake": lambda: check_protocols(engine)},
        stores={"filesystem": filesystem_store},
        runtimes={"fake": lambda _settings: runtime},
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
    lock: bool | Catalog = True,
    plan: PlanOptions | None = None,
) -> RunResult:
    parsed = parse_spec(spec.replace("STORE", str(tmp_path / "store")))
    options = RunOptions(
        stages=stages, work=tmp_path / "work", resume=resume, plan=plan or PlanOptions()
    )
    locked = lock if isinstance(lock, Catalog) else shop()
    lock_value = CatalogLock.of(locked) if lock else None
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
    assert summary.server_version == "1.0"
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


def test_resume_after_a_failure(
    tmp_path: Path, registry: Registry, engine: FakeEngine, runtime: FakeRuntime
) -> None:
    engine.fail_load = True
    with pytest.raises(EngineError, match="told to fail"):
        run(tmp_path, registry)
    # A failed build still closes its working databases.
    assert engine.closed == 1
    (generation,) = [p.name for p in (tmp_path / "work" / "shop").iterdir()]
    engine.fail_load = False
    resumed = run(tmp_path, registry, resume=Generation(generation))
    assert engine.closed == 2
    assert resumed.generation == generation
    assert statuses(resumed)["plan"] == "reused"
    assert statuses(resumed)["extract"] == "reused"
    assert statuses(resumed)["load/all"] == "ran"
    assert runtime.released == ["all", generation]
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


def test_signed_images(tmp_path: Path, registry: Registry) -> None:
    signed = SPEC + "signing: {cosign: {key: {env: COSIGN_KEY}}}\n"
    result = run(tmp_path, registry, spec=signed)
    (summary,) = result.images
    stored = FilesystemStore(tmp_path / "store")
    data = stored.get_bytes(summary.identity.summary_key())
    assert data is not None
    on_disk = ImageSummary.model_validate_json(data)
    signature = summary.identity.summary_key().replace("summary", "sigstore")
    assert on_disk.signature == signature
    assert stored.get_bytes(signature) is not None


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


def test_runtime_is_required_from_extract(tmp_path: Path, registry: Registry) -> None:
    bare = SPEC.replace("runtime: {fake: {}}\n", "")
    planned = run(tmp_path, registry, spec=bare, stages=(Stage.PLAN,))
    assert not planned.failed()
    with pytest.raises(RunError, match="the spec has no `runtime`"):
        run(tmp_path, registry, spec=bare, stages=(Stage.EXTRACT,), resume=planned.generation)


def test_engines_get_the_runtime_and_image(
    tmp_path: Path, registry: Registry, engine: FakeEngine, runtime: FakeRuntime
) -> None:
    run(tmp_path, registry, stages=(Stage.PLAN, Stage.EXTRACT, Stage.LOAD))
    assert engine.source.toolings == [Tooling(runtime=runtime, image="vendor/fake:1")] * 2


def test_builds_run_as_the_serving_user(
    tmp_path: Path, registry: Registry, engine: FakeEngine
) -> None:
    # A user nobody runs the tests as.
    engine.owner_ = "12345:12345"
    with pytest.raises(EngineError, match=r"fake images are served as 12345:12345.*--user 12345"):
        run(tmp_path, registry)
    # Planning alone touches no engine files.
    assert run(tmp_path, registry, stages=(Stage.PLAN,)).stages


FILES_SPEC = (
    SPEC
    + """
files:
  connection: {root: /srv/uploads}
  select: {avatars: "path <= 'a2'"}
  paths_from: posts.title
  cleanup: ["delete files.documents where path = 'b'"]
"""
)


TEXT_PATH = {"path": ColumnKind.TEXT}


def test_files_build_beside_the_source(tmp_path: Path, engine: FakeEngine) -> None:
    tree = FakeEngine(
        catalog=catalog(
            *(
                table(name, ["path"], key=("path",), kinds=TEXT_PATH, namespace="files")
                for name in ("avatars", "documents")
            )
        ),
        source=FakeSource(
            rows={
                T("files", "avatars"): [{"path": "a1"}, {"path": "a3"}],
                T("files", "documents"): [{"path": "a"}, {"path": "b"}, {"path": "z"}],
            }
        ),
        name_=EngineName("files"),
    )
    runtime = FakeRuntime()
    registry = Registry(
        engines={"fake": lambda: engine, "files": lambda: check_protocols(tree)},
        stores={"filesystem": filesystem_store},
        runtimes={"fake": lambda _settings: runtime},
    )
    spec = FILES_SPEC.replace(
        "select: {avatars: ", "exclude: []\n  select: {documents: \"path = 'none'\", avatars: "
    )
    # The default strategy is nested, whose conditions a projection cannot read alone.
    keys = PlanOptions()
    result = run(tmp_path, registry, spec=spec, plan=keys)
    assert not result.failed(), result.verification
    files = [r for r in result.stages if r.engine == "files"]
    assert {r.stage for r in files} == set(Stage)
    assert {s.identity.engine for s in result.images} == {"fake", "files"}
    followed = json.loads((Path(result.work) / "extract" / "paths.json").read_text())
    assert followed == ["a", "b", "c"]
    # The files the chosen posts name, and the avatars the files' own seed selects.
    rows = json.loads((Path(result.work) / "files" / "extract" / "data" / "rows.json").read_text())
    assert {table: sorted(r["path"] for r in found) for table, found in rows.items()} == {
        "files.avatars": ["a1"],
        "files.documents": ["a", "b"],
    }
    # The cleanup removed document b from the copy.
    transformed = json.loads(
        (Path(result.work) / "files" / "transform" / "all" / "manifest.json").read_text()
    )
    assert {c["table"]["name"]: c["rows"] for c in transformed["outputs"]["counts"]} == {
        "avatars": 1,
        "documents": 1,
    }
    resumed = run(tmp_path, registry, spec=spec, resume=result.generation, plan=keys)
    assert {r.status for r in resumed.stages} == {StageStatus.REUSED}


def test_files_without_paths_from(tmp_path: Path, engine: FakeEngine) -> None:
    tree = FakeEngine(
        catalog=catalog(
            table("avatars", ["path"], key=("path",), kinds=TEXT_PATH, namespace="files")
        ),
        source=FakeSource(rows={T("files", "avatars"): [{"path": "a1"}]}),
        name_=EngineName("files"),
    )
    registry = Registry(
        engines={"fake": lambda: engine, "files": lambda: tree},
        stores={"filesystem": filesystem_store},
        runtimes={"fake": lambda _settings: FakeRuntime()},
    )
    spec = SPEC + "files: {select: {avatars: \"path = 'a1'\"}}\n"
    result = run(tmp_path, registry, spec=spec, plan=PlanOptions(strategy=Strategy.KEYS))
    assert not result.failed(), result.verification
    assert not (Path(result.work) / "extract" / "paths.json").exists()
    assert len(result.images) == 2


def test_files_paths_from_a_pattern(tmp_path: Path, engine: FakeEngine) -> None:
    tree = FakeEngine(
        catalog=catalog(
            table("documents", ["path"], key=("path",), kinds=TEXT_PATH, namespace="files")
        ),
        source=FakeSource(rows={T("files", "documents"): [{"path": "a"}, {"path": "b"}]}),
        name_=EngineName("files"),
    )
    registry = Registry(
        engines={"fake": lambda: engine, "files": lambda: tree},
        stores={"filesystem": filesystem_store},
        runtimes={"fake": lambda _settings: FakeRuntime()},
    )
    # A post with no title names no file, and is not counted as unmatched.
    engine.source.rows[T("public", "posts")].append(
        {"id": 13, "user_id": 1, "category_id": None, "title": None}
    )
    paths_from = (
        "{table: posts, path: 'https://cdn/{title}?v={id}', match: '^https://cdn/([ab])[?]'}"
    )
    spec = SPEC + (
        f"files:\n  select: {{documents: \"path = 'none'\"}}\n  paths_from: {paths_from}\n"
    )
    result = run(tmp_path, registry, spec=spec)
    assert json.loads((Path(result.work) / "extract" / "paths.json").read_text()) == ["a", "b"]
    expected = (
        "files.paths_from: 1 value(s) did not match the pattern, so their files were not"
        " copied; check `match`"
    )
    assert [w for w in result.warnings if "paths_from" in w] == [expected]


def test_runs_need_the_boundary(
    tmp_path: Path, registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATADOUBLE_BOUNDARY")
    with pytest.raises(BoundaryError, match="reading the source runs only on machines"):
        run(tmp_path, registry)


def test_an_unusable_work_directory(tmp_path: Path, registry: Registry) -> None:
    (tmp_path / "work").write_text("not a directory", encoding="utf-8")
    with pytest.raises(RunError, match=r"cannot start a generation in .*work: Not a directory"):
        run(tmp_path, registry)


def test_the_image_must_provide_the_source_requirements(
    tmp_path: Path, registry: Registry, engine: FakeEngine
) -> None:
    vector = Requirement(kind=RequirementKind.EXTENSION, name="vector", version="0.8.2")
    engine.catalog = shop().model_copy(update={"requirements": (vector,)})
    with pytest.raises(RequirementError) as missing:
        run(tmp_path / "a", registry, lock=engine.catalog)
    assert missing.value.problems == ("extension vector is not in the working image",)
    # The image is checked before the snapshot opens, by working databases it closes.
    assert engine.snapshots == 0
    assert engine.closed == 1
    engine.available = (vector.model_copy(update={"version": "0.8.7"}),)
    with pytest.raises(RequirementError) as other:
        run(tmp_path / "b", registry, lock=engine.catalog)
    assert other.value.problems == (
        "extension vector 0.8.2 is not in the working image, which has 0.8.7",
    )
    engine.available = (vector,)
    assert not run(tmp_path / "c", registry, lock=engine.catalog).failed()
    # Working databases that need no add-ons provide whatever the source uses.
    engine.available = None
    assert not run(tmp_path / "d", registry, lock=engine.catalog).failed()


def test_omitted_requirements_are_not_checked(
    tmp_path: Path, registry: Registry, engine: FakeEngine
) -> None:
    vector = Requirement(kind=RequirementKind.EXTENSION, name="vector", version="0.8.2")
    engine.catalog = shop().model_copy(update={"requirements": (vector,)})
    spec = SPEC.replace("image: vendor/fake:1}", "image: vendor/fake:1, omit: [vector]}")
    result = run(tmp_path, registry, spec=spec, lock=engine.catalog)
    assert not result.failed()
    (summary,) = [s for s in result.images if s.identity.group == "all"]
    _, manifest = unpack(tmp_path / "store" / summary.archives[0].key, tmp_path / "unpacked")
    build = json.loads(manifest)
    assert build["omitted"] == [{"kind": "extension", "name": "vector", "version": "0.8.2"}]
    assert build["requirements"] == []
    # The manifest says what each seed chose and which tables nothing selected.
    assert {s["pattern"] for s in build["seeds"]} == {"users", "comments"}
    assert build["unselected"] == []


def test_a_resumed_build_checks_the_image_too(
    tmp_path: Path, registry: Registry, engine: FakeEngine
) -> None:
    vector = Requirement(kind=RequirementKind.EXTENSION, name="vector", version="0.8.2")
    engine.catalog = shop().model_copy(update={"requirements": (vector,)})
    engine.available = (vector,)
    extracted = run(tmp_path, registry, stages=(Stage.PLAN, Stage.EXTRACT), lock=engine.catalog)
    engine.available = ()
    with pytest.raises(RequirementError):
        run(
            tmp_path,
            registry,
            stages=(Stage.LOAD,),
            resume=extracted.generation,
            lock=engine.catalog,
        )


def test_engines_name_their_compatibility_line(
    tmp_path: Path, registry: Registry, engine: FakeEngine
) -> None:
    engine.capabilities_ = engine.capabilities_.model_copy(update={"version_line": 2})
    result = run(tmp_path, registry)
    summary = next(s for s in result.images if s.identity.group == "all")
    assert summary.identity.engine_version == "1.0"
    assert summary.identity.prefix() == "shop/all/fake-1.0"


def test_a_resumed_build_packages_again_for_a_new_line(
    tmp_path: Path, registry: Registry, engine: FakeEngine
) -> None:
    first = run(tmp_path, registry)
    engine.capabilities_ = engine.capabilities_.model_copy(update={"version_line": 2})
    resumed = run(tmp_path, registry, resume=Generation(first.generation))
    assert statuses(resumed)["package/all"] == "ran"
    assert statuses(resumed)["transform/all"] == "reused"
    summary = next(s for s in resumed.images if s.identity.group == "all")
    assert summary.identity.engine_version == "1.0"


ACCOUNTS = """package:
  accounts:
    - {user: app, password: {env: COPY_PASSWORD}}
    - {user: reporting, password: PLAIN, privileges: read}
store:"""


def test_physical_images_get_the_accounts(
    tmp_path: Path, registry: Registry, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine.capabilities_ = engine.capabilities_.model_copy(update={"accounts": AccountNames()})
    value, written = uuid.uuid4().hex, uuid.uuid4().hex
    monkeypatch.setenv("COPY_PASSWORD", value)
    spec = SPEC.replace("store:", ACCOUNTS.replace("PLAIN", written))
    result = run(tmp_path, registry, spec=spec)
    assert not result.failed()
    ((image, accounts),) = engine.accounts
    assert image.name == "physical"
    assert accounts == (
        Account(user="app", password=value),
        Account(user="reporting", password=written, privileges=AccountPrivileges.READ),
    )
    assert value not in repr(accounts)
    summary = next(s for s in result.images if s.identity.group == "all")
    assert summary.accounts == ("app", "reporting")
    _, manifest = unpack(tmp_path / "store" / summary.archives[0].key, tmp_path / "unpacked")
    assert json.loads(manifest)["accounts"] == [
        {"user": "app", "privileges": "all"},
        {"user": "reporting", "privileges": "read"},
    ]
    assert value.encode() not in manifest
    assert written.encode() not in manifest
    package = (tmp_path / "work" / "shop" / result.generation / "package" / "all").glob("*.json")
    for file in package:
        assert value.encode() not in file.read_bytes()


def test_a_changed_password_packages_again(
    tmp_path: Path, registry: Registry, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine.capabilities_ = engine.capabilities_.model_copy(update={"accounts": AccountNames()})
    spec = SPEC.replace("store:", ACCOUNTS.replace("PLAIN", "unchanged"))
    monkeypatch.setenv("COPY_PASSWORD", uuid.uuid4().hex)
    first = run(tmp_path, registry, spec=spec)
    same = run(tmp_path, registry, spec=spec, resume=first.generation)
    assert set(statuses(same).values()) == {"reused"}
    changed = uuid.uuid4().hex
    monkeypatch.setenv("COPY_PASSWORD", changed)
    again = run(tmp_path, registry, spec=spec, resume=first.generation)
    assert statuses(again)["plan"] == "reused"
    assert statuses(again)["transform/all"] == "reused"
    assert statuses(again)["package/all"] == "ran"
    assert engine.accounts[-1][1][0].password == changed


def test_unresolvable_passwords_fail_before_extract(
    tmp_path: Path, registry: Registry, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine.capabilities_ = engine.capabilities_.model_copy(update={"accounts": AccountNames()})
    monkeypatch.delenv("COPY_PASSWORD", raising=False)
    spec = SPEC.replace("store:", ACCOUNTS.replace("PLAIN", "x"))
    with pytest.raises(SecretError, match="COPY_PASSWORD is not set"):
        run(tmp_path, registry, spec=spec)
    assert engine.snapshots == 0


def test_working_databases_must_be_able_to_add_accounts(
    tmp_path: Path, registry: Registry, engine: FakeEngine
) -> None:
    engine.capabilities_ = engine.capabilities_.model_copy(update={"accounts": AccountNames()})
    engine.accounts_working = False
    spec = SPEC.replace("store:", "package: {accounts: [{user: app, password: x}]}\nstore:")
    with pytest.raises(EngineError, match="cannot give images accounts"):
        run(tmp_path, registry, spec=spec)
