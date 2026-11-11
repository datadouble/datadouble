# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""``replicate``, ``claim`` and ``release``: copies made from staging.

Each image line in staging has a registry of the copies made from it,
``copies.json``, changed only under the line's lock. A copy is recorded as
``creating`` before the replicate adapter makes it, so a copy that never
completed is found and removed later.

- **Pool mode** keeps ``replicate.size`` unclaimed copies of the active
  generation. Unclaimed copies of any other generation are stale: each is
  removed only after a fresh copy replaces it, so the pool never empties
  (``--all-at-once`` removes them first, for urgent replacement). Consumers
  ``claim`` a copy; a claimed copy belongs to its consumer until released.
- **On-demand mode** makes ``replicate.size`` copies for a named owner, which
  expire after ``replicate.expires_hours``.

Replicate adapters register under ``datadouble.replicators``; the entry point
names a callable that takes the adapter's settings and returns a
:class:`Replicator`.
"""

import datetime as dt
import os
import shutil
import subprocess
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, JsonValue, TypeAdapter

from datadouble.images import ImageSummary
from datadouble.names import Generation, GroupName
from datadouble.settings import parse_settings
from datadouble.spec import Replicate, ReplicateMode, Spec
from datadouble.staging import Staging, StagingError, choose_line

if TYPE_CHECKING:
    from datadouble.registry import Registry


class ReplicateError(Exception):
    """A copy that cannot be made, claimed or released."""


class CopyKind(StrEnum):
    """What a copy is."""

    VOLUME = "volume"
    """Files a consumer starts its own server on."""
    INSTANCE = "instance"
    """A running server with an endpoint."""


class ClaimState(StrEnum):
    """What a replicate adapter can see of a copy's consumer."""

    UNKNOWN = "unknown"
    """The adapter cannot tell; ``claim`` records claims."""
    FREE = "free"
    CLAIMED = "claimed"
    RETURNED = "returned"
    """The consumer is done with it (a deleted Kubernetes claim): remove the copy."""


class CopyState(StrEnum):
    """Whether a copy is finished."""

    CREATING = "creating"
    READY = "ready"


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class Made(_Model):
    """What a replicate adapter reports about a copy it made."""

    location: dict[str, JsonValue]
    """Where the copy is: a path, a volume name, or a host, port and database."""
    secrets: dict[str, str] = {}
    """The copy's own credentials, for secret-capable announce targets only."""


class Copy(_Model):
    """One copy, as the registry records it."""

    id: str
    prefix: str
    generation: Generation
    kind: CopyKind
    mode: ReplicateMode
    adapter: str
    state: CopyState
    created_at: AwareDatetime
    built_at: AwareDatetime
    """When the image the copy was made from was built."""
    owner: str | None = None
    claimed: bool = False
    expires_at: AwareDatetime | None = None
    location: dict[str, JsonValue] = {}
    secrets: dict[str, str] = {}

    def public(self) -> "Copy":
        """The copy without its credentials."""
        return self.model_copy(update={"secrets": {}})


class Replicator(Protocol):
    """Makes and destroys copies of staged images."""

    @property
    def kind(self) -> CopyKind:
        """The kind of copy this adapter makes."""
        ...

    def create(self, copy_id: str, image: Path, summary: ImageSummary) -> Made:
        """Copy an unpacked image; the copy must not be visible until it is complete."""
        ...

    def destroy(self, copy: Copy) -> None:
        """Remove a copy, complete or not; removing one that is gone is not an error."""
        ...

    def claim_state(self, copy: Copy, /) -> ClaimState:
        """Whether a consumer has taken the copy by the adapter's own means, if it can tell."""
        ...


type ReplicatorFactory = Callable[[Mapping[str, JsonValue]], Replicator]

_COPIES = TypeAdapter(list[Copy])


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


class Line:
    """One image line's copies, read and written under its lock."""

    def __init__(self, staging: Staging, prefix: str) -> None:
        """Work on ``prefix`` in ``staging``."""
        self.staging = staging
        self.prefix = prefix
        self.path = staging.line(prefix) / "copies.json"

    def load(self) -> list[Copy]:
        """The recorded copies."""
        return _COPIES.validate_json(self.path.read_bytes()) if self.path.is_file() else []

    def save(self, copies: list[Copy]) -> None:
        """Record copies; the file holds clone credentials, so only its owner can read it."""
        partial = self.path.with_name(f"copies.json.{os.getpid()}")
        descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_COPIES.dump_json(copies, indent=2))
        partial.replace(self.path)


class ReplicateResult(_Model):
    """What a replicate, claim or release did."""

    created: tuple[Copy, ...] = ()
    removed: tuple[Copy, ...] = ()
    copies: tuple[Copy, ...] = ()
    """Every copy now recorded for the lines involved, without credentials."""


