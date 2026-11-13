# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The non-production side: promote, fetch, activate, replicate, claim, release, prune."""

import datetime as dt
import hashlib
import json
import os
from pathlib import Path

import pytest

from datadouble.announce import (
    AnnounceError,
    CiOutputsAnnouncer,
    CiOutputsSettings,
    CiSystem,
    announce,
    describe,
    withdraw,
)
from datadouble.channels import point_channel, read_channel
from datadouble.engine import EngineError
from datadouble.images import (
    ImageArchive,
    ImageIdentity,
    ImageSummary,
    Verification,
    pack,
    read_summary,
)
from datadouble.names import EngineName, Generation, GroupName, SpecName
from datadouble.promote import PromoteError, promote
from datadouble.prune import PruneError, PruneTarget, prune
from datadouble.registry import Registry
from datadouble.replicate import (
    ClaimState,
    Copy,
    CopyKind,
    FilesystemReplicateSettings,
    FilesystemReplicator,
    Options,
    ReplicateError,
    claim,
    copies_of,
    release,
    replicate,
)
from datadouble.signing import SigningError, bundle_key, payload, sign, verify
from datadouble.spec import (
    CosignSigning,
    CosignTrust,
    EnvSecret,
    Fetch,
    FileSecret,
    ImageFormat,
    Spec,
    StrictLevel,
    parse_spec,
)
from datadouble.staging import FetchStatus, Staging, StagingError, activate, fetch
from datadouble.storage import FilesystemStore
from fake_engine import ENGINES, FakeEngine
from fakes import FakeSource, shop

T0 = dt.datetime(2026, 11, 1, tzinfo=dt.UTC)


def make_spec(tmp_path: Path, extra: str = "", *, mode: str = "pool", size: int = 2) -> Spec:
    return parse_spec(
        f"""
name: shop
source: {{engine: fake}}
store: [{{filesystem: {{path: {tmp_path / "build"}}}}}]
signing: {{cosign: {{key: {{env: COSIGN_KEY}}}}}}
trust: {{cosign: {{public_key: {{env: COSIGN_PUB}}}}}}
promote: {{to: [{{filesystem: {{path: {tmp_path / "export"}}}}}]}}
fetch:
  from: {{filesystem: {{path: {tmp_path / "export"}}}}}
  staging: {tmp_path / "staging"}
  keep: 2
replicate: {{using: {{filesystem: {{path: {tmp_path / "copies"}}}}}, mode: {mode}, size: {size}}}
announce: [{{dotenv: {{path: {tmp_path / "clone.env"}}}}}]
{extra}"""
    )


def build(
    tmp_path: Path,
    generation: str,
    *,
    passed: bool = True,
    signed: bool = True,
    strict: StrictLevel = StrictLevel.COLUMNS,
    version: str = "18",
    built_at: dt.datetime = T0,
) -> ImageSummary:
    """Put an image into the build store, as ``run`` would."""
    store = FilesystemStore(tmp_path / "build")
    identity = ImageIdentity(
        spec=SpecName("shop"),
        group=GroupName("all"),
        engine=EngineName("fake"),
        engine_version=version,
        generation=Generation(generation),
    )
    files = tmp_path / "files" / generation
    files.mkdir(parents=True)
    (files / "data.txt").write_text(f"rows of {generation}\n", encoding="utf-8")
    archive_path = tmp_path / f"{generation}.tar.gz"
    found = pack(files, b"{}", archive_path)
    key = identity.archive_key(ImageFormat.PHYSICAL)
    store.put_file(key, archive_path)
    summary = ImageSummary(
        identity=identity,
        built_at=built_at,
        archives=(ImageArchive(format=ImageFormat.PHYSICAL, key=key, digest=found),),
        manifest_sha256="0" * 64,
        verification=Verification(passed=passed, checks=4, failed=0 if passed else 1),
        strict=strict,
    )
    report = identity.summary_key().replace(".summary.json", ".verify.json")
    store.put_bytes(report, b'{"group": "all", "formats": []}')
    if signed:
        store.put_bytes(bundle_key(summary), sign(summary, CosignSigning(key=EnvSecret(env="K"))))
        summary = summary.model_copy(update={"signature": bundle_key(summary)})
    store.put_bytes(identity.summary_key(), summary.model_dump_json().encode())
    return summary


PREFIX = "shop/all/fake-18"


