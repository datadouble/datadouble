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

Every Kubernetes adapter takes an optional ``kubeconfig``, a secret reference
to a kubeconfig's content, and an optional ``context``. The kubeconfig is
written to a private temporary file for each ``kubectl`` call, passed with
``--kubeconfig``, and removed when the call ends, whether or not it
succeeded. Without one, ``kubectl`` uses the in-cluster service account or
the ambient configuration.
"""

import json
import os
import re
import shutil
import tempfile
from collections.abc import Generator, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from datadouble.announce import Announced, AnnounceError, Announcer
from datadouble.files import copy_tree
from datadouble.images import ImageSummary
from datadouble.programs import Completed, locate, run_program, start_program
from datadouble.replicate import ClaimState, Copy, CopyKind, Made, ReplicateError, Replicator
from datadouble.secrets import resolve_secret
from datadouble.settings import parse_settings
from datadouble.spec import SecretRef

if TYPE_CHECKING:
    import subprocess

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


def kubectl_binary() -> str:
    """``DATADOUBLE_KUBECTL``, or ``kubectl`` on ``PATH``."""
    binary = locate("kubectl", "DATADOUBLE_KUBECTL")
    if not binary:
        message = "kubectl was not found; install it or set DATADOUBLE_KUBECTL"
        raise KubectlError(message)
    return binary


REDACTED_LENGTH = 16
"""String values of a kubeconfig at least this long are secret (tokens, keys,
certificates, server URLs): masked in CI logs and removed from ``kubectl``'s output."""
_LINE_VALUE = re.compile(r"""^[\s-]*(?:["']?[\w.-]+["']?\s*:\s*)?(.*?)[\s,]*$""")
"""A line's value, after any list marker and key, in YAML or JSON."""
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def _strings(value: JsonValue) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def secret_values(content: str) -> tuple[str, ...]:
    """A kubeconfig's secret values, longest first.

    Those are its string values at least :data:`REDACTED_LENGTH` characters long,
    from YAML or JSON, and from each line in case it does not parse.
    """
    found: set[str] = set()
    with suppress(yaml.YAMLError, ValidationError):
        found.update(_strings(_JSON.validate_python(yaml.safe_load(content))))
    for line in content.splitlines():
        found.add(_LINE_VALUE.sub(r"\1", line).strip("'\""))
    return tuple(sorted((v for v in found if len(v) >= REDACTED_LENGTH), key=len, reverse=True))


