# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Staging in non-production: ``fetch`` and ``activate``.

Staging is a local directory laid out like a store::

    <staging>/<spec>/<group>/<engine>-<version>/
      <generation>/image/         the unpacked image replicate copies from
      <generation>/summary.json
      channels/active.json        the staging channel
      copies.json                 copies made from this line (see replicate)

``fetch`` reads the export store's channel and the image summary, and downloads
an image only when its generation is not staged yet. A generation is unpacked
under a temporary name and renamed when complete, under a lock, so ``replicate``
never copies a partial image. Staging keeps the newest generations, so
``activate`` can move the staging channel back to an earlier one; a rollback
holds until a newer generation is fetched.
"""

import fcntl
import os
import shutil
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from datadouble.channels import Channel, point_channel, read_channel
from datadouble.engine import check_owner
from datadouble.images import ImageSummary, digest, image_lines, read_summary, unpack
from datadouble.names import Generation, GroupName
from datadouble.promote import check_signature
from datadouble.signing import SigningError
from datadouble.spec import CosignTrust, Fetch, Spec
from datadouble.storage import FilesystemStore, Store

if TYPE_CHECKING:
    from datadouble.registry import Registry


class StagingError(Exception):
    """A fetch or activation that cannot go ahead."""


class FetchStatus(StrEnum):
    """What ``fetch`` did for one image line."""

    FETCHED = "fetched"
    SKIPPED = "skipped"


class Fetched(BaseModel):
    """The outcome of ``fetch`` for one image line."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    prefix: str
    generation: Generation
    status: FetchStatus
    removed: tuple[Generation, ...] = ()
    """Older generations removed from staging to keep it to ``fetch.keep``."""


