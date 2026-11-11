# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The btrfs replicate adapter for Datadouble.

Spec settings::

    replicate:
      using:
        btrfs:
          path: /srv/clones   # on the same btrfs filesystem as staging

Each copy is its own subvolume, filled with reflinked copies of the staged
image's files: the copy shares every data block with staging until it is
written to, so it is made in moments and costs no space up front, and
consumers can snapshot or delete it like any subvolume. Uses the ``btrfs``
command; staging and ``path`` must be on one btrfs filesystem.
"""

import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, JsonValue

from datadouble.files import Reflink, copy_tree
from datadouble.images import ImageSummary
from datadouble.replicate import ClaimState, Copy, CopyKind, Made, ReplicateError, Replicator
from datadouble.settings import parse_settings


class BtrfsSettings(BaseModel):
    """Settings for the ``btrfs`` replicate adapter."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    path: Path


def _run(arguments: list[str], action: str) -> None:
    result = subprocess.run(  # noqa: S603 - btrfs or cp, with arguments built here
        arguments, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        lines = result.stderr.strip().splitlines()
        message = f"{action} failed: {lines[-1] if lines else f'status {result.returncode}'}"
        raise ReplicateError(message)


def btrfs_binary() -> str:
    """``DATADOUBLE_BTRFS``, or ``btrfs`` on ``PATH``."""
    found = os.environ.get("DATADOUBLE_BTRFS") or shutil.which("btrfs")
    if not found:
        message = "the btrfs command was not found; install btrfs-progs"
        raise ReplicateError(message)
    return found


class BtrfsReplicator:
    """Volume copies as subvolumes sharing blocks with staging."""

    def __init__(self, settings: BtrfsSettings) -> None:
        """Make subvolumes under ``settings.path``."""
        self.root = settings.path

    @property
    def kind(self) -> CopyKind:
        """Volumes."""
        return CopyKind.VOLUME

    def create(self, copy_id: str, image: Path, summary: ImageSummary) -> Made:
        """Create a subvolume, reflink the image into it, then rename it into place."""
        self.root.mkdir(parents=True, exist_ok=True)
        partial = self.root / f".{copy_id}.partial"
        btrfs = btrfs_binary()
        _run([btrfs, "subvolume", "create", str(partial)], "creating the subvolume")
        problem = copy_tree(image, partial, Reflink.ALWAYS, contents=True)
        if problem:
            subprocess.run(  # noqa: S603 - btrfs, with arguments built here
                [btrfs, "subvolume", "delete", str(partial)], capture_output=True, check=False
            )
            message = f"copying {summary.identity.generation} failed: {problem}"
            raise ReplicateError(message)
        final = self.root / copy_id
        partial.rename(final)
        return Made(location={"path": str(final)})

    def destroy(self, copy: Copy) -> None:
        """Delete the copy's subvolume, complete or not."""
        btrfs = btrfs_binary()
        for path in (self.root / copy.id, self.root / f".{copy.id}.partial"):
            if path.exists():
                _run([btrfs, "subvolume", "delete", str(path)], f"deleting {path.name}")

    def claim_state(self, _copy: Copy, /) -> ClaimState:
        """Claims are recorded by ``claim``."""
        return ClaimState.UNKNOWN


def btrfs_replicator(settings: Mapping[str, JsonValue]) -> Replicator:
    """The entry point named in ``datadouble.replicators``."""
    return BtrfsReplicator(parse_settings(BtrfsSettings, settings, "btrfs replicate"))
