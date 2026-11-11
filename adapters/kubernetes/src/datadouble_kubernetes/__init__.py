# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Kubernetes adapters for Datadouble: ``k8s-pv`` (replicate) and ``k8s-secret`` (announce).

A ``k8s-pv`` copy is a directory and a PersistentVolume pointing at it, either
a local volume on one node or an NFS export. The volume is both the copy's
handle and its announcement: it carries the copy's labels, which consumers
match with a PersistentVolumeClaim selector. A bound volume counts as claimed;
when its claim is deleted the volume is released, and the copy is removed and
the pool refilled.

``k8s-secret`` writes a Secret per copy with where it is and, for instance
copies, its credentials. Both drive ``kubectl``.
"""

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from datadouble.announce import Announced, AnnounceError, Announcer
from datadouble.images import ImageSummary
from datadouble.replicate import ClaimState, Copy, CopyKind, Made, ReplicateError, Replicator
from datadouble.settings import parse_settings

_LABEL = "datadouble.dev"
_PHASES = {
    "Available": ClaimState.FREE,
    "Pending": ClaimState.FREE,
    "Bound": ClaimState.CLAIMED,
    "Released": ClaimState.RETURNED,
    "Failed": ClaimState.RETURNED,
}


class KubectlError(Exception):
    """A ``kubectl`` command that failed."""


def kubectl(arguments: list[str], *, stdin: str | None = None) -> str:
    """Run ``kubectl``; raise :class:`KubectlError` with its last error line."""
    binary = os.environ.get("DATADOUBLE_KUBECTL") or shutil.which("kubectl")
    if not binary:
        message = "kubectl was not found; install it or set DATADOUBLE_KUBECTL"
        raise KubectlError(message)
    result = subprocess.run(  # noqa: S603 - kubectl, with arguments built here
        [binary, *arguments], input=stdin, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        lines = result.stderr.strip().splitlines()
        message = f"kubectl {arguments[0]} failed: {lines[-1] if lines else result.returncode}"
        raise KubectlError(message)
    return result.stdout


def _labels(values: Mapping[str, str]) -> dict[str, str]:
    """Kubernetes-safe labels under the datadouble.dev prefix."""
    safe: dict[str, str] = {}
    for key, value in values.items():
        cleaned = "".join(c if c.isalnum() or c in "-_." else "-" for c in value)[:63]
        safe[f"{_LABEL}/{key}"] = cleaned.strip("-_.") or "none"
    return safe


class LocalVolume(BaseModel):
    """A local volume on one node, where ``path`` is that node's filesystem."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    node: str


class NfsVolume(BaseModel):
    """An NFS export; ``path`` here is the export path matching the adapter's ``path``."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    server: str
    path: str


class PvSettings(BaseModel):
    """Settings for the ``k8s-pv`` replicate adapter."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    path: Path
    """Where copies are written, as this process sees it."""
    local: LocalVolume | None = None
    nfs: NfsVolume | None = None
    storage_class: str = "datadouble"
    access_mode: str = "ReadWriteOnce"
    capacity: str = Field(default="1Gi", pattern=r"^\d+[KMGT]i$")

    @model_validator(mode="after")
    def _one_kind(self) -> Self:
        if (self.local is None) == (self.nfs is None):
            message = "give exactly one of `local` or `nfs`"
            raise ValueError(message)
        return self