def settings(spec: Spec) -> Fetch:
    assert spec.fetch is not None
    return spec.fetch


def export_channel(tmp_path: Path) -> Generation | None:
    channel = read_channel(FilesystemStore(tmp_path / "export"), PREFIX, "latest")
    return None if channel is None else channel.generation


# promote


def test_promote_copies_and_advances_the_channel(tmp_path: Path, registry: Registry) -> None:
    spec = make_spec(tmp_path)
    build(tmp_path, "0000000000010001")
    newest = build(tmp_path, "0000000000020001")
    (promoted,) = promote(spec, registry)
    assert promoted.summary == newest
    assert export_channel(tmp_path) == "0000000000020001"
    export = FilesystemStore(tmp_path / "export")
    assert read_summary(export, PREFIX, Generation("0000000000020001")) == newest
    promote(spec, registry, generation=Generation("0000000000010001"))
    channel = read_channel(export, PREFIX, "latest")
    assert channel is not None
    assert (channel.generation, channel.previous) == ("0000000000010001", ("0000000000020001",))


@pytest.mark.parametrize(
    ("passed", "signed", "strict", "problem"),
    [
        (False, True, StrictLevel.COLUMNS, "verification did not pass"),
        (True, False, StrictLevel.COLUMNS, "the image is not signed"),
        (True, True, StrictLevel.TABLES, "built with strict mode tables, but columns is required"),
    ],
)
def test_promote_refuses(
    tmp_path: Path,
    registry: Registry,
    passed: bool,
    signed: bool,
    strict: StrictLevel,
    problem: str,
) -> None:
    build(tmp_path, "0000000000010001", passed=passed, signed=signed, strict=strict)
    with pytest.raises(PromoteError) as caught:
        promote(make_spec(tmp_path), registry)
    assert any(problem in p for p in caught.value.problems), caught.value.problems
    assert export_channel(tmp_path) is None
    assert not (tmp_path / "export").exists()


def test_promote_refuses_tampering(tmp_path: Path, registry: Registry) -> None:
    summary = build(tmp_path, "0000000000010001")
    store = FilesystemStore(tmp_path / "build")
    forged = summary.model_copy(update={"strict": StrictLevel.OFF})
    store.put_bytes(summary.identity.summary_key(), forged.model_dump_json().encode())
    with pytest.raises(PromoteError) as caught:
        promote(make_spec(tmp_path), registry)
    assert any("invalid signature" in p for p in caught.value.problems)
    store.put_bytes(summary.identity.summary_key(), summary.model_dump_json().encode())
    store.put_bytes(summary.archives[0].key, b"swapped")
    with pytest.raises(PromoteError) as caught:
        promote(make_spec(tmp_path), registry)
    assert any("does not match the hash" in p for p in caught.value.problems)
    store.delete(summary.archives[0].key)
    store.delete(bundle_key(summary))
    with pytest.raises(PromoteError) as caught:
        promote(make_spec(tmp_path), registry)
    (problem,) = caught.value.problems
    assert "sigstore.json is missing" in problem
    assert "physical.tar.gz is missing" in problem


def test_promote_without_a_verification_report(tmp_path: Path, registry: Registry) -> None:
    summary = build(tmp_path, "0000000000010001")
    report = summary.identity.summary_key().replace(".summary.json", ".verify.json")
    FilesystemStore(tmp_path / "build").delete(report)
    promote(make_spec(tmp_path), registry)
    assert FilesystemStore(tmp_path / "export").get_bytes(report) is None


def test_promote_needs_settings_and_images(tmp_path: Path, registry: Registry) -> None:
    bare = parse_spec("name: shop\nsource: {engine: fake}\n")
    with pytest.raises(PromoteError, match="1 problem"):
        promote(bare, registry)
    unstored = parse_spec(
        "name: shop\nsource: {engine: fake}\ntrust: {cosign: {public_key: {env: P}}}\n"
        f"promote: {{to: [{{filesystem: {{path: {tmp_path}}}}}]}}\n"
    )
    with pytest.raises(PromoteError) as caught:
        promote(unstored, registry)
    assert caught.value.problems == ("the spec lists no stores to promote from",)
    with pytest.raises(PromoteError) as caught:
        promote(make_spec(tmp_path), registry)
    assert caught.value.problems == (
        "shop/all: no images in filesystem:" + str(tmp_path / "build"),
    )
    build(tmp_path, "0000000000010001")
    with pytest.raises(PromoteError) as caught:
        promote(make_spec(tmp_path), registry, generation=Generation("0000000000099999"))
    assert caught.value.problems == ("shop/all: no generation 0000000000099999",)


