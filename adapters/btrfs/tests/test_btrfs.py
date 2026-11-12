# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The btrfs replicate adapter: against a stand-in btrfs, and a real one when available."""

import datetime as dt
import os
from pathlib import Path

import pytest

import datadouble_btrfs
from datadouble.files import Reflink, copy_tree
from datadouble.images import ImageIdentity, ImageSummary, Verification
from datadouble.names import EngineName, Generation, GroupName, SpecName
from datadouble.registry import installed
from datadouble.replicate import ClaimState, Copy, CopyKind, CopyState, ReplicateError
from datadouble.spec import ReplicateMode, StrictLevel
from datadouble_btrfs import BtrfsReplicator, BtrfsSettings, btrfs_binary, btrfs_replicator

FAKE_BTRFS = """#!/bin/sh
# Stands in for btrfs: subvolumes are plain directories.
case "$1 $2" in
  "subvolume create") mkdir "$3" ;;
  "subvolume delete") rm -rf "$3" ;;
  *) echo "unexpected: $*" >&2; exit 2 ;;
esac
"""

SUMMARY = ImageSummary(
    identity=ImageIdentity(
        spec=SpecName("shop"),
        group=GroupName("all"),
        engine=EngineName("postgres"),
        engine_version="18",
        generation=Generation("0193a7c21b4e9f30"),
    ),
    built_at=dt.datetime(2026, 11, 1, tzinfo=dt.UTC),
    archives=(),
    manifest_sha256="0" * 64,
    verification=Verification(passed=True, checks=1, failed=0),
    strict=StrictLevel.COLUMNS,
)


def record(copy_id: str) -> Copy:
    return Copy(
        id=copy_id,
        prefix="shop/all/postgres-18",
        generation=Generation("0193a7c21b4e9f30"),
        kind=CopyKind.VOLUME,
        mode=ReplicateMode.POOL,
        adapter="btrfs",
        state=CopyState.READY,
        created_at=SUMMARY.built_at,
        built_at=SUMMARY.built_at,
    )


def staged_image(root: Path) -> Path:
    image = root / "image"
    (image / "pgdata").mkdir(parents=True)
    (image / "pgdata" / "PG_VERSION").write_text("18\n", encoding="utf-8")
    return image


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "bin" / "btrfs"
    script.parent.mkdir()
    script.write_text(FAKE_BTRFS, encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("DATADOUBLE_BTRFS", str(script))

    # Most test filesystems cannot reflink; copy without sharing blocks instead.
    def plain(source: Path, destination: Path, _reflink: Reflink, *, contents: bool) -> str | None:
        return copy_tree(source, destination, contents=contents)

    monkeypatch.setattr(datadouble_btrfs, "copy_tree", plain)
    return script


@pytest.mark.usefixtures("fake")
def test_copies_are_subvolumes(tmp_path: Path) -> None:
    replicator = btrfs_replicator({"path": str(tmp_path / "clones")})
    assert replicator.kind is CopyKind.VOLUME
    image = staged_image(tmp_path)
    made = replicator.create("abc", image, SUMMARY)
    assert made.location == {"path": str(tmp_path / "clones" / "abc")}
    assert (tmp_path / "clones" / "abc" / "pgdata" / "PG_VERSION").is_file()
    replicator.destroy(record("abc"))
    assert not (tmp_path / "clones" / "abc").exists()
    replicator.destroy(record("gone"))
    assert replicator.claim_state(record("abc")) is ClaimState.UNKNOWN


def test_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: Path) -> None:
    replicator = BtrfsReplicator(BtrfsSettings(path=tmp_path / "clones"))
    (tmp_path / "clones" / ".abc.partial").mkdir(parents=True)
    with pytest.raises(ReplicateError, match="creating the subvolume failed: mkdir"):
        replicator.create("abc", staged_image(tmp_path), SUMMARY)
    (tmp_path / "clones" / ".abc.partial").rmdir()
    with pytest.raises(ReplicateError, match="copying 0193a7c21b4e9f30 failed"):
        replicator.create("abc", tmp_path / "no-image", SUMMARY)
    assert not (tmp_path / "clones" / ".abc.partial").exists()
    (tmp_path / "clones" / ".abc.partial").mkdir()
    fake.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    with pytest.raises(ReplicateError, match=r"deleting \.abc\.partial failed: status 3"):
        replicator.destroy(record("abc"))
    monkeypatch.delenv("DATADOUBLE_BTRFS")
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    with pytest.raises(ReplicateError, match="btrfs command was not found"):
        btrfs_binary()
    assert "btrfs" in installed().replicators


@pytest.mark.btrfs
def test_on_btrfs() -> None:
    root = os.environ.get("DATADOUBLE_TEST_BTRFS")
    if not root:
        pytest.skip("no btrfs filesystem; run with the btrfs Compose profile")
    base = Path(root)
    image = staged_image(base / "staging")
    replicator = BtrfsReplicator(BtrfsSettings(path=base / "clones"))
    replicator.create("real", image, SUMMARY)
    # The root directory of a btrfs subvolume always has inode 256.
    assert (base / "clones" / "real").stat().st_ino == 256
    assert (base / "clones" / "real" / "pgdata" / "PG_VERSION").read_text(
        encoding="utf-8"
    ) == "18\n"
    replicator.destroy(record("real"))
    assert not (base / "clones" / "real").exists()
