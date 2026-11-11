# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The Docker replicate adapter for Datadouble.

Spec settings::

    replicate:
      using:
        docker-volume:
          kind: instance            # or volume (the default)
          server_image: postgres:18-trixie@sha256:...   # optional
          host: db-clones.internal  # how consumers reach published ports
          network: ci               # optional

A copy is a volume named ``datadouble-<copy id>``, filled by streaming the
staged image into a short-lived container as a tar archive, so it works the
same against a local or a remote daemon. An instance copy also runs the
engine's server on the volume, with a generated password. Everything is done
with the ``docker`` command, so ``DOCKER_HOST`` and contexts apply.
"""

import io
import os
import secrets
import shutil
import subprocess
import tarfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, JsonValue

from datadouble.engine import ServerPlan
from datadouble.images import ImageSummary
from datadouble.registry import installed
from datadouble.replicate import ClaimState, Copy, CopyKind, Made, ReplicateError, Replicator
from datadouble.settings import parse_settings

_READY_SECONDS = 120


class DockerSettings(BaseModel):
    """Settings for the ``docker-volume`` replicate adapter."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    kind: CopyKind = CopyKind.VOLUME
    server_image: str | None = None
    host: str = "localhost"
    """The address consumers use for published ports: the Docker host."""
    network: str | None = None
    """A Docker network instances join; they are reached by name there, too."""


def docker_binary() -> str:
    """The ``docker`` command."""
    found = shutil.which("docker")
    if found is None:
        message = "the docker command was not found"
        raise ReplicateError(message)
    return found


def _docker(
    arguments: list[str],
    action: str,
    *,
    stdin: bytes | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    result = subprocess.run(  # noqa: S603 - docker, with arguments built here
        [docker_binary(), *arguments],
        input=stdin,
        capture_output=True,
        check=False,
        env={**os.environ, **(env or {})},
    )
    if result.returncode != 0:
        lines = result.stderr.decode(errors="replace").strip().splitlines()
        message = f"{action} failed: {lines[-1] if lines else f'status {result.returncode}'}"
        raise ReplicateError(message)
    return result.stdout.decode()


def _archive(image: Path) -> bytes:
    """The image directory's contents as an uncompressed tar archive."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for item in sorted(image.iterdir()):
            archive.add(item, arcname=item.name)
    return buffer.getvalue()


def _labels(copy_id: str, summary: ImageSummary) -> list[str]:
    identity = summary.identity
    labels = {
        "datadouble.copy": copy_id,
        "datadouble.spec": identity.spec,
        "datadouble.group": identity.group,
        "datadouble.engine": f"{identity.engine}-{identity.engine_version}",
        "datadouble.generation": identity.generation,
    }
    return [argument for key, value in labels.items() for argument in ("--label", f"{key}={value}")]


class Serves(Protocol):
    """The part of an engine adapter this adapter needs."""

    def server(self, image: Path, server_image: str | None, /) -> ServerPlan:
        """How to serve a copy of ``image``."""
        ...


class DockerReplicator:
    """Volume and instance copies on a Docker daemon."""

    def __init__(self, settings: DockerSettings, engines: Callable[[str], Serves]) -> None:
        """Use ``engines`` to find the engine adapter for an image."""
        self.settings = settings
        self.engines = engines

    @property
    def kind(self) -> CopyKind:
        """Volumes or instances, as configured."""
        return self.settings.kind

    def create(self, copy_id: str, image: Path, summary: ImageSummary) -> Made:
        """Fill a volume from the image; for an instance, start the server on it."""
        plan = self.engines(summary.identity.engine).server(image, self.settings.server_image)
        name = f"datadouble-{copy_id}"
        labels = _labels(copy_id, summary)
        _docker(["volume", "create", *labels, name], "creating the volume")
        fill = f"tar -x -C {plan.mount} && chown -R {plan.owner} {plan.mount}"
        _docker(
            [
                "run",
                "--rm",
                "--interactive",
                "--user",
                "0:0",
                "--entrypoint",
                "sh",
                "--volume",
                f"{name}:{plan.mount}",
                plan.image,
                "-c",
                fill,
            ],
            "filling the volume",
            stdin=_archive(image),
        )
        if self.settings.kind is CopyKind.VOLUME:
            return Made(location={"volume": name})
        return self._start(name, labels, plan)

    def _start(self, name: str, labels: list[str], plan: ServerPlan) -> Made:
        password = secrets.token_hex(16)
        network = ["--network", self.settings.network] if self.settings.network else []
        # The password reaches the container through the docker command's own
        # environment, so it never appears on a command line.
        _docker(
            [
                "run",
                "--detach",
                "--name",
                name,
                *labels,
                *network,
                "--user",
                plan.owner,
                "--volume",
                f"{name}:{plan.mount}",
                "--env",
                "DATADOUBLE_PASSWORD",
                "--publish",
                str(plan.port),
                "--entrypoint",
                plan.command[0],
                plan.image,
                *plan.command[1:],
            ],
            "starting the server",
            env={"DATADOUBLE_PASSWORD": password},
        )
        deadline = time.monotonic() + _READY_SECONDS
        while not self._ready(name, plan):
            if time.monotonic() > deadline:
                message = f"the server in {name} was not ready after {_READY_SECONDS} seconds"
                raise ReplicateError(message)
            time.sleep(1)
        published = _docker(["port", name, f"{plan.port}/tcp"], "reading the port")
        port = int(published.splitlines()[0].rsplit(":", 1)[1])
        return Made(
            location={
                "host": self.settings.host,
                "port": port,
                "database": plan.database,
                "user": plan.user,
                "container": name,
                "volume": name,
            },
            secrets={"password": password},
        )

    @staticmethod
    def _ready(name: str, plan: ServerPlan) -> bool:
        """Whether the server answers; raises if its container has stopped."""
        running = _docker(["inspect", "--format", "{{.State.Running}}", name], "inspecting")
        if running.strip() != "true":
            logs = subprocess.run(  # noqa: S603 - docker, with arguments built here
                [docker_binary(), "logs", "--tail", "5", name],
                capture_output=True,
                text=True,
                check=False,
            )
            lines = (logs.stdout + logs.stderr).strip().splitlines()
            last = lines[-1] if lines else "no output"
            message = f"the server in {name} stopped: {last}"
            raise ReplicateError(message)
        check = subprocess.run(  # noqa: S603 - docker, with arguments built here
            [docker_binary(), "exec", name, *plan.ready], capture_output=True, check=False
        )
        return check.returncode == 0

    def destroy(self, copy: Copy) -> None:
        """Remove the container, if any, and the volume."""
        name = f"datadouble-{copy.id}"
        if self.settings.kind is CopyKind.INSTANCE:
            subprocess.run(  # noqa: S603 - docker, with arguments built here
                [docker_binary(), "rm", "--force", "--volumes", name],
                capture_output=True,
                check=False,
            )
        subprocess.run(  # noqa: S603 - docker, with arguments built here
            [docker_binary(), "volume", "rm", "--force", name], capture_output=True, check=False
        )

    def claim_state(self, _copy: Copy, /) -> ClaimState:
        """Claims are recorded by ``claim``."""
        return ClaimState.UNKNOWN


def docker_replicator(settings: Mapping[str, JsonValue]) -> Replicator:
    """The entry point named in ``datadouble.replicators``."""
    registry = installed()
    return DockerReplicator(
        parse_settings(DockerSettings, settings, "docker-volume replicate"), registry.engine
    )