class Kubectl:
    """``kubectl``, with a cluster's kubeconfig and context when they are given."""

    def __init__(
        self,
        kubeconfig: SecretRef | None = None,
        context: str | None = None,
        *,
        avoid: Path | None = None,
    ) -> None:
        """Use ``kubeconfig``, a reference to a kubeconfig's content, and ``context``.

        The kubeconfig's temporary file is never written under ``avoid``, such as
        a work directory that pods mount.
        """
        self.kubeconfig = kubeconfig
        self.context = context
        self.avoid = avoid
        self._content: str | None = None
        self._secrets: tuple[str, ...] = ()

    def resolve(self) -> None:
        """Resolve the kubeconfig now, so a missing one fails before any work.

        Raises :class:`~datadouble.secrets.SecretError` when it cannot be resolved.
        """
        if self.kubeconfig is not None and self._content is None:
            content = resolve_secret(self.kubeconfig, masked=secret_values)
            self._secrets = secret_values(content)
            self._content = content

    def _directory(self) -> Path:
        """A new private directory (0700), outside ``avoid``."""
        for base in (Path(tempfile.gettempdir()), Path.home()):
            inside = self.avoid is not None and base.resolve().is_relative_to(self.avoid.resolve())
            if not inside:
                return Path(tempfile.mkdtemp(prefix="datadouble-kubeconfig-", dir=base))
        message = (
            f"the temporary directory and home are under {self.avoid}, which pods mount;"
            " set TMPDIR elsewhere for the kubeconfig"
        )
        raise KubectlError(message)

    @contextmanager
    def options(self) -> Generator[list[str]]:
        """Global options for ``kubectl`` calls inside the block.

        A kubeconfig is written to a file only its owner can read, in a private
        temporary directory, both removed when the block ends, even after a failure.
        """
        options = [f"--context={self.context}"] if self.context else []
        self.resolve()
        if self._content is None:
            yield options
            return
        directory = self._directory()
        path = directory / "kubeconfig"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(self._content + "\n")
            yield [f"--kubeconfig={path}", *options]
        finally:
            path.unlink(missing_ok=True)
            directory.rmdir()

    def run(self, arguments: Sequence[str], *, stdin: str | None = None) -> Completed:
        """Run ``kubectl`` and return what it did, without the kubeconfig's secret values."""
        binary = kubectl_binary()
        with self.options() as options:
            done = run_program(binary, [*options, *arguments], stdin=stdin)
        return Completed(done.status, self.redact(done.stdout), self.redact(done.stderr))

    @contextmanager
    def started(self, arguments: Sequence[str]) -> "Generator[subprocess.Popen[str]]":
        """``kubectl`` running for the block, such as a port-forward; stopped afterwards."""
        binary = kubectl_binary()
        with self.options() as options:
            process = start_program(binary, [*options, *arguments])
            try:
                yield process
            finally:
                process.terminate()
                process.wait()

    def __call__(self, arguments: Sequence[str], *, stdin: str | None = None) -> str:
        """Run ``kubectl``; raise :class:`KubectlError` with its last error line."""
        done = self.run(arguments, stdin=stdin)
        if done.status != 0:
            message = f"kubectl {arguments[0]} failed: {done.last_error()}"
            raise KubectlError(message)
        return done.stdout

    def redact(self, text: str) -> str:
        """``text`` with each of the kubeconfig's secret values replaced by ``[redacted]``."""
        for value in self._secrets:
            text = text.replace(value, "[redacted]")
        return text


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


class ClusterSettings(BaseModel):
    """How a Kubernetes adapter reaches its cluster."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    kubeconfig: SecretRef | None = None
    """A reference to a kubeconfig's content (``{env: NAME}`` or ``{file: path}``);
    without one, the in-cluster service account or the ambient configuration."""
    context: str | None = Field(default=None, min_length=1)
    """The kubeconfig's context to use; its current context otherwise."""

    def kubectl(self, avoid: Path | None = None) -> Kubectl:
        """``kubectl`` for this cluster, keeping its kubeconfig out of ``avoid``."""
        return Kubectl(self.kubeconfig, self.context, avoid=avoid)


class PvSettings(ClusterSettings):
    """Settings for the ``k8s-pv`` replicate adapter."""

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
        self.kubectl = settings.kubectl()

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
        self.kubectl.resolve()
        root = self.settings.path
        root.mkdir(parents=True, exist_ok=True)
        partial = root / f".{copy_id}.partial"
        problem = copy_tree(image, partial)
        if problem:
            shutil.rmtree(partial, ignore_errors=True)
            message = f"copying {summary.identity.generation} failed: {problem}"
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
                **({"server-version": summary.server_version} if summary.server_version else {}),
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
            self.kubectl(["apply", "--filename=-", "--output=name"], stdin=json.dumps(volume))
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
            self.kubectl(
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
            phase = self.kubectl(
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


class SecretSettings(ClusterSettings):
    """Settings for the ``k8s-secret`` announce adapter."""

    namespace: str = "default"


class SecretAnnouncer:
    """A Secret per copy, named ``datadouble-<copy id>``."""

    def __init__(self, settings: SecretSettings) -> None:
        """Write Secrets into ``settings.namespace``."""
        self.settings = settings
        self.kubectl = settings.kubectl()

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
                self.kubectl(["apply", "--filename=-", "--output=name"], stdin=json.dumps(secret))
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
            self.kubectl(
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
