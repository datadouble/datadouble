# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""``prune``: retention for stores and staging.

For each image line, ``prune`` keeps the newest ``prune.keep`` generations
(``fetch.keep`` in staging), anything built within ``prune.keep_days``, and
whatever any channel points at. Of a group's engine versions, only the newest
``prune.keep_versions`` keep their generations; older versions keep only
their channels' targets, and their unclaimed pool copies are removed. A
channel's target is never removed. ``--dry-run`` reports what would go.
"""

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, assert_never

from pydantic import BaseModel, ConfigDict

from datadouble.channels import Channel
from datadouble.images import generations, image_lines, read_summary
from datadouble.names import Generation
from datadouble.replicate import CopyState, Line, Options, Replication
from datadouble.spec import Retention, Spec
from datadouble.staging import Staging, remove_directory
from datadouble.storage import Store

if TYPE_CHECKING:
    from datadouble.registry import Registry


class PruneError(Exception):
    """Retention that cannot be applied as asked."""


class PruneTarget(StrEnum):
    """Where to prune."""

    STORE = "store"
    """The build's stores (production)."""
    EXPORT = "export"
    """The export stores ``promote`` writes to (production)."""
    STAGING = "staging"
    """Staging (non-production)."""


class Pruned(BaseModel):
    """One generation removed (or that would be, on a dry run)."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    location: str
    prefix: str
    generation: Generation
    bytes: int


class PruneResult(BaseModel):
    """What ``prune`` removed, or would remove."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    dry_run: bool
    removed: tuple[Pruned, ...]
    copies_removed: tuple[str, ...] = ()

    @property
    def bytes(self) -> int:
        """Space recovered."""
        return sum(item.bytes for item in self.removed)


def _protected(channels: list[Channel]) -> set[Generation]:
    return {channel.generation for channel in channels}


def _store_channels(store: Store, prefix: str) -> list[Channel]:
    return [
        Channel.model_validate_json(data)
        for key in store.list(f"{prefix}/channels/")
        if (data := store.get_bytes(key)) is not None
    ]


@dataclass(frozen=True)
class _Rules:
    keep: int
    cutoff: dt.datetime
    current_version: bool
    protected: set[Generation]


def _doomed(
    known: list[Generation], built: Callable[[Generation], dt.datetime | None], rules: _Rules
) -> list[Generation]:
    """The generations retention removes, oldest first."""
    newest = set(known[-rules.keep :]) if rules.current_version else set[Generation]()
    removable: list[Generation] = []
    for generation in known:
        when = built(generation)
        recent = rules.current_version and when is not None and when >= rules.cutoff
        if generation not in newest and generation not in rules.protected and not recent:
            removable.append(generation)
    return removable


def _prune_store(
    spec: Spec, store: Store, retention: Retention, cutoff: dt.datetime, *, dry_run: bool
) -> list[Pruned]:
    removed: list[Pruned] = []
    for group in spec.images:
        lines = image_lines(store, spec.name, group)
        current = set(lines[-retention.keep_versions :])
        for prefix in lines:
            summaries = {g: read_summary(store, prefix, g) for g in generations(store, prefix)}
            built = {g: s.built_at for g, s in summaries.items() if s is not None}
            doomed = _doomed(
                list(summaries),
                built.get,
                _Rules(
                    keep=retention.keep,
                    cutoff=cutoff,
                    current_version=prefix in current,
                    protected=_protected(_store_channels(store, prefix)),
                ),
            )
            for generation in doomed:
                summary = summaries[generation]
                size = sum(a.digest.size for a in summary.archives) if summary else 0
                removed.append(
                    Pruned(
                        location=store.describe(), prefix=prefix, generation=generation, bytes=size
                    )
                )
                if not dry_run:
                    for key in store.list(f"{prefix}/{generation}."):
                        store.delete(key)
    return removed


def _size(root: Path) -> int:
    return sum(item.stat().st_size for item in root.rglob("*") if item.is_file())


def _prune_staging(
    spec: Spec, registry: "Registry", retention: Retention, *, dry_run: bool
) -> tuple[list[Pruned], list[str]]:
    if spec.fetch is None:
        message = "the spec needs a `fetch` section to prune staging"
        raise PruneError(message)
    staging = Staging(spec.fetch)
    removed: list[Pruned] = []
    copies_removed: list[str] = []
    for group in spec.images:
        lines = sorted(
            staging.lines(spec.name, group),
            key=lambda p: tuple(int(x) for x in p.rsplit("-", 1)[-1].split(".") if x.isdigit()),
        )
        current = set(lines[-retention.keep_versions :])
        for prefix in lines:
            with staging.locked(prefix):
                channel = staging.channel(prefix)
                staged = staging.staged(prefix)
                doomed = _doomed(
                    staged,
                    lambda _generation: None,
                    _Rules(
                        keep=spec.fetch.keep,
                        cutoff=dt.datetime.max.replace(tzinfo=dt.UTC),
                        current_version=prefix in current,
                        protected=_protected([channel] if channel else []),
                    ),
                )
                for generation in doomed:
                    path = staging.line(prefix) / generation
                    removed.append(
                        Pruned(
                            location=str(staging.root),
                            prefix=prefix,
                            generation=generation,
                            bytes=_size(path),
                        )
                    )
                    if not dry_run:
                        remove_directory(path)
                if prefix not in current and spec.replicate is not None:
                    copies_removed.extend(
                        _retire_pool(spec, registry, staging, prefix, dry_run=dry_run)
                    )
    return removed, copies_removed


def _retire_pool(
    spec: Spec, registry: "Registry", staging: Staging, prefix: str, *, dry_run: bool
) -> list[str]:
    work = Replication(spec, registry, Options())
    line = Line(staging, prefix)
    copies = line.load()
    unclaimed = [c for c in copies if not c.claimed or c.state is CopyState.CREATING]
    if not dry_run:
        for copy in unclaimed:
            work.remove(line, copies, copy)
    return [copy.id for copy in unclaimed]


def prune(
    spec: Spec,
    registry: "Registry",
    target: PruneTarget,
    *,
    dry_run: bool = False,
    clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(tz=dt.UTC),
) -> PruneResult:
    """Apply retention to one kind of place."""
    retention = spec.prune
    cutoff = clock() - dt.timedelta(days=retention.keep_days)
    match target:
        case PruneTarget.STORE | PruneTarget.EXPORT:
            entries = (
                spec.store
                if target is PruneTarget.STORE
                else (spec.promote.to if spec.promote else ())
            )
            if not entries:
                message = f"the spec configures no {target} stores"
                raise PruneError(message)
            removed = [
                item
                for entry in entries
                for kind, settings in entry.items()
                for item in _prune_store(
                    spec, registry.store(kind, settings), retention, cutoff, dry_run=dry_run
                )
            ]
            return PruneResult(dry_run=dry_run, removed=tuple(removed))
        case PruneTarget.STAGING:
            staged, copies = _prune_staging(spec, registry, retention, dry_run=dry_run)
            return PruneResult(dry_run=dry_run, removed=tuple(staged), copies_removed=tuple(copies))
        case _:
            assert_never(target)
