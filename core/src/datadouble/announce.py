# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""``announce``: telling consumers where their copies are.

Announcing never writes data; ``replicate`` has already made the copies. Each
announcement carries the copy's labels (spec, group, engine, engine version,
owner) and provenance (generation, build time), and where to reach it: a
volume, or connection parameters. A copy's credentials go only to
secret-capable targets.

Announce adapters register under ``datadouble.announcers``; the entry point
names a callable that takes the adapter's settings and returns an
:class:`Announcer`. ``dotenv`` and ``ci-outputs`` live here.
"""

import json
import os
import re
from collections.abc import Callable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict, JsonValue

from datadouble.replicate import Copy, record_announced
from datadouble.secrets import SecretError
from datadouble.settings import SettingsError, parse_settings
from datadouble.spec import Spec
from datadouble.staging import StagingError

if TYPE_CHECKING:
    from datadouble.registry import Registry


class AnnounceError(Exception):
    """An announcement that could not be written, or withdrawn."""

    def __init__(self, message: str, written: tuple[str, ...] = ()) -> None:
        """``written`` is what the adapter wrote before it failed, so it is recorded."""
        super().__init__(message)
        self.written = written


class Announced(BaseModel):
    """One copy, as announced: labels, provenance and location."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    copy_id: str
    kind: str
    labels: dict[str, str]
    location: dict[str, JsonValue]
    secrets: dict[str, str] = {}


def describe(copy: Copy) -> Announced:
    """What consumers are told about a copy."""
    spec, group, line = copy.prefix.split("/")
    engine, _, version = line.rpartition("-")
    labels = {
        "spec": spec,
        "group": group,
        "engine": engine,
        "engine-version": version,
        "generation": str(copy.generation),
        "built-at": copy.built_at.isoformat(),
        "mode": str(copy.mode),
    }
    if copy.owner:
        labels["owner"] = copy.owner
    return Announced(
        copy_id=copy.id,
        kind=str(copy.kind),
        labels=labels,
        location=copy.location,
        secrets=copy.secrets,
    )


class Announcer(Protocol):
    """Writes where copies can be reached to one kind of target."""

    def announce(self, copies: tuple[Announced, ...]) -> tuple[str, ...]:
        """Announce copies; return what was written, for the response."""
        ...

    def withdraw(self, copy_ids: tuple[str, ...], announced: tuple[str, ...], /) -> tuple[str, ...]:
        """Remove announcements of released copies; return what was removed.

        ``announced`` is every target this ``announce`` entry reported for these
        copies, as the registry recorded it, so an adapter removes what it wrote
        where it wrote it. An adapter attempts every target, then raises one
        :class:`AnnounceError` naming each one it left behind.
        """
        ...


type AnnouncerFactory = Callable[[Mapping[str, JsonValue]], Announcer]

_NAME = re.compile(r"[^A-Z0-9]+")


def _variable(*parts: str) -> str:
    return _NAME.sub("_", "_".join(parts).upper()).strip("_")


def _flatten(copy: Announced, *, secrets: bool) -> dict[str, str]:
    values: dict[str, str] = {"COPY_ID": copy.copy_id, "KIND": copy.kind}
    values.update({_variable(k): v for k, v in copy.labels.items()})
    values.update(
        {_variable(k): v if isinstance(v, str) else json.dumps(v) for k, v in copy.location.items()}
    )
    if secrets:
        values.update({_variable(k): v for k, v in copy.secrets.items()})
    return values


def _quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'" if re.search(r"[^\w@%+=:,./-]", value) else value


