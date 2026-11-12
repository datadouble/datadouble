# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The Kubernetes container runtime: engine software in pods.

Spec settings::

    runtime:
      kubernetes:
        namespace: datadouble
        claim: datadouble-work        # the PersistentVolumeClaim holding the work directory
        mount: /work                  # where that claim is mounted in Datadouble's own pod
        node: worker-3                # optional: run pods on this node (ReadWriteOnce claims)
        access: pod-ip                # or port-forward, when Datadouble runs outside the cluster
        image_pull_secrets: [registry]

Datadouble runs with the work directory on ``claim``, mounted at ``mount``;
every pod mounts the same claim at the same path, so paths mean the same on
both sides, and every mounted path must be under ``mount``. Pods run as
Datadouble's own user and group. Secrets reach a pod through a Secret object
made for it and removed with it. A command's output is written to files on the
claim and read back, which keeps standard output and standard error apart.
Everything is done with ``kubectl``, so its configuration and contexts apply.
"""

import json
import os
import re
import secrets
import shutil
import subprocess
import time
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from datadouble.runtime import (
    Completed,
    Container,
    ContainerError,
    ContainerRuntime,
    Endpoint,
    absolute,
)
from datadouble.settings import parse_settings
from datadouble_kubernetes import KubectlError, kubectl

_OWNER = "datadouble.dev/owner"
_RUNTIME_LABEL = "datadouble.dev/runtime"
_STUCK = ("ErrImagePull", "ImagePullBackOff", "InvalidImageName", "CreateContainerConfigError")
_FORWARDING = re.compile(r"Forwarding from 127\.0\.0\.1:(\d+)")
_MAIN = "main"
_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


def _mapping(value: JsonValue) -> dict[str, JsonValue]:
    """``value`` if it is a JSON object, else an empty one."""
    return _OBJECT.validate_python(value) if isinstance(value, dict) else {}


class Access(StrEnum):
    """How Datadouble reaches a server pod's port."""

    POD_IP = "pod-ip"
    """Directly, at the pod's address: Datadouble runs in the cluster."""
    PORT_FORWARD = "port-forward"
    """Through ``kubectl port-forward``: Datadouble runs outside it."""


class KubernetesRuntimeSettings(BaseModel):
    """Settings for the ``kubernetes`` container runtime."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    namespace: str = "default"
    claim: str
    mount: Path
    node: str | None = None
    access: Access = Access.POD_IP
    image_pull_secrets: tuple[str, ...] = ()
    ready_seconds: int = Field(default=300, ge=1)
    stop_seconds: int = Field(default=300, ge=1)


def _kubectl(arguments: list[str], action: str, *, stdin: str | None = None) -> str:
    try:
        return kubectl(arguments, stdin=stdin)
    except KubectlError as error:
        message = f"{action}: {error}"
        raise ContainerError(message) from None


class KubernetesServer:
    """A server pod started by :class:`KubernetesRuntime`."""

    def __init__(self, runtime: "KubernetesRuntime", name: str, endpoint: Endpoint) -> None:
        """Wrap the running pod ``name``."""
        self.runtime = runtime
        self.name = name
        self._endpoint = endpoint

    @property
    def endpoint(self) -> Endpoint:
        """The pod's address, or the local end of a port-forward."""
        return self._endpoint

    def execute(self, command: Sequence[str], env: Mapping[str, str] | None = None) -> Completed:
        """``kubectl exec``; ``env`` is passed in the exec request, so it must not be secret."""
        assignments = [f"{key}={value}" for key, value in (env or {}).items()]
        prefix = ["env", *assignments] if assignments else []
        return self.runtime.invoke(
            [
                "exec",
                f"--namespace={self.runtime.settings.namespace}",
                self.name,
                f"--container={_MAIN}",
                "--",
                *prefix,
                *command,
            ]
        )