# fetch and activate


def promoted(tmp_path: Path, registry: Registry, *generations: str) -> Spec:
    spec = make_spec(tmp_path)
    for generation in generations:
        build(tmp_path, generation)
        promote(spec, registry, generation=Generation(generation))
    return spec


def test_fetch_skips_when_nothing_is_newer(tmp_path: Path, registry: Registry) -> None:
    spec = promoted(tmp_path, registry, "0000000000010001")
    (first,) = fetch(spec, registry)
    assert first.status is FetchStatus.FETCHED
    staged = Staging(settings(spec))
    image = staged.image(PREFIX, Generation("0000000000010001"))
    assert (image / "data.txt").read_text(encoding="utf-8") == "rows of 0000000000010001\n"
    (again,) = fetch(spec, registry)
    assert again.status is FetchStatus.SKIPPED
    assert again.generation == "0000000000010001"


def test_fetch_keeps_the_newest_and_activate_rolls_back(tmp_path: Path, registry: Registry) -> None:
    spec = promoted(tmp_path, registry, "0000000000010001")
    fetch(spec, registry)
    for generation in ("0000000000020001", "0000000000030001"):
        build(tmp_path, generation)
        promote(spec, registry, generation=Generation(generation))
        (result,) = fetch(spec, registry)
    assert result.removed == ("0000000000010001",)
    staging = Staging(settings(spec))
    assert staging.staged(PREFIX) == ["0000000000020001", "0000000000030001"]
    (channel,) = activate(spec, Generation("0000000000020001"))
    assert (channel.generation, channel.previous[0]) == ("0000000000020001", "0000000000030001")
    # The rollback holds: the bad generation is still the export channel's, but staged.
    (again,) = fetch(spec, registry)
    assert again.status is FetchStatus.SKIPPED
    current = staging.channel(PREFIX)
    assert current is not None
    assert current.generation == "0000000000020001"
    with pytest.raises(StagingError, match="0000000000010001 is not staged"):
        activate(spec, Generation("0000000000010001"))


def test_fetch_checks_signatures_and_hashes(tmp_path: Path, registry: Registry) -> None:
    spec = promoted(tmp_path, registry, "0000000000010001")
    export = FilesystemStore(tmp_path / "export")
    summary = read_summary(export, PREFIX, Generation("0000000000010001"))
    assert summary is not None
    export.put_bytes(summary.archives[0].key, b"swapped")
    with pytest.raises(StagingError, match="does not match the hash"):
        fetch(spec, registry)
    forged = summary.model_copy(update={"built_at": T0 + dt.timedelta(days=1)})
    export.put_bytes(summary.identity.summary_key(), forged.model_dump_json().encode())
    with pytest.raises(StagingError, match="invalid signature"):
        fetch(spec, registry)
    assert Staging(settings(spec)).staged(PREFIX) == []
    assert not list((tmp_path / "staging" / PREFIX).glob("*.partial-*"))


def test_fetch_errors(tmp_path: Path, registry: Registry) -> None:
    with pytest.raises(StagingError, match="needs `fetch` and `trust`"):
        fetch(parse_spec("name: shop\nsource: {engine: fake}\n"), registry)
    spec = make_spec(tmp_path)
    with pytest.raises(StagingError, match="there are no images in"):
        fetch(spec, registry)
    build(tmp_path, "0000000000010001")
    export = FilesystemStore(tmp_path / "export")
    build_store = FilesystemStore(tmp_path / "build")
    for key in build_store.list(""):
        data = build_store.get_bytes(key)
        assert data is not None
        export.put_bytes(key, data)
    with pytest.raises(StagingError, match="has never been promoted to"):
        fetch(spec, registry)
    logical = make_spec(tmp_path, "").model_copy(
        update={"fetch": settings(spec).model_copy(update={"format": ImageFormat.LOGICAL})}
    )
    promote(spec, registry)
    with pytest.raises(StagingError, match="has no logical archive"):
        fetch(logical, registry)
    pinned = spec.model_copy(
        update={"fetch": settings(spec).model_copy(update={"engine_version": "17"})}
    )
    with pytest.raises(StagingError, match="for engine version 17"):
        fetch(pinned, registry)
    with pytest.raises(StagingError, match="needs a `fetch` section"):
        activate(parse_spec("name: shop\nsource: {engine: fake}\n"), Generation("x"))