class PvReplicator:
    """Directory copies exposed as PersistentVolumes."""

    def __init__(self, settings: PvSettings) -> None:
        """Copy under ``settings.path``."""
        self.settings = settings

    @property
    def kind(self) -> CopyKind:
        """Volumes."""
        return CopyKind.VOLUME

    def _source(self, directory: str) -> dict[str, JsonValue]:
        settings = self.settings
        if settings.nfs is not None:
            path = f"{settings.nfs.path.rstrip('/')}/{directory}"
            return {"nfs": {"server": settings.nfs.server, "path": path}}
        node = settings.local.node if settings.local else ""
        return {
            "local": {"path": str(settings.path / directory)},
            "nodeAffinity": {
                "required": {
                    "nodeSelectorTerms": [
                        {
                            "matchExpressions": [
                                {
                                    "key": "kubernetes.io/hostname",
                                    "operator": "In",
                                    "values": [node],
                                }
                            ]
                        }
                    ]
                }
            },
        }

    def create(self, copy_id: str, image: Path, summary: ImageSummary) -> Made:
        """Copy the image, then create the volume."""
        root = self.settings.path
        root.mkdir(parents=True, exist_ok=True)
        partial = root / f".{copy_id}.partial"
        result = subprocess.run(  # noqa: S603 - fixed program, paths from this adapter
            ["cp", "-a", "--reflink=auto", str(image), str(partial)],  # noqa: S607 - coreutils cp
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            shutil.rmtree(partial, ignore_errors=True)
            message = f"copying {summary.identity.generation} failed: {result.stderr.strip()}"
            raise ReplicateError(message)
        partial.rename(root / copy_id)
        identity = summary.identity
        name = f"datadouble-{copy_id}"
        labels = _labels(
            {
                "copy": copy_id,
                "spec": identity.spec,
                "group": identity.group,
                "engine": identity.engine,
                "engine-version": identity.engine_version,
                "generation": identity.generation,
            }
        )
        volume: dict[str, JsonValue] = {
            "apiVersion": "v1",
            "kind": "PersistentVolume",
            "metadata": {"name": name, "labels": dict(labels)},
            "spec": {
                "capacity": {"storage": self.settings.capacity},
                "accessModes": [self.settings.access_mode],
                "persistentVolumeReclaimPolicy": "Retain",
                "storageClassName": self.settings.storage_class,
                "volumeMode": "Filesystem",
                **self._source(copy_id),
            },
        }
        try:
            kubectl(["apply", "--filename=-", "--output=name"], stdin=json.dumps(volume))
        except KubectlError as error:
            shutil.rmtree(root / copy_id, ignore_errors=True)
            raise ReplicateError(str(error)) from None
        return Made(
            location={
                "persistent_volume": name,
                "storage_class": self.settings.storage_class,
                "path": str(root / copy_id),
            }
        )

    def destroy(self, copy: Copy) -> None:
        """Delete the volume and the directory."""
        try:
            kubectl(
                [
                    "delete",
                    "persistentvolume",
                    f"datadouble-{copy.id}",
                    "--ignore-not-found",
                    "--wait=false",
                ]
            )
        except KubectlError as error:
            raise ReplicateError(str(error)) from None
        for path in (self.settings.path / copy.id, self.settings.path / f".{copy.id}.partial"):
            shutil.rmtree(path, ignore_errors=True)

    def claim_state(self, copy: Copy, /) -> ClaimState:
        """The volume's phase: available, bound (claimed), or released (returned)."""
        try:
            phase = kubectl(
                [
                    "get",
                    "persistentvolume",
                    f"datadouble-{copy.id}",
                    "--ignore-not-found",
                    "--output=jsonpath={.status.phase}",
                ]
            ).strip()
        except KubectlError as error:
            raise ReplicateError(str(error)) from None
        return _PHASES.get(phase, ClaimState.RETURNED)


class SecretSettings(BaseModel):
    """Settings for the ``k8s-secret`` announce adapter."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    namespace: str = "default"


class SecretAnnouncer:
    """A Secret per copy, named ``datadouble-<copy id>``."""

    def __init__(self, settings: SecretSettings) -> None:
        """Write Secrets into ``settings.namespace``."""
        self.settings = settings

    def announce(self, copies: tuple[Announced, ...]) -> tuple[str, ...]:
        """Create or update one Secret per copy."""
        written: list[str] = []
        for copy in copies:
            data = {key: str(value) for key, value in copy.location.items()}
            data.update(copy.secrets)
            data["copy_id"] = copy.copy_id
            secret = {
                "apiVersion": "v1",
                "kind": "Secret",
                "type": "Opaque",
                "metadata": {
                    "name": f"datadouble-{copy.copy_id}",
                    "namespace": self.settings.namespace,
                    "labels": _labels({"copy": copy.copy_id, **copy.labels}),
                },
                "stringData": data,
            }
            try:
                kubectl(["apply", "--filename=-", "--output=name"], stdin=json.dumps(secret))
            except KubectlError as error:
                raise AnnounceError(str(error)) from None
            written.append(f"k8s-secret:{self.settings.namespace}/datadouble-{copy.copy_id}")
        return tuple(written)

    def withdraw(self, copy_ids: tuple[str, ...], /) -> tuple[str, ...]:
        """Delete the copies' Secrets."""
        names = [f"datadouble-{copy_id}" for copy_id in copy_ids]
        if not names:
            return ()
        try:
            kubectl(
                [
                    "delete",
                    "secret",
                    *names,
                    f"--namespace={self.settings.namespace}",
                    "--ignore-not-found",
                ]
            )
        except KubectlError as error:
            raise AnnounceError(str(error)) from None
        return tuple(f"k8s-secret:{self.settings.namespace}/{name}" for name in names)


def pv_replicator(settings: Mapping[str, JsonValue]) -> Replicator:
    """The ``k8s-pv`` entry point."""
    return PvReplicator(parse_settings(PvSettings, settings, "k8s-pv replicate"))


def secret_announcer(settings: Mapping[str, JsonValue]) -> Announcer:
    """The ``k8s-secret`` entry point."""
    return SecretAnnouncer(parse_settings(SecretSettings, settings, "k8s-secret announce"))