@dataclass(frozen=True)
class Options:
    """Which copies an operation works on."""

    groups: tuple[GroupName, ...] = ()
    owner: str | None = None
    copy_ids: tuple[str, ...] = ()
    expired: bool = False
    """For ``release``: on-demand copies past their expiry."""
    all_at_once: bool = False
    """For ``replicate`` in pool mode: remove stale copies before making new ones."""
    clock: Callable[[], dt.datetime] = _now


class Replication:
    """Copies of one spec's image lines, made by its replicate adapter."""

    def __init__(self, spec: Spec, registry: "Registry", options: Options) -> None:
        """Use the spec's replicate adapter."""
        if spec.fetch is None or spec.replicate is None:
            message = "the spec needs `fetch` and `replicate` sections to make copies"
            raise ReplicateError(message)
        self.spec = spec
        self.settings: Replicate = spec.replicate
        self.staging = Staging(spec.fetch)
        self.options = options
        name, settings = next(iter(spec.replicate.using.items()))
        factory = registry.replicators.get(name)
        if factory is None:
            message = f"no replicate adapter named {name!r} is installed"
            raise ReplicateError(message)
        self.adapter = name
        self.replicator = factory(settings)
        self.created: list[Copy] = []
        self.removed: list[Copy] = []
        self.lines: list[Line] = []

    def each_line(self) -> list[Line]:
        """The image lines the options name, remembered for the result."""
        version = self.spec.fetch.engine_version if self.spec.fetch else None
        for group in self.options.groups or tuple(self.spec.images):
            prefix = choose_line(
                self.staging.lines(self.spec.name, group), version, str(self.staging.root)
            )
            self.lines.append(Line(self.staging, prefix))
        return self.lines

    def active(self, prefix: str) -> Generation:
        """The generation the staging channel points at."""
        channel = self.staging.channel(prefix)
        if channel is None:
            message = f"nothing is active in staging for {prefix}; run fetch first"
            raise ReplicateError(message)
        return channel.generation

    def make(self, line: Line, copies: list[Copy], generation: Generation) -> None:
        """Make one copy, recording it as creating until the adapter is done."""
        mode = self.settings.mode
        now = self.options.clock()
        summary = self.staging.summary(line.prefix, generation)
        on_demand = mode is ReplicateMode.ON_DEMAND
        record = Copy(
            id=uuid.uuid4().hex[:12],
            prefix=line.prefix,
            generation=generation,
            kind=self.replicator.kind,
            mode=mode,
            adapter=self.adapter,
            state=CopyState.CREATING,
            created_at=now,
            built_at=summary.built_at,
            owner=self.options.owner if on_demand else None,
            claimed=on_demand,
            expires_at=now + dt.timedelta(hours=self.settings.expires_hours) if on_demand else None,
        )
        copies.append(record)
        line.save(copies)
        made = self.replicator.create(
            record.id, self.staging.image(line.prefix, generation), summary
        )
        ready = record.model_copy(
            update={"state": CopyState.READY, "location": made.location, "secrets": made.secrets}
        )
        copies[copies.index(record)] = ready
        line.save(copies)
        self.created.append(ready)

    def remove(self, line: Line, copies: list[Copy], copy: Copy) -> None:
        """Destroy a copy and forget it."""
        self.replicator.destroy(copy)
        copies.remove(copy)
        line.save(copies)
        self.removed.append(copy)

    def tidy(self, line: Line) -> list[Copy]:
        """Remove copies that never completed or were returned; record observed claims."""
        copies = line.load()
        states = {
            copy.id: self.replicator.claim_state(copy)
            for copy in copies
            if copy.state is CopyState.READY
        }
        for copy in [
            c
            for c in copies
            if c.state is CopyState.CREATING or states.get(c.id) is ClaimState.RETURNED
        ]:
            self.remove(line, copies, copy)
        for index, copy in enumerate(copies):
            if not copy.claimed and states.get(copy.id) is ClaimState.CLAIMED:
                copies[index] = copy.model_copy(update={"claimed": True})
        line.save(copies)
        return copies

    def fill_pool(self, line: Line, copies: list[Copy], active: Generation) -> None:
        """Make fresh copies until the pool is full, retiring one stale copy per fresh one."""

        def unclaimed(*, fresh: bool) -> list[Copy]:
            return [
                c
                for c in copies
                if not c.claimed
                and c.state is CopyState.READY
                and (c.generation == active) == fresh
            ]

        if self.options.all_at_once:
            for copy in unclaimed(fresh=False):
                self.remove(line, copies, copy)
        while len(unclaimed(fresh=True)) < self.settings.size:
            self.make(line, copies, active)
            stale = unclaimed(fresh=False)
            if stale:
                self.remove(line, copies, stale[0])
        for copy in unclaimed(fresh=False):
            self.remove(line, copies, copy)

    def result(self) -> "ReplicateResult":
        """What was created and removed, and every copy now recorded."""
        return ReplicateResult(
            created=tuple(c.public() for c in self.created),
            removed=tuple(c.public() for c in self.removed),
            copies=tuple(c.public() for line in self.lines for c in line.load()),
        )