# replicate, claim and release


class Watched(FilesystemReplicator):
    """Records how many copies are ready and unclaimed whenever one is destroyed."""

    def __init__(self, root: Path, spec: Spec) -> None:
        super().__init__(FilesystemReplicateSettings(path=root))
        self.spec = spec
        self.unclaimed_at_destroy: list[int] = []

    def destroy(self, copy: Copy) -> None:
        ready = [c for c in copies_of(self.spec) if not c.claimed and c.state == "ready"]
        self.unclaimed_at_destroy.append(len(ready))
        super().destroy(copy)


def watched(registry: Registry, tmp_path: Path, spec: Spec) -> tuple[Registry, Watched]:
    replicator = Watched(tmp_path / "copies", spec)
    return Registry(
        engines=ENGINES,
        stores=registry.stores,
        replicators={"filesystem": lambda _settings: replicator},
        announcers=registry.announcers,
    ), replicator


def staged(
    tmp_path: Path, registry: Registry, *generations: str, mode: str = "pool", size: int = 2
) -> Spec:
    spec = make_spec(tmp_path, mode=mode, size=size)
    for generation in generations:
        build(tmp_path, generation)
        promote(spec, registry, generation=Generation(generation))
        fetch(spec, registry)
    return spec


def test_the_pool_is_filled_and_recycled_without_emptying(
    tmp_path: Path, registry: Registry
) -> None:
    spec = staged(tmp_path, registry, "0000000000010001")
    wired, replicator = watched(registry, tmp_path, spec)
    first = replicate(spec, wired, Options())
    assert len(first.created) == 2
    assert all(c.generation == "0000000000010001" and not c.claimed for c in first.copies)
    assert all((tmp_path / "copies" / c.id / "data.txt").is_file() for c in first.created)
    assert replicate(spec, wired, Options()).created == ()
    taken = claim(spec, wired, Options(owner="alice"))
    assert taken.created[0].owner == "alice"
    build(tmp_path, "0000000000020001")
    promote(spec, registry)
    fetch(spec, registry)
    second = replicate(spec, wired, Options())
    # One unclaimed copy of the old generation is replaced by two fresh ones;
    # at every removal at least one ready copy remained.
    assert len(second.created) == 2
    assert [c.generation for c in second.removed] == ["0000000000010001"]
    assert min(replicator.unclaimed_at_destroy) >= 1
    alice = [c for c in second.copies if c.owner == "alice"]
    assert [c.generation for c in alice] == ["0000000000010001"]
    assert sorted(c.generation for c in second.copies if not c.claimed) == [
        "0000000000020001",
        "0000000000020001",
    ]


def test_all_at_once_replaces_stale_copies_first(tmp_path: Path, registry: Registry) -> None:
    spec = staged(tmp_path, registry, "0000000000010001")
    replicate(spec, registry, Options())
    build(tmp_path, "0000000000020001")
    promote(spec, registry)
    fetch(spec, registry)
    result = replicate(spec, registry, Options(all_at_once=True))
    assert len(result.removed) == 2
    assert {c.generation for c in result.copies} == {"0000000000020001"}


def test_activate_moves_the_pool_back(tmp_path: Path, registry: Registry) -> None:
    spec = staged(tmp_path, registry, "0000000000010001", "0000000000020001")
    replicate(spec, registry, Options())
    activate(spec, Generation("0000000000010001"))
    result = replicate(spec, registry, Options())
    assert {c.generation for c in result.copies} == {"0000000000010001"}


def test_on_demand_copies(tmp_path: Path, registry: Registry) -> None:
    spec = staged(tmp_path, registry, "0000000000010001", mode="on-demand", size=1)
    with pytest.raises(ReplicateError, match="need an owner"):
        replicate(spec, registry, Options())

    def later() -> dt.datetime:
        return T0 + dt.timedelta(days=30)

    made = replicate(spec, registry, Options(owner="mr-12", clock=lambda: T0))
    (copy,) = made.created
    assert (copy.owner, copy.claimed, copy.expires_at) == (
        "mr-12",
        True,
        T0 + dt.timedelta(hours=24),
    )
    replicate(spec, registry, Options(owner="mr-13", clock=later))
    expired = release(spec, registry, Options(expired=True, clock=later))
    assert [c.owner for c in expired.removed] == ["mr-12"]
    assert not (tmp_path / "copies" / copy.id).exists()
    by_owner = release(spec, registry, Options(owner="mr-13"))
    assert [c.owner for c in by_owner.removed] == ["mr-13"]
    assert by_owner.copies == ()