class Staging:
    """The staging directory of one spec."""

    def __init__(self, settings: Fetch) -> None:
        """Use the staging directory in ``settings``."""
        self.settings = settings
        self.root = settings.staging
        self.store = FilesystemStore(self.root)

    def line(self, prefix: str) -> Path:
        """The directory of one image line."""
        return self.root / prefix

    def image(self, prefix: str, generation: Generation) -> Path:
        """The unpacked image of a staged generation."""
        return self.line(prefix) / generation / "image"

    def staged(self, prefix: str) -> list[Generation]:
        """Completely staged generations, oldest first."""
        line = self.line(prefix)
        return sorted(
            Generation(path.name)
            for path in line.iterdir()
            if path.is_dir() and (path / "summary.json").is_file() and "." not in path.name
        )

    def summary(self, prefix: str, generation: Generation) -> ImageSummary:
        """A staged generation's summary."""
        path = self.line(prefix) / generation / "summary.json"
        return ImageSummary.model_validate_json(path.read_bytes())

    def channel(self, prefix: str) -> Channel | None:
        """The staging channel of a line."""
        return read_channel(self.store, prefix, self.settings.staging_channel)

    def point(self, prefix: str, generation: Generation) -> Channel:
        """Move the staging channel of a line."""
        return point_channel(self.store, prefix, self.settings.staging_channel, generation)

    @contextmanager
    def locked(self, prefix: str) -> Generator[None]:
        """Hold the line's lock, so only one fetch, replicate or release works on it."""
        line = self.line(prefix)
        line.mkdir(parents=True, exist_ok=True)
        with (line / ".lock").open("w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def lines(self, spec: str, group: str) -> list[str]:
        """Image lines with anything staged, for a group."""
        base = self.root / spec / group
        if not base.is_dir():
            return []
        return sorted(f"{spec}/{group}/{path.name}" for path in base.iterdir() if path.is_dir())

    def trim(self, prefix: str, keep: int) -> tuple[Generation, ...]:
        """Remove all but the newest ``keep`` generations, and never the channel's target."""
        channel = self.channel(prefix)
        protected = {channel.generation} if channel else set[Generation]()
        staged = self.staged(prefix)
        removable = [g for g in staged[: max(len(staged) - keep, 0)] if g not in protected]
        for generation in removable:
            remove_directory(self.line(prefix) / generation)
        for leftover in self.line(prefix).glob("*.partial-*"):
            remove_directory(leftover)
        return tuple(removable)


def remove_directory(path: Path) -> None:
    """Move a directory aside and delete it, so a half-deleted one never looks staged."""
    doomed = path.with_name(f"{path.name}.deleting-{uuid.uuid4().hex[:8]}")
    path.rename(doomed)
    shutil.rmtree(doomed, ignore_errors=True)


def choose_line(prefixes: list[str], engine_version: str | None, where: str) -> str:
    """The configured engine version's line, or the newest."""
    if engine_version is not None:
        prefixes = [p for p in prefixes if p.rsplit("-", 1)[-1] == engine_version]
    if not prefixes:
        version = f" for engine version {engine_version}" if engine_version else ""
        message = f"there are no images{version} in {where}"
        raise StagingError(message)
    return prefixes[-1]


def _download(source: Store, summary: ImageSummary, settings: Fetch, partial: Path) -> None:
    archive = next((a for a in summary.archives if a.format is settings.format), None)
    if archive is None:
        message = f"{summary.identity.summary_key()} has no {settings.format} archive"
        raise StagingError(message)
    file = partial / "archive"
    if not source.get_file(archive.key, file):
        message = f"{archive.key} is missing from the export store"
        raise StagingError(message)
    if digest(file) != archive.digest:
        message = f"{archive.key} does not match the hash in its summary"
        raise StagingError(message)
    unpack(file, partial)
    file.unlink()


def fetch(
    spec: Spec, registry: "Registry", *, groups: tuple[GroupName, ...] = ()
) -> tuple[Fetched, ...]:
    """Bring the export channel's image of each group into staging, if it is new."""
    if spec.fetch is None or spec.trust is None:
        message = "the spec needs `fetch` and `trust` sections to fetch images"
        raise StagingError(message)
    check_owner(registry.engine(spec.source.engine))
    settings = spec.fetch
    source = registry.store(*next(iter(settings.source.items())))
    staging = Staging(settings)
    results: list[Fetched] = []
    for group in groups or tuple(spec.images):
        prefix = choose_line(
            image_lines(source, spec.name, group), settings.engine_version, source.describe()
        )
        channel = read_channel(source, prefix, settings.channel)
        if channel is None:
            message = f"channel {settings.channel} of {prefix} has never been promoted to"
            raise StagingError(message)
        with staging.locked(prefix):
            results.append(
                _fetch_line(spec.trust.cosign, source, staging, prefix, channel.generation)
            )
    return tuple(results)


def _fetch_line(
    trust: CosignTrust, source: Store, staging: Staging, prefix: str, generation: Generation
) -> Fetched:
    settings = staging.settings
    if generation in staging.staged(prefix):
        if staging.channel(prefix) is None:
            staging.point(prefix, generation)
        return Fetched(prefix=prefix, generation=generation, status=FetchStatus.SKIPPED)
    summary = read_summary(source, prefix, generation)
    if summary is None:
        message = f"the summary of {prefix}/{generation} is missing"
        raise StagingError(message)
    try:
        check_signature(source, summary, trust)
    except SigningError as error:
        message = f"{prefix}/{generation}: {error}"
        raise StagingError(message) from None
    partial = staging.line(prefix) / f"{generation}.partial-{os.getpid()}"
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(parents=True)
    try:
        _download(source, summary, settings, partial)
        (partial / "summary.json").write_text(summary.model_dump_json(indent=2), "utf-8")
        partial.rename(staging.line(prefix) / generation)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    staging.point(prefix, generation)
    removed = staging.trim(prefix, settings.keep)
    return Fetched(
        prefix=prefix, generation=generation, status=FetchStatus.FETCHED, removed=removed
    )


def activate(
    spec: Spec, generation: Generation, *, groups: tuple[GroupName, ...] = ()
) -> tuple[Channel, ...]:
    """Point the staging channel of each group's line at a staged generation."""
    if spec.fetch is None:
        message = "the spec needs a `fetch` section to activate a generation"
        raise StagingError(message)
    staging = Staging(spec.fetch)
    channels: list[Channel] = []
    for group in groups or tuple(spec.images):
        prefix = choose_line(
            staging.lines(spec.name, group), spec.fetch.engine_version, str(staging.root)
        )
        with staging.locked(prefix):
            if generation not in staging.staged(prefix):
                message = f"{generation} is not staged for {prefix}; staged: " + (
                    ", ".join(staging.staged(prefix)) or "none"
                )
                raise StagingError(message)
            channels.append(staging.point(prefix, generation))
    return tuple(channels)