class DotenvSettings(BaseModel):
    """Settings for the ``dotenv`` announce adapter."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    path: Path
    prefix: str = "DATADOUBLE"


class DotenvAnnouncer:
    """A dotenv file, readable only by its owner, for local and simple CI use.

    The first copy's values are unnumbered (``DATADOUBLE_PATH``); every copy also
    appears numbered (``DATADOUBLE_1_PATH``). Credentials are included.
    """

    def __init__(self, settings: DotenvSettings) -> None:
        """Write to ``settings.path``."""
        self.settings = settings

    def announce(self, copies: tuple[Announced, ...]) -> tuple[str, ...]:
        """Write the file, replacing what was there."""
        lines: list[str] = []
        prefix = self.settings.prefix
        for number, copy in enumerate(copies, start=1):
            values = _flatten(copy, secrets=True)
            if number == 1:
                lines.extend(f"{prefix}_{k}={_quote(v)}" for k, v in values.items())
            lines.extend(f"{prefix}_{number}_{k}={_quote(v)}" for k, v in values.items())
        path = self.settings.path
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("".join(f"{line}\n" for line in lines))
        return (f"dotenv:{path}",)

    def withdraw(
        self, copy_ids: tuple[str, ...], _announced: tuple[str, ...], /
    ) -> tuple[str, ...]:
        """Remove the file if it announced any of the released copies."""
        path = self.settings.path
        try:
            if not path.is_file():
                return ()
            text = path.read_text(encoding="utf-8")
            if not any(f"_COPY_ID={copy_id}\n" in text for copy_id in copy_ids):
                return ()
            path.unlink()
        except OSError as error:
            message = f"left behind: dotenv:{path} ({error.strerror})"
            raise AnnounceError(message) from None
        return (f"dotenv:{path}",)


class CiSystem(StrEnum):
    """Where ``ci-outputs`` writes."""

    AUTO = "auto"
    GITHUB = "github"
    GITLAB = "gitlab"
    FILE = "file"


class CiOutputsSettings(BaseModel):
    """Settings for the ``ci-outputs`` announce adapter."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    system: CiSystem = CiSystem.AUTO
    path: Path | None = None
    """For ``gitlab`` (a dotenv report) and ``file`` (JSON); ignored for ``github``."""
    prefix: str = "DATADOUBLE"


class CiOutputsAnnouncer:
    """Job outputs of the running CI system. Never includes credentials.

    GitHub Actions gets step outputs through ``GITHUB_OUTPUT``; GitLab CI a
    dotenv report (declare it under ``artifacts: reports: dotenv``); anything
    else a JSON file.
    """

    def __init__(self, settings: CiOutputsSettings, environ: Mapping[str, str]) -> None:
        """Choose the target from ``settings`` and the environment."""
        self.settings = settings
        self.environ = environ

    def _system(self) -> CiSystem:
        if self.settings.system is not CiSystem.AUTO:
            return self.settings.system
        if self.environ.get("GITHUB_OUTPUT"):
            return CiSystem.GITHUB
        if self.environ.get("GITLAB_CI"):
            return CiSystem.GITLAB
        return CiSystem.FILE

    def _path(self, system: CiSystem) -> Path:
        if system is CiSystem.GITHUB:
            return Path(self.environ.get("GITHUB_OUTPUT", ""))
        if self.settings.path is None:
            message = f"ci-outputs for {system} needs a `path`"
            raise AnnounceError(message)
        return self.settings.path

    def announce(self, copies: tuple[Announced, ...]) -> tuple[str, ...]:
        """Write the outputs."""
        system = self._system()
        path = self._path(system)
        if system is CiSystem.FILE:
            data = [
                copy.model_copy(update={"secrets": {}}).model_dump(mode="json") for copy in copies
            ]
            path.write_text(json.dumps({"copies": data}, indent=2) + "\n", encoding="utf-8")
            return (f"file:{path}",)
        prefix = self.settings.prefix
        lines: list[str] = []
        for number, copy in enumerate(copies, start=1):
            values = _flatten(copy, secrets=False)
            if number == 1:
                lines.extend(f"{prefix}_{k}={v}" for k, v in values.items())
            lines.extend(f"{prefix}_{number}_{k}={v}" for k, v in values.items())
        mode = "a" if system is CiSystem.GITHUB else "w"
        with path.open(mode, encoding="utf-8") as handle:
            handle.write("".join(f"{line}\n" for line in lines))
        return (f"{system}:{path}",)

    def withdraw(
        self, _copy_ids: tuple[str, ...], _announced: tuple[str, ...], /
    ) -> tuple[str, ...]:
        """Job outputs end with the job; there is nothing to remove."""
        return ()


def dotenv_announcer(settings: Mapping[str, JsonValue]) -> Announcer:
    """The ``dotenv`` entry point."""
    return DotenvAnnouncer(parse_settings(DotenvSettings, settings, "dotenv announce"))


def ci_outputs_announcer(settings: Mapping[str, JsonValue]) -> Announcer:
    """The ``ci-outputs`` entry point."""
    return CiOutputsAnnouncer(
        parse_settings(CiOutputsSettings, settings, "ci-outputs announce"), os.environ
    )