def test_release_by_id_and_incomplete_copies(tmp_path: Path, registry: Registry) -> None:
    spec = staged(tmp_path, registry, "0000000000010001")
    made = replicate(spec, registry, Options())
    line = tmp_path / "staging" / PREFIX / "copies.json"
    records = json.loads(line.read_text())
    records[0]["state"] = "creating"
    line.write_text(json.dumps(records))
    assert (line.stat().st_mode & 0o777) in {0o600, 0o644}
    result = release(spec, registry, Options(copy_ids=(made.created[1].id,)))
    assert {c.id for c in result.removed} == {c.id for c in made.created}
    with pytest.raises(ReplicateError, match="say which copies"):
        release(spec, registry, Options())


def test_claims(tmp_path: Path, registry: Registry) -> None:
    spec = staged(tmp_path, registry, "0000000000010001", size=1)
    with pytest.raises(ReplicateError, match="say who"):
        claim(spec, registry, Options())
    replicate(spec, registry, Options())
    claim(spec, registry, Options(owner="a"))
    with pytest.raises(ReplicateError, match="has no unclaimed copy"):
        claim(spec, registry, Options(owner="b"))


class Observed(FilesystemReplicator):
    """An adapter that sees claims itself, as Kubernetes does through bound volumes."""

    def __init__(self, settings: FilesystemReplicateSettings, states: list[ClaimState]) -> None:
        super().__init__(settings)
        self.states = states

    def claim_state(self, _copy: Copy, /) -> ClaimState:
        return self.states.pop(0) if self.states else ClaimState.FREE


def test_claims_the_adapter_observes(tmp_path: Path, registry: Registry) -> None:
    spec = staged(tmp_path, registry, "0000000000010001", size=2)
    replicate(spec, registry, Options())
    observed = Observed(
        FilesystemReplicateSettings(path=tmp_path / "copies"),
        [ClaimState.CLAIMED, ClaimState.RETURNED],
    )
    wired = Registry(
        engines=ENGINES, stores=registry.stores, replicators={"filesystem": lambda _s: observed}
    )
    result = replicate(spec, wired, Options())
    # One copy was claimed and the other returned: two fresh copies refill the pool.
    assert sum(c.claimed for c in result.copies) == 1
    assert len(result.removed) == 1
    assert len(result.created) == 2


def test_replicate_errors(tmp_path: Path, registry: Registry) -> None:
    bare = parse_spec("name: shop\nsource: {engine: fake}\n")
    with pytest.raises(ReplicateError, match="needs `fetch` and `replicate`"):
        replicate(bare, registry, Options())
    spec = make_spec(tmp_path)
    with pytest.raises(ReplicateError, match="no replicate adapter named 'filesystem'"):
        replicate(spec, Registry(engines=ENGINES, stores={}), Options())
    with pytest.raises(StagingError, match="no images in"):
        replicate(spec, registry, Options())
    (tmp_path / "staging" / PREFIX).mkdir(parents=True)
    with pytest.raises(ReplicateError, match="nothing is active in staging"):
        replicate(spec, registry, Options())
    with pytest.raises(StagingError, match="needs a `fetch` section"):
        copies_of(bare)
    broken = FilesystemReplicator(FilesystemReplicateSettings(path=tmp_path / "x"))
    summary = build(tmp_path, "0000000000010001")
    with pytest.raises(ReplicateError, match="copying 0000000000010001 failed"):
        broken.create("abc", tmp_path / "missing", summary)
    assert broken.kind is CopyKind.VOLUME


# announce


