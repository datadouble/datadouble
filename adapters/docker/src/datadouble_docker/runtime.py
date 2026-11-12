# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The Docker container runtime: engine software on any Docker-compatible daemon.

Spec settings::

    runtime:
      docker:
        publish: 127.0.0.1   # the daemon host's address server ports are published on
        address: 127.0.0.1   # where Datadouble reaches published ports
        network: builds      # optional: a network every container joins

Paths are bind-mounted at the same absolute path, so the work directory must
exist at that path on the daemon's host: true for a local daemon, and arranged
with a shared volume for a remote one. Containers run as Datadouble's own user
and group. Everything is done with the ``docker`` command, so ``DOCKER_HOST``
and contexts apply; secrets reach containers through the command's own
environment, never its arguments.
"""

import os
import secrets
import shutil
import subprocess
import time
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from datadouble.runtime import (
    Completed,
    Container,
    ContainerError,
    ContainerRuntime,
    Endpoint,
    absolute,
    check,
    identity,
)
from datadouble.settings import parse_settings

_OWNER = "datadouble.owner"


class DockerRuntimeSettings(BaseModel):
    """Settings for the ``docker`` container runtime."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    publish: str = "127.0.0.1"
    """The daemon host's address server ports are published on."""
    address: str = "127.0.0.1"
    """Where Datadouble reaches published ports."""
    network: str | None = None
    """A Docker network every container joins, for reaching the source."""
    ready_seconds: int = Field(default=300, ge=1)
    stop_seconds: int = Field(default=300, ge=1)


def _docker(arguments: Sequence[str], env: Mapping[str, str] | None = None) -> Completed:
    binary = shutil.which("docker")
    if binary is None:
        message = "the docker command was not found"
        raise ContainerError(message)
    result = subprocess.run(  # noqa: S603 - docker, with arguments built here
        [binary, *arguments],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )
    return Completed(status=result.returncode, stdout=result.stdout, stderr=result.stderr)


def _options(container: Container, network: str | None) -> list[str]:
    """``docker run`` options shared by jobs and servers."""
    options = ["--label", f"{_OWNER}={absolute([container.owner])[0]}", "--user", identity()]
    if network:
        options += ["--network", network]
    for path in absolute(container.mounts):
        options += ["--volume", f"{path}:{path}"]
    for key, value in container.env.items():
        options += ["--env", f"{key}={value}"]
    for key in container.secrets:
        options += ["--env", key]
    return options


class DockerServer:
    """A server container started by :class:`DockerRuntime`."""

    def __init__(self, name: str, endpoint: Endpoint) -> None:
        """Wrap the running container ``name``."""
        self.name = name
        self._endpoint = endpoint

    @property
    def endpoint(self) -> Endpoint:
        """The published port, at the configured address."""
        return self._endpoint

    def execute(self, command: Sequence[str], env: Mapping[str, str] | None = None) -> Completed:
        """``docker exec``; ``env`` reaches the command through docker's environment."""
        options = [argument for key in env or {} for argument in ("--env", key)]
        return _docker(["exec", *options, self.name, *command], env)


class DockerRuntime:
    """Runs containers with the ``docker`` command."""

    def __init__(self, settings: DockerRuntimeSettings) -> None:
        """Use ``settings`` for every container."""
        self.settings = settings

    def run(self, container: Container) -> Completed:
        """``docker run --rm``, waiting for the command to finish."""
        return _docker(
            [
                "run",
                "--rm",
                *_options(container, self.settings.network),
                "--entrypoint",
                container.command[0],
                container.image,
                *container.command[1:],
            ],
            container.secrets,
        )

    @contextmanager
    def serve(self, container: Container) -> Generator[DockerServer]:
        """Start a detached server, publish its port, and stop it cleanly afterwards."""
        if container.port is None:
            message = "a server container needs a port"
            raise ContainerError(message)
        name = f"datadouble-{secrets.token_hex(6)}"
        check(
            _docker(
                [
                    "run",
                    "--detach",
                    "--name",
                    name,
                    *_options(container, self.settings.network),
                    "--publish",
                    f"{self.settings.publish}::{container.port}",
                    "--stop-timeout",
                    str(self.settings.stop_seconds),
                    "--entrypoint",
                    container.command[0],
                    container.image,
                    *container.command[1:],
                ],
                container.secrets,
            ),
            f"starting {container.image}",
        )
        try:
            self._wait(name, container)
            yield DockerServer(name, self._endpoint(name, container.port))
            self._stop(name)
        finally:
            _docker(["rm", "--force", name])

    def _endpoint(self, name: str, port: int) -> Endpoint:
        published = check(_docker(["port", name, f"{port}/tcp"]), "reading the published port")
        return Endpoint(
            host=self.settings.address,
            port=int(published.stdout.splitlines()[0].rsplit(":", 1)[1]),
        )

    def _wait(self, name: str, container: Container) -> None:
        deadline = time.monotonic() + self.settings.ready_seconds
        while True:
            state = _docker(["inspect", "--format", "{{.State.Running}}", name])
            if state.stdout.strip() != "true":
                raise ContainerError(self._stopped(name, "stopped while starting"))
            if _docker(["exec", name, *container.ready]).status == 0:
                return
            if time.monotonic() > deadline:
                message = (
                    f"{container.image} was not ready after {self.settings.ready_seconds} seconds"
                )
                raise ContainerError(message)
            time.sleep(1)

    def _stop(self, name: str) -> None:
        """Stop the server with its normal signal; it must exit cleanly."""
        check(_docker(["stop", name]), "stopping the server")
        status = _docker(["inspect", "--format", "{{.State.ExitCode}}", name]).stdout.strip()
        if status != "0":
            raise ContainerError(self._stopped(name, f"exited with status {status}"))

    @staticmethod
    def _stopped(name: str, what: str) -> str:
        logs = _docker(["logs", "--tail", "5", name])
        lines = (logs.stdout + logs.stderr).strip().splitlines()
        return f"the server {what}: {lines[-1] if lines else 'no output'}"

    def release(self, owner: Path) -> None:
        """Remove containers labelled with ``owner`` or a directory under it."""
        root = absolute([owner])[0]
        listed = _docker(
            [
                "ps",
                "--all",
                "--filter",
                f"label={_OWNER}",
                "--format",
                f'{{{{.ID}}}} {{{{.Label "{_OWNER}"}}}}',
            ]
        )
        for line in listed.stdout.splitlines():
            container, _, labelled = line.partition(" ")
            if Path(labelled).is_relative_to(root):
                _docker(["rm", "--force", container])


def docker_runtime(settings: Mapping[str, JsonValue]) -> ContainerRuntime:
    """The entry point named in ``datadouble.runtimes``."""
    return DockerRuntime(parse_settings(DockerRuntimeSettings, settings, "docker runtime"))
