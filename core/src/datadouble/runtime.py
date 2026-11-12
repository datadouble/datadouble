# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Running engine software in the vendor's container images.

Working databases, restores for verification and engine tools such as
``pg_dump`` run in containers from the engine vendor's images, so Datadouble
itself needs no engine binaries. A container runtime adapter starts them:
``docker`` for any Docker-compatible daemon, ``kubernetes`` for pods.

Every container sees the paths it is given at the same absolute path as
Datadouble, and runs as Datadouble's own user and group, so files written on
either side can be read and removed by the other. Containers are labelled with
the stage directory they serve, so the core can remove what an interrupted
attempt left behind.

Runtime adapters register under the ``datadouble.runtimes`` entry point group;
the entry point names a callable that takes the adapter's settings and returns
a :class:`ContainerRuntime`.
"""

import os
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pydantic import JsonValue


class ContainerError(Exception):
    """A container that could not be run; the message is safe to show and log."""


@dataclass(frozen=True, slots=True)
class Container:
    """A container to run.

    ``secrets`` reach the container's environment through the runtime's secret
    channel, never a command line or a pod spec. ``port`` is the TCP port a
    server listens on; ``ready`` is a command run inside the container that
    succeeds once it accepts connections.
    """

    image: str
    command: tuple[str, ...]
    owner: Path
    """The stage directory the container serves, for cleanup after an interruption."""
    mounts: tuple[Path, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict[str, str])
    secrets: Mapping[str, str] = field(default_factory=dict[str, str])
    port: int | None = None
    ready: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Completed:
    """A finished command: its status and output."""

    status: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class Endpoint:
    """Where Datadouble reaches a server's port."""

    host: str
    port: int


class Server(Protocol):
    """A running server container."""

    @property
    def endpoint(self) -> Endpoint:
        """Where Datadouble connects to the server's port."""
        ...

    def execute(self, command: Sequence[str], env: Mapping[str, str] | None = None) -> Completed:
        """Run a command inside the container, as its user, and wait for it."""
        ...


class ContainerRuntime(Protocol):
    """Starts containers from vendor images."""

    def run(self, container: Container) -> Completed:
        """Run a container to completion and remove it."""
        ...

    def serve(self, container: Container) -> AbstractContextManager[Server]:
        """Start a server, wait until ``ready`` succeeds, and stop it cleanly on exit.

        Stopping sends the server its normal termination signal and waits, so
        its files are consistent once the block ends.
        """
        ...

    def release(self, owner: Path) -> None:
        """Remove every container labelled with ``owner`` or a directory under it."""
        ...


type RuntimeFactory = Callable[[Mapping[str, JsonValue]], ContainerRuntime]


def identity() -> str:
    """Datadouble's own ``uid:gid``, which containers run as."""
    return f"{os.getuid()}:{os.getgid()}"


def check(completed: Completed, what: str) -> Completed:
    """``completed`` if it succeeded; otherwise an error naming ``what`` and its last line.

    Engine adapters whose tools may print data values redact them first and
    use their own check instead.
    """
    if completed.status == 0:
        return completed
    lines = completed.stderr.strip().splitlines()
    message = f"{what} failed: {lines[-1] if lines else f'status {completed.status}'}"
    raise ContainerError(message)


def absolute(paths: Sequence[Path]) -> tuple[Path, ...]:
    """Paths made absolute, as the runtime mounts them."""
    return tuple(path.resolve() for path in paths)