def test_announcements(tmp_path: Path, registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = staged(tmp_path, registry, "0000000000010001", mode="on-demand", size=2)
    made = replicate(spec, registry, Options(owner="mr 1"))
    full = copies_of(spec, with_secrets=True)
    with_secret = tuple(c.model_copy(update={"secrets": {"password": "s3 cr'et"}}) for c in full)
    github = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github))
    wired = make_spec(
        tmp_path,
        "announce: [{dotenv: {path: DOT}}, {ci-outputs: {}}]".replace(
            "DOT", str(tmp_path / "e.env")
        ),
    )
    written = announce(wired, registry, with_secret)
    assert written == (f"dotenv:{tmp_path / 'e.env'}", f"github:{github}")
    dotenv = (tmp_path / "e.env").read_text(encoding="utf-8")
    assert f"DATADOUBLE_COPY_ID={made.created[0].id}\n" in dotenv
    assert f"DATADOUBLE_2_COPY_ID={made.created[1].id}\n" in dotenv
    assert "DATADOUBLE_PASSWORD='s3 cr'\\''et'" in dotenv
    assert "DATADOUBLE_OWNER='mr 1'" in dotenv
    assert (tmp_path / "e.env").stat().st_mode & 0o777 == 0o600
    outputs = github.read_text(encoding="utf-8")
    assert "DATADOUBLE_ENGINE_VERSION=18" in outputs
    assert "PASSWORD" not in outputs
    assert announce(wired, registry, ()) == ()


def test_ci_output_targets(tmp_path: Path, registry: Registry) -> None:
    spec = staged(tmp_path, registry, "0000000000010001", size=1)
    (copy,) = replicate(spec, registry, Options()).created
    gitlab = CiOutputsAnnouncer(CiOutputsSettings(path=tmp_path / "out.env"), {"GITLAB_CI": "true"})
    assert gitlab.announce((describe(copy),)) == (f"gitlab:{tmp_path / 'out.env'}",)
    assert gitlab.withdraw(("x",)) == ()
    plain = CiOutputsAnnouncer(CiOutputsSettings(path=tmp_path / "out.json"), {})
    plain.announce((describe(copy),))
    data = json.loads((tmp_path / "out.json").read_text())
    assert data["copies"][0]["labels"]["group"] == "all"
    with pytest.raises(AnnounceError, match="needs a `path`"):
        CiOutputsAnnouncer(CiOutputsSettings(), {}).announce((describe(copy),))


def test_withdrawing_dotenv(tmp_path: Path, registry: Registry) -> None:

    spec = staged(tmp_path, registry, "0000000000010001", size=1)
    (copy,) = replicate(spec, registry, Options()).created
    announce(spec, registry, (copy,))
    assert withdraw(spec, registry, ("other",)) == ()
    assert withdraw(spec, registry, (copy.id,)) == (f"dotenv:{tmp_path / 'clone.env'}",)
    assert withdraw(spec, registry, (copy.id,)) == ()
    assert withdraw(spec, registry, ()) == ()


# prune


def test_prune_never_removes_a_channels_target(tmp_path: Path, registry: Registry) -> None:
    spec = make_spec(tmp_path, "prune: {keep: 1, keep_days: 0, keep_versions: 1}")
    for generation in ("0000000000010001", "0000000000020001", "0000000000030001"):
        build(tmp_path, generation)
    promote(spec, registry, generation=Generation("0000000000010001"))
    build_store = FilesystemStore(tmp_path / "build")

    point_channel(build_store, PREFIX, "pinned", Generation("0000000000010001"))
    report = prune(
        spec, registry, PruneTarget.STORE, dry_run=True, clock=lambda: T0 + dt.timedelta(days=1)
    )
    assert [p.generation for p in report.removed] == ["0000000000020001"]
    assert report.bytes > 0
    assert len(build_store.list(f"{PREFIX}/0000000000020001.")) == 4
    done = prune(spec, registry, PruneTarget.STORE, clock=lambda: T0 + dt.timedelta(days=1))
    assert [p.generation for p in done.removed] == ["0000000000020001"]
    assert build_store.list(f"{PREFIX}/0000000000020001.") == []
    assert prune(spec, registry, PruneTarget.EXPORT).removed == ()


def test_prune_keeps_recent_images_and_newer_versions(tmp_path: Path, registry: Registry) -> None:
    spec = make_spec(tmp_path, "prune: {keep: 1, keep_days: 7, keep_versions: 1}")
    build(tmp_path, "0000000000010001", version="17")
    build(tmp_path, "0000000000020001")
    build(tmp_path, "0000000000030001")
    result = prune(
        spec, registry, PruneTarget.STORE, dry_run=True, clock=lambda: T0 + dt.timedelta(days=1)
    )
    assert [(p.prefix, p.generation) for p in result.removed] == [
        ("shop/all/fake-17", "0000000000010001")
    ]


