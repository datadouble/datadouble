# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The Kubernetes container runtime: engine software in pods.

Spec settings::

    runtime:
      kubernetes:
        namespace: datadouble         # optional: see below
        claim: datadouble-work        # the PersistentVolumeClaim holding the work directory
        mount: /work                  # where that claim is mounted in Datadouble's own pod
        node: worker-3                # optional: run pods on this node (ReadWriteOnce claims)
        access: pod-ip                # or port-forward, when Datadouble runs outside the cluster
        image_pull_secrets: [registry]
        kubeconfig:                          # optional: another cluster's kubeconfig
          env: SHOP_KUBECONFIG
        context: build                       # optional: the kubeconfig's context to use

Datadouble runs with the work directory on ``claim``, mounted at ``mount``;
every pod mounts the same claim at the same path, so paths mean the same on
both sides, and every mounted path must be under ``mount``. Pods run as
Datadouble's own user and group. Secrets reach a pod through a Secret object
made for it and removed with it. A command's output is written to files on the
claim and read back, which keeps standard output and standard error apart.
Everything is done with ``kubectl``: with the kubeconfig and context the
settings give, written to a private temporary directory outside ``mount`` for
each call, or else in-cluster or ambient configuration. A kubeconfig that
cannot be resolved fails ``run``, ``serve`` and ``release`` with
:class:`~datadouble.secrets.SecretError` before any pod starts.

Without ``namespace``, pods start where ``kubectl`` itself would put them:
inside a cluster (no kubeconfig in the settings or ``KUBECONFIG``, and a
service account's namespace file), the namespace of Datadouble's own pod, so
one spec serves pods in each side's namespace; otherwise the kubeconfig
context's namespace, or ``default``.
"""

import json
import os
import re
import secrets
import time
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path

from pydantic import Field, JsonValue, TypeAdapter

from datadouble.runtime import (
    Completed,
    Container,
    ContainerError,
    ContainerRuntime,
    Endpoint,
    absolute,
)
from datadouble.settings import parse_settings
from datadouble_kubernetes import ClusterSettings, Kubectl, KubectlError

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


class KubernetesRuntimeSettings(ClusterSettings):
    """Settings for the ``kubernetes`` container runtime."""

    namespace: str | None = None
    """Where pods start; Datadouble's own namespace (or kubectl's) when left out."""
    claim: str
    mount: Path
    node: str | None = None
    access: Access = Access.POD_IP
    image_pull_secrets: tuple[str, ...] = ()
    ready_seconds: int = Field(default=300, ge=1)
    stop_seconds: int = Field(default=300, ge=1)


SERVICE_ACCOUNT_NAMESPACE = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
"""The namespace of the pod a process runs in, as Kubernetes mounts it for its service account."""


def current_namespace(kubectl: Kubectl) -> str:
    """The namespace ``kubectl`` uses when told none.

    Inside a cluster, with no kubeconfig (neither the settings' nor
    ``KUBECONFIG``), that is the namespace of the pod Datadouble runs in;
    otherwise the namespace of the kubeconfig's context (the settings'
    ``context``, or its current one), or ``default`` when it names none.
    """
    in_cluster = kubectl.kubeconfig is None and "KUBECONFIG" not in os.environ
    if in_cluster and SERVICE_ACCOUNT_NAMESPACE.is_file():
        return SERVICE_ACCOUNT_NAMESPACE.read_text(encoding="utf-8").strip()
    try:
        done = kubectl.run(["config", "view", "--minify", "--output=jsonpath={..namespace}"])
    except KubectlError as error:
        raise ContainerError(str(error)) from None
    return (done.stdout.strip() if done.status == 0 else "") or "default"


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
                f"--namespace={self.runtime.namespace}",
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
        self.kubectl = settings.kubectl(avoid=settings.mount)
        self._namespace = settings.namespace

    @property
    def namespace(self) -> str:
        """The namespace pods start in: the settings', or :func:`current_namespace`."""
        if self._namespace is None:
            self._namespace = current_namespace(self.kubectl)
        return self._namespace

    def invoke(self, arguments: Sequence[str]) -> Completed:
        """Run ``kubectl`` and return what it did, whether or not it succeeded."""
        try:
            return self.kubectl.run(arguments)
        except KubectlError as error:
            raise ContainerError(str(error)) from None

    def _kubectl(self, arguments: list[str], action: str, *, stdin: str | None = None) -> str:
        try:
            return self.kubectl(arguments, stdin=stdin)
        except KubectlError as error:
            message = f"{action}: {error}"
            raise ContainerError(message) from None

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
                "namespace": self.namespace,
                "labels": {_RUNTIME_LABEL: "true"},
                "annotations": {_OWNER: str(absolute([container.owner])[0])},
            },
            "spec": spec,
        }

    def create(self, name: str, container: Container, command: Sequence[str]) -> None:
        """Create the pod, and its Secret when it has secrets."""
        namespace = f"--namespace={self.namespace}"
        if container.secrets:
            secret = {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": name,
                    "namespace": self.namespace,
                    "labels": {_RUNTIME_LABEL: "true"},
                },
                "stringData": dict(container.secrets),
            }
            self._kubectl(
                ["create", namespace, "-f", "-"], "creating the secret", stdin=json.dumps(secret)
            )
        pod = json.dumps(self.manifest(name, container, command))
        self._kubectl(["create", namespace, "-f", "-"], f"starting {container.image}", stdin=pod)

    def _status(self, name: str) -> dict[str, JsonValue]:
        output = self._kubectl(
            ["get", f"--namespace={self.namespace}", "pod", name, "--output=json"],
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
        namespace = f"--namespace={self.namespace}"
        for kind in ("pod", "secret"):
            self.invoke(["delete", namespace, kind, name, "--ignore-not-found", "--wait=false"])

    def _logs(self, name: str) -> str:
        result = self.invoke(["logs", f"--namespace={self.namespace}", name, "--tail=5"])
        lines = (result.stdout + result.stderr).strip().splitlines()
        return lines[-1] if lines else "no output"

    # The runtime

    def run(self, container: Container) -> Completed:
        """Run a pod to completion; its output comes back through files on the claim."""
        self.kubectl.resolve()
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
        self.kubectl.resolve()
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
        arguments = ["port-forward", f"--namespace={self.namespace}", f"pod/{name}"]
        # serve() has run kubectl already, so it is installed and the kubeconfig resolved.
        with self.kubectl.started([*arguments, f":{port}"]) as forward:
            line = forward.stdout.readline() if forward.stdout else ""
            match = _FORWARDING.search(line)
            if match is None:
                message = f"port-forward to {name} failed"
                raise ContainerError(message)
            yield Endpoint(host="127.0.0.1", port=int(match.group(1)))

    def _stop(self, name: str) -> None:
        """Signal the server to stop and wait; it must exit cleanly."""
        self._kubectl(
            [
                "exec",
                f"--namespace={self.namespace}",
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
        self.kubectl.resolve()
        root = absolute([owner])[0]
        listed = self._kubectl(
            [
                "get",
                f"--namespace={self.namespace}",
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