class KubernetesRuntime:
    """Runs containers as pods with ``kubectl``."""

    def __init__(self, settings: KubernetesRuntimeSettings) -> None:
        """Use ``settings`` for every pod."""
        self.settings = settings

    @staticmethod
    def binary() -> str:
        """The ``kubectl`` command."""
        found = os.environ.get("DATADOUBLE_KUBECTL") or shutil.which("kubectl")
        if not found:
            message = "kubectl was not found; install it or set DATADOUBLE_KUBECTL"
            raise ContainerError(message)
        return found

    def invoke(self, arguments: Sequence[str]) -> Completed:
        """Run ``kubectl`` and return what it did, whether or not it succeeded."""
        result = subprocess.run(  # noqa: S603 - kubectl, with arguments built here
            [self.binary(), *arguments], capture_output=True, text=True, check=False
        )
        return Completed(status=result.returncode, stdout=result.stdout, stderr=result.stderr)

    # Pods

    def manifest(
        self, name: str, container: Container, command: Sequence[str]
    ) -> dict[str, JsonValue]:
        """The pod running ``command`` for ``container``."""
        mount = self.settings.mount
        for path in absolute(container.mounts):
            if not path.is_relative_to(mount):
                message = f"{path} is not on the work claim, which is mounted at {mount}"
                raise ContainerError(message)
        env: list[JsonValue] = [{"name": k, "value": v} for k, v in container.env.items()]
        for key in container.secrets:
            reference: dict[str, JsonValue] = {"name": name, "key": key}
            env.append({"name": key, "valueFrom": {"secretKeyRef": reference}})
        main: dict[str, JsonValue] = {
            "name": _MAIN,
            "image": container.image,
            "command": list(command),
            "env": env,
            "volumeMounts": [{"name": "work", "mountPath": str(mount)}],
        }
        if container.port is not None:
            main["ports"] = [{"containerPort": container.port}]
            main["readinessProbe"] = {
                "exec": {"command": list(container.ready)},
                "periodSeconds": 1,
                "failureThreshold": 1,
            }
        spec: dict[str, JsonValue] = {
            "restartPolicy": "Never",
            "terminationGracePeriodSeconds": self.settings.stop_seconds,
            "securityContext": {"runAsUser": os.getuid(), "runAsGroup": os.getgid()},
            "volumes": [
                {"name": "work", "persistentVolumeClaim": {"claimName": self.settings.claim}}
            ],
            "containers": [main],
            "imagePullSecrets": [{"name": s} for s in self.settings.image_pull_secrets],
        }
        if self.settings.node:
            spec["nodeSelector"] = {"kubernetes.io/hostname": self.settings.node}
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": name,
                "namespace": self.settings.namespace,
                "labels": {_RUNTIME_LABEL: "true"},
                "annotations": {_OWNER: str(absolute([container.owner])[0])},
            },
            "spec": spec,
        }

    def create(self, name: str, container: Container, command: Sequence[str]) -> None:
        """Create the pod, and its Secret when it has secrets."""
        namespace = f"--namespace={self.settings.namespace}"
        if container.secrets:
            secret = {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": name,
                    "namespace": self.settings.namespace,
                    "labels": {_RUNTIME_LABEL: "true"},
                },
                "stringData": dict(container.secrets),
            }
            _kubectl(
                ["create", namespace, "-f", "-"], "creating the secret", stdin=json.dumps(secret)
            )
        pod = json.dumps(self.manifest(name, container, command))
        _kubectl(["create", namespace, "-f", "-"], f"starting {container.image}", stdin=pod)

    def _status(self, name: str) -> dict[str, JsonValue]:
        output = _kubectl(
            ["get", f"--namespace={self.settings.namespace}", "pod", name, "--output=json"],
            "reading the pod",
        )
        return _mapping(_OBJECT.validate_json(output).get("status"))

    @staticmethod
    def container_state(status: Mapping[str, JsonValue]) -> tuple[str | None, int | None, bool]:
        """The main container's waiting reason, exit code once terminated, and readiness."""
        statuses = status.get("containerStatuses")
        if not isinstance(statuses, list) or not statuses:
            return None, None, False
        main = _mapping(statuses[0])
        state = _mapping(main.get("state"))
        reason = _mapping(state.get("waiting")).get("reason")
        code = _mapping(state.get("terminated")).get("exitCode")
        return (
            reason if isinstance(reason, str) else None,
            code if isinstance(code, int) else None,
            main.get("ready") is True,
        )

    def delete(self, name: str) -> None:
        """Delete a pod and its Secret, if they exist."""
        namespace = f"--namespace={self.settings.namespace}"
        for kind in ("pod", "secret"):
            self.invoke(["delete", namespace, kind, name, "--ignore-not-found", "--wait=false"])

    def _logs(self, name: str) -> str:
        result = self.invoke(["logs", f"--namespace={self.settings.namespace}", name, "--tail=5"])
        lines = (result.stdout + result.stderr).strip().splitlines()
        return lines[-1] if lines else "no output"

    # The runtime

    def run(self, container: Container) -> Completed:
        """Run a pod to completion; its output comes back through files on the claim."""
        name = f"datadouble-{secrets.token_hex(6)}"
        outputs = absolute([container.owner])[0] / f".{name}"
        outputs.mkdir(parents=True, exist_ok=True)
        command = [
            "sh",
            "-c",
            f'"$@" > {outputs}/stdout 2> {outputs}/stderr',
            "sh",
            *container.command,
        ]
        try:
            self.create(name, container, command)
            while True:
                reason, code, _ = self.container_state(self._status(name))
                if code is not None:
                    return Completed(
                        status=code,
                        stdout=(outputs / "stdout").read_text("utf-8", errors="replace"),
                        stderr=(outputs / "stderr").read_text("utf-8", errors="replace"),
                    )
                if reason in _STUCK:
                    message = f"{container.image} could not start: {reason}"
                    raise ContainerError(message)
                time.sleep(1)
        finally:
            self.delete(name)
            for item in outputs.glob("*"):
                item.unlink()
            outputs.rmdir()

    @contextmanager
    def serve(self, container: Container) -> Generator[KubernetesServer]:
        """Start a server pod, wait until it is ready, and stop it cleanly afterwards."""
        if container.port is None:
            message = "a server container needs a port"
            raise ContainerError(message)
        name = f"datadouble-{secrets.token_hex(6)}"
        try:
            self.create(name, container, container.command)
            status = self._wait(name, container)
            with self._endpoint(name, container.port, status) as endpoint:
                yield KubernetesServer(self, name, endpoint)
            self._stop(name)
        finally:
            self.delete(name)

    def _wait(self, name: str, container: Container) -> dict[str, JsonValue]:
        deadline = time.monotonic() + self.settings.ready_seconds
        while True:
            status = self._status(name)
            reason, code, ready = self.container_state(status)
            if ready:
                return status
            if code is not None:
                message = f"the server stopped while starting: {self._logs(name)}"
                raise ContainerError(message)
            if reason in _STUCK:
                message = f"{container.image} could not start: {reason}"
                raise ContainerError(message)
            if time.monotonic() > deadline:
                message = (
                    f"{container.image} was not ready after {self.settings.ready_seconds} seconds"
                )
                raise ContainerError(message)
            time.sleep(1)

    @contextmanager
    def _endpoint(
        self, name: str, port: int, status: Mapping[str, JsonValue]
    ) -> Generator[Endpoint]:
        if self.settings.access is Access.POD_IP:
            yield Endpoint(host=str(status.get("podIP")), port=port)
            return
        forward = subprocess.Popen(  # noqa: S603 - kubectl, with arguments built here
            [
                self.binary(),
                "port-forward",
                f"--namespace={self.settings.namespace}",
                f"pod/{name}",
                f":{port}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            line = forward.stdout.readline() if forward.stdout else ""
            match = _FORWARDING.search(line)
            if match is None:
                message = f"port-forward to {name} failed"
                raise ContainerError(message)
            yield Endpoint(host="127.0.0.1", port=int(match.group(1)))
        finally:
            forward.terminate()
            forward.wait()

    def _stop(self, name: str) -> None:
        """Signal the server to stop and wait; it must exit cleanly."""
        _kubectl(
            [
                "exec",
                f"--namespace={self.settings.namespace}",
                name,
                f"--container={_MAIN}",
                "--",
                "sh",
                "-c",
                "kill -TERM 1",
            ],
            "stopping the server",
        )
        deadline = time.monotonic() + self.settings.stop_seconds
        while True:
            _, code, _ = self.container_state(self._status(name))
            if code is not None:
                break
            if time.monotonic() > deadline:
                message = f"the server did not stop within {self.settings.stop_seconds} seconds"
                raise ContainerError(message)
            time.sleep(1)
        if code != 0:
            message = f"the server exited with status {code}: {self._logs(name)}"
            raise ContainerError(message)

    def release(self, owner: Path) -> None:
        """Remove pods (and their secrets) labelled with ``owner`` or a directory under it."""
        root = absolute([owner])[0]
        listed = _kubectl(
            [
                "get",
                f"--namespace={self.settings.namespace}",
                "pods",
                f"--selector={_RUNTIME_LABEL}=true",
                "--output=json",
            ],
            "listing pods",
        )
        items = _OBJECT.validate_json(listed).get("items")
        for item in items if isinstance(items, list) else []:
            metadata = _mapping(_mapping(item).get("metadata"))
            labelled = _mapping(metadata.get("annotations")).get(_OWNER)
            if isinstance(labelled, str) and Path(labelled).is_relative_to(root):
                self.delete(str(metadata.get("name")))


def kubernetes_runtime(settings: Mapping[str, JsonValue]) -> ContainerRuntime:
    """The entry point named in ``datadouble.runtimes``."""
    return KubernetesRuntime(
        parse_settings(KubernetesRuntimeSettings, settings, "kubernetes runtime")
    )