def test_prune_staging(tmp_path: Path, registry: Registry) -> None:
    spec = staged(tmp_path, registry, "0000000000010001", "0000000000020001")
    replicate(spec, registry, Options())
    pruned = make_spec(tmp_path, "prune: {keep_versions: 1}")
    assert prune(pruned, registry, PruneTarget.STAGING).removed == ()
    # A newer engine version retires the old line, except its channel's target.
    build(tmp_path, "0000000000030001", version="19")
    promote(spec, registry, generation=Generation("0000000000030001"))
    newer = pruned.model_copy(
        update={"fetch": settings(pruned).model_copy(update={"engine_version": "19"})}
    )
    fetch(newer, registry)
    report = prune(newer, registry, PruneTarget.STAGING, dry_run=True)
    assert [(p.prefix, p.generation) for p in report.removed] == [(PREFIX, "0000000000010001")]
    assert len(report.copies_removed) == 2
    done = prune(newer, registry, PruneTarget.STAGING)
    assert [p.generation for p in done.removed] == ["0000000000010001"]
    assert Staging(settings(newer)).staged(PREFIX) == ["0000000000020001"]
    assert copies_of(newer) == ()


def test_prune_errors(registry: Registry) -> None:
    bare = parse_spec("name: shop\nsource: {engine: fake}\n")
    with pytest.raises(PruneError, match="no store stores"):
        prune(bare, registry, PruneTarget.STORE)
    with pytest.raises(PruneError, match="no export stores"):
        prune(bare, registry, PruneTarget.EXPORT)
    with pytest.raises(PruneError, match="needs a `fetch` section"):
        prune(bare, registry, PruneTarget.STAGING)


# signing


def test_signing_payload_ignores_the_signature(tmp_path: Path) -> None:
    summary = build(tmp_path, "0000000000010001")
    unsigned = summary.model_copy(update={"signature": None})
    assert payload(summary) == payload(unsigned)
    assert (
        hashlib.sha256(payload(summary)).hexdigest()
        in (tmp_path / "build" / PREFIX / "0000000000010001.sigstore.json").read_text()
    )


def test_cosign_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    summary = build(tmp_path, "0000000000010001", signed=False)
    monkeypatch.delenv("DATADOUBLE_COSIGN")
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    with pytest.raises(SigningError, match="cosign was not found"):
        sign(summary, CosignSigning(keyless=True))
    failing = tmp_path / "failing-cosign"
    failing.write_text("#!/bin/sh\nexit 4\n", encoding="utf-8")
    failing.chmod(0o755)
    monkeypatch.setenv("DATADOUBLE_COSIGN", str(failing))
    with pytest.raises(SigningError, match="signing the image failed: status 4"):
        sign(summary, CosignSigning(keyless=True, transparency_log=False))


def test_cosign_arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:

    log = tmp_path / "args"
    recorder = tmp_path / "recording-cosign"
    recorder.write_text(
        f'#!/bin/sh\necho "$@" >> {log}\n'
        'for a; do case $a in --bundle=*) echo {} > "${a#--bundle=}";; esac; done\n',
        encoding="utf-8",
    )
    recorder.chmod(0o755)
    monkeypatch.setenv("DATADOUBLE_COSIGN", str(recorder))
    summary = build(tmp_path, "0000000000010001", signed=False)
    sign(summary, CosignSigning(keyless=True))
    sign(
        summary,
        CosignSigning(key=FileSecret(file=Path("/keys/cosign.key")), transparency_log=False),
    )
    verify(summary, b"{}", CosignTrust(identity="https://ci/workflow", issuer="https://issuer"))
    verify(summary, b"{}", CosignTrust(public_key=EnvSecret(env="PUB"), transparency_log=False))
    calls = log.read_text().splitlines()
    assert calls[0].startswith("sign-blob --yes --bundle=")
    assert "--key=/keys/cosign.key --use-signing-config=false --tlog-upload=false" in calls[1]
    assert (
        "--certificate-identity=https://ci/workflow --certificate-oidc-issuer=https://issuer"
        in calls[2]
    )
    assert "--key=env://PUB --insecure-ignore-tlog=true" in calls[3]