def replicate(spec: Spec, registry: "Registry", options: Options) -> ReplicateResult:
    """Fill the pool, or make copies for ``options.owner`` in on-demand mode."""
    work = Replication(spec, registry, options)
    on_demand = work.settings.mode is ReplicateMode.ON_DEMAND
    if on_demand and not options.owner:
        message = "on-demand copies need an owner (the pipeline, merge request or environment)"
        raise ReplicateError(message)
    for line in work.each_line():
        with work.staging.locked(line.prefix):
            active = work.active(line.prefix)
            copies = work.tidy(line)
            if on_demand:
                for _ in range(work.settings.size):
                    work.make(line, copies, active)
            else:
                work.fill_pool(line, copies, active)
    return work.result()


def claim(spec: Spec, registry: "Registry", options: Options) -> ReplicateResult:
    """Take one unclaimed pool copy per group for ``options.owner``, active generation first."""
    if not options.owner:
        message = "say who is claiming the copy: an owner"
        raise ReplicateError(message)
    work = Replication(spec, registry, options)
    for line in work.each_line():
        with work.staging.locked(line.prefix):
            active = work.active(line.prefix)
            copies = work.tidy(line)
            free = [c for c in copies if not c.claimed and c.state is CopyState.READY]
            if not free:
                message = f"the pool for {line.prefix} has no unclaimed copy; run replicate"
                raise ReplicateError(message)
            free.sort(key=lambda c: (c.generation != active, c.created_at))
            taken = free[0].model_copy(update={"claimed": True, "owner": options.owner})
            copies[copies.index(free[0])] = taken
            line.save(copies)
            work.created.append(taken)
    return work.result().model_copy(update={"created": tuple(work.created)})


def release(spec: Spec, registry: "Registry", options: Options) -> ReplicateResult:
    """Destroy copies by ID, by owner, or because they expired; and incomplete ones."""
    if not (options.copy_ids or options.owner or options.expired):
        message = "say which copies to release: copy IDs, an owner, or expired ones"
        raise ReplicateError(message)
    work = Replication(spec, registry, options)
    now = options.clock()
    for line in work.each_line():
        with work.staging.locked(line.prefix):
            copies = work.tidy(line)
            for copy in [
                c
                for c in copies
                if c.id in options.copy_ids
                or (options.owner is not None and c.owner == options.owner)
                or (options.expired and c.expires_at is not None and c.expires_at <= now)
            ]:
                work.remove(line, copies, copy)
    return work.result()


def copies_of(
    spec: Spec, groups: tuple[GroupName, ...] = (), *, with_secrets: bool = False
) -> tuple[Copy, ...]:
    """Every recorded copy; credentials only when asked, for announcing."""
    if spec.fetch is None:
        message = "the spec needs a `fetch` section"
        raise StagingError(message)
    staging = Staging(spec.fetch)
    return tuple(
        c if with_secrets else c.public()
        for group in groups or tuple(spec.images)
        for prefix in staging.lines(spec.name, group)
        for c in Line(staging, prefix).load()
    )


# The filesystem replicate adapter


class FilesystemReplicateSettings(_Model):
    """Settings for the ``filesystem`` replicate adapter."""

    path: Path


class FilesystemReplicator:
    """Volume copies as directories, sharing blocks where the filesystem allows."""

    def __init__(self, settings: FilesystemReplicateSettings) -> None:
        """Make copies under ``settings.path``."""
        self.root = settings.path

    @property
    def kind(self) -> CopyKind:
        """Volumes."""
        return CopyKind.VOLUME

    def create(self, copy_id: str, image: Path, summary: ImageSummary) -> Made:
        """Copy under a temporary name, then rename."""
        self.root.mkdir(parents=True, exist_ok=True)
        partial = self.root / f".{copy_id}.partial"
        result = subprocess.run(  # noqa: S603 - fixed program, paths from this module
            ["cp", "-a", "--reflink=auto", str(image), str(partial)],  # noqa: S607 - coreutils cp
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            shutil.rmtree(partial, ignore_errors=True)
            message = f"copying {summary.identity.generation} failed: {result.stderr.strip()}"
            raise ReplicateError(message)
        final = self.root / copy_id
        partial.rename(final)
        return Made(location={"path": str(final)})

    def destroy(self, copy: Copy) -> None:
        """Remove the copy's directory, or its partial one."""
        for path in (self.root / copy.id, self.root / f".{copy.id}.partial"):
            shutil.rmtree(path, ignore_errors=True)

    def claim_state(self, _copy: Copy, /) -> ClaimState:
        """Claims are recorded by ``claim``; the filesystem cannot tell."""
        return ClaimState.UNKNOWN


def filesystem_replicator(settings: Mapping[str, JsonValue]) -> Replicator:
    """The entry point named in ``datadouble.replicators``."""
    return FilesystemReplicator(
        parse_settings(FilesystemReplicateSettings, settings, "filesystem replicate")
    )