_WITHDRAW_FAILURES = (AnnounceError, SecretError, SettingsError, OSError, ValueError)
"""What one entry's withdrawal may fail with without stopping the others'."""


def _entries(spec: Spec) -> list[tuple[str, str, Mapping[str, JsonValue]]]:
    """Each ``announce`` entry's key, adapter name and settings.

    The key is the adapter's name, ``name#2`` for the second entry of the same adapter,
    and so on: positional per adapter, so reordering same-adapter entries changes it.
    """
    found: list[tuple[str, str, Mapping[str, JsonValue]]] = []
    seen: dict[str, int] = {}
    for entry in spec.announce:
        for name, settings in entry.items():
            seen[name] = seen.get(name, 0) + 1
            found.append((name if seen[name] == 1 else f"{name}#{seen[name]}", name, settings))
    return found


def _announcer(registry: "Registry", name: str, settings: Mapping[str, JsonValue]) -> Announcer:
    factory = registry.announcers.get(name)
    if factory is None:
        message = f"no announce adapter named {name!r} is installed"
        raise AnnounceError(message)
    return factory(settings)


def _announcers(spec: Spec, registry: "Registry") -> list[tuple[str, Announcer]]:
    """Each ``announce`` entry's adapter, by its key."""
    return [(key, _announcer(registry, name, settings)) for key, name, settings in _entries(spec)]


def announce(spec: Spec, registry: "Registry", copies: tuple[Copy, ...]) -> tuple[str, ...]:
    """Announce copies (with their credentials, for the targets allowed them) everywhere."""
    ready = tuple(copy for copy in copies if copy.state == "ready")
    if not ready:
        return ()
    described = tuple(describe(copy) for copy in ready)
    written: dict[str, tuple[str, ...]] = {}
    try:
        for key, announcer in _announcers(spec, registry):
            try:
                written[key] = announcer.announce(described)
            except AnnounceError as error:
                if error.written:
                    written[key] = error.written
                raise
    except BaseException as error:
        # What was written before the failure is recorded, so release finds it; a
        # failure to record is reported with the original error, not instead of it.
        if written:
            try:
                record_announced(spec, ready, written)
            except (StagingError, OSError, ValueError) as recording:
                error.add_note(f"recording what was announced failed too: {recording}")
        raise
    targets = tuple(target for entry in written.values() for target in entry)
    if written:
        try:
            record_announced(spec, ready, written)
        except (StagingError, OSError, ValueError) as error:
            message = (
                f"announced, but not recorded, so release will not withdraw: {', '.join(targets)}"
                f" ({error})"
            )
            raise AnnounceError(message) from error
    return targets


def withdraw(spec: Spec, registry: "Registry", copies: tuple[Copy, ...]) -> tuple[str, ...]:
    """Remove announcements of released copies everywhere, where they were announced."""
    if not copies:
        return ()
    copy_ids = tuple(copy.id for copy in copies)
    recorded: dict[str, tuple[str, ...]] = {}
    for copy in copies:
        for key, targets in copy.announced.items():
            recorded[key] = tuple(dict.fromkeys((*recorded.get(key, ()), *targets)))
    removed: list[str] = []
    failures: list[str] = []
    entries = _entries(spec)
    for key, name, settings in entries:
        announced = recorded.get(key, ())
        # Each entry is made and withdrawn on its own: the copies are already gone from
        # the registry, so one failing entry must not keep the others from running.
        # An adapter's own AnnounceError from withdraw names what it left behind; any
        # other failure (making the adapter, an OSError) gets the recorded targets added.
        try:
            announcer = _announcer(registry, name, settings)
        except _WITHDRAW_FAILURES as error:
            failures.append(f"{key}: {error} (recorded: {', '.join(announced) or 'nothing'})")
            continue
        try:
            removed.extend(announcer.withdraw(copy_ids, announced))
        except AnnounceError as error:
            failures.append(f"{key}: {error}")
        except _WITHDRAW_FAILURES as error:
            failures.append(f"{key}: {error} (recorded: {', '.join(announced) or 'nothing'})")
    current = {key for key, _, _ in entries}
    failures.extend(
        f"recorded by an announce entry no longer in the spec: {key}: {', '.join(targets)}"
        for key, targets in recorded.items()
        if key not in current and targets
    )
    if failures:
        message = "announcements left behind; remove them by hand: " + "; ".join(failures)
        raise AnnounceError(message)
    return tuple(removed)