def test_more_stale_copies_than_the_pool_holds(tmp_path: Path, registry: Registry) -> None:
    spec = staged(tmp_path, registry, "0000000000010001", size=2)
    replicate(spec, registry, Options())
    build(tmp_path, "0000000000020001")
    promote(spec, registry)
    fetch(spec, registry)
    assert spec.replicate is not None
    smaller = spec.model_copy(update={"replicate": spec.replicate.model_copy(update={"size": 1})})
    result = replicate(smaller, registry, Options())
    assert len(result.created) == 1
    assert len(result.removed) == 2


def test_repointing_a_channel_keeps_its_history(tmp_path: Path) -> None:
    store = FilesystemStore(tmp_path)
    point_channel(store, PREFIX, "latest", Generation("a"))
    point_channel(store, PREFIX, "latest", Generation("b"))
    again = point_channel(store, PREFIX, "latest", Generation("b"))
    assert again.previous == ("a",)


def test_fetch_recovers_from_leftovers(tmp_path: Path, registry: Registry) -> None:
    spec = promoted(tmp_path, registry, "0000000000010001")
    line = tmp_path / "staging" / PREFIX
    (line / "0000000000010001.partial-1").mkdir(parents=True)
    (line / f"0000000000010001.partial-{os.getpid()}").mkdir()
    fetch(spec, registry)
    assert sorted(p.name for p in line.iterdir()) == [".lock", "0000000000010001", "channels"]
    (line / "channels" / "active.json").unlink()
    (again,) = fetch(spec, registry)
    assert again.status is FetchStatus.SKIPPED
    assert Staging(settings(spec)).channel(PREFIX) is not None


def test_fetch_needs_the_whole_image(tmp_path: Path, registry: Registry) -> None:
    spec = promoted(tmp_path, registry, "0000000000010001")
    export = FilesystemStore(tmp_path / "export")
    summary = read_summary(export, PREFIX, Generation("0000000000010001"))
    assert summary is not None
    export.delete(summary.archives[0].key)
    with pytest.raises(StagingError, match="is missing from the export store"):
        fetch(spec, registry)
    build(tmp_path, "0000000000020001")
    promote(spec, registry, generation=Generation("0000000000020001"))
    newest = read_summary(export, PREFIX, Generation("0000000000020001"))
    assert newest is not None
    export.delete(newest.identity.summary_key())
    with pytest.raises(StagingError, match=r"summary of shop/all/fake-18/0000000000020001 is"):
        fetch(spec, registry)


def test_activate_needs_something_staged(tmp_path: Path) -> None:
    with pytest.raises(StagingError, match="there are no images in"):
        activate(make_spec(tmp_path), Generation("x"))


def test_announce_errors(tmp_path: Path, registry: Registry) -> None:
    spec = staged(tmp_path, registry, "0000000000010001", size=1)
    (copy,) = replicate(spec, registry, Options()).created
    unknown = make_spec(tmp_path, "announce: [{carrier-pigeon: {}}]")
    with pytest.raises(AnnounceError, match="no announce adapter named 'carrier-pigeon'"):
        announce(unknown, registry, (copy,))
    explicit = CiOutputsAnnouncer(
        CiOutputsSettings(system=CiSystem.FILE, path=tmp_path / "o.json"), {"GITHUB_OUTPUT": "x"}
    )
    assert explicit.announce((describe(copy),)) == (f"file:{tmp_path / 'o.json'}",)


def test_signed_builds(tmp_path: Path) -> None:
    summary = build(tmp_path, "0000000000010001")
    bundle = FilesystemStore(tmp_path / "build").get_bytes(bundle_key(summary))
    assert bundle is not None
    verify(summary, bundle, CosignTrust(public_key=EnvSecret(env="P")))


def test_staging_and_copies_belong_to_the_serving_user(tmp_path: Path, registry: Registry) -> None:
    spec = promoted(tmp_path, registry, "0000000000010001")
    elsewhere = Registry(
        engines={
            "fake": lambda: FakeEngine(catalog=shop(), source=FakeSource(rows={}), owner_="1:1")
        },
        stores=registry.stores,
        replicators=registry.replicators,
    )
    with pytest.raises(EngineError, match="served as 1:1"):
        fetch(spec, elsewhere)
    with pytest.raises(EngineError, match="served as 1:1"):
        replicate(spec, elsewhere, Options())
