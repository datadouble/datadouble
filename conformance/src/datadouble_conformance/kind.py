# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""A kind cluster for CI/CD conformance suites whose jobs run as Kubernetes pods.

:func:`kind_cluster` starts a one-node kind cluster whose node sees the work
directory (``DATADOUBLE_TEST_CI_WORK``) at the same path, and loads images into
it, since the node pulls only what it cannot find. :meth:`Cluster.claim` gives
a world's namespace the ``datadouble`` PersistentVolumeClaim over the world's
directory, so the host sees what pods write there. Argo CD's suite and the
Jenkins suite's Kubernetes agents share it.
"""

import json
import os
import tempfile
import time
import uuid
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue, TypeAdapter

from datadouble.ci import CLAIM
from datadouble.programs import run_program
from datadouble_conformance.ci import WORK_VARIABLE, docker

NODE = "kindest/node@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5"
_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


def wait(check: Callable[[], bool], what: str, seconds: int) -> None:
    """Poll ``check`` every few seconds until it holds; fail after ``seconds``."""
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            message = f"timed out waiting for {what}"
            raise TimeoutError(message)
        time.sleep(3)


def _kind(*arguments: str) -> None:
    done = run_program("kind", arguments)
    if done.status != 0:
        message = f"kind {arguments[0]} failed: {done.stderr.strip()}"
        raise RuntimeError(message)


@dataclass
class Cluster:
    """A kind cluster and its kubeconfig."""

    name: str
    kubeconfig: Path

    @property
    def node(self) -> str:
        """The node's container."""
        return f"{self.name}-control-plane"

    def kubectl(self, *arguments: str, stdin: str | None = None, check: bool = True) -> str:
        """Run kubectl against the cluster."""
        done = run_program(
            "kubectl", ["--kubeconfig", str(self.kubeconfig), *arguments], stdin=stdin
        )
        if check and done.status != 0:
            message = f"kubectl {' '.join(arguments[:3])} failed: {done.stderr.strip()}"
            raise RuntimeError(message)
        return done.stdout

    def json(self, *arguments: str) -> dict[str, JsonValue]:
        """A resource, as JSON."""
        return _OBJECT.validate_json(self.kubectl(*arguments, "--output", "json"))

    def apply(self, manifest: Mapping[str, JsonValue]) -> None:
        """Apply one manifest."""
        self.kubectl("apply", "--filename", "-", stdin=json.dumps(manifest))

    def gateway(self) -> str:
        """The host's address on the node's network, where pods reach the host."""
        return docker(
            "inspect", "--format", '{{(index .NetworkSettings.Networks "kind").Gateway}}', self.node
        ).strip()  # fmt: skip

    def load(self, image: str, scratch: Path) -> None:
        """Import an image into the node; `kind load` fails on multi-platform images."""
        archive = scratch / f"image-{uuid.uuid4().hex[:8]}.tar"
        if not docker("image", "ls", "--quiet", image).strip():
            docker("pull", "--quiet", image)
        docker("save", "--output", str(archive), image)
        done = run_program(
            "sh",
            [
                "-c",
                (
                    f"docker exec --interactive {self.node} ctr --namespace=k8s.io images"
                    f" import --snapshotter=overlayfs - < {archive}"
                ),
            ],
        )
        archive.unlink()
        assert done.status == 0, done.stderr

    def claim(self, namespace: str, base: Path) -> None:
        """The namespace's ``datadouble`` claim: a hostPath volume over ``base``."""
        self.apply(
            {
                "apiVersion": "v1",
                "kind": "PersistentVolume",
                "metadata": {"name": namespace},
                "spec": {
                    "capacity": {"storage": "10Gi"},
                    "accessModes": ["ReadWriteOnce"],
                    "storageClassName": "",
                    "hostPath": {"path": str(base)},
                    "claimRef": {"namespace": namespace, "name": CLAIM},
                },
            }
        )
        self.apply(
            {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {"name": CLAIM, "namespace": namespace},
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "storageClassName": "",
                    "volumeName": namespace,
                    "resources": {"requests": {"storage": "10Gi"}},
                },
            }
        )


@contextmanager
def kind_cluster(scratch: Path, images: Sequence[str]) -> Generator[Cluster]:
    """A kind cluster holding ``images``, its node seeing the work directory; removed after."""
    name = f"ddci-{uuid.uuid4().hex[:8]}"
    work = Path(os.environ.get(WORK_VARIABLE, tempfile.gettempdir()))
    config = {
        "kind": "Cluster",
        "apiVersion": "kind.x-k8s.io/v1alpha4",
        "nodes": [
            {
                "role": "control-plane",
                "extraMounts": [{"hostPath": str(work), "containerPath": str(work)}],
            }
        ],
    }
    (scratch / "kind.yaml").write_text(json.dumps(config), encoding="utf-8")
    kubeconfig = scratch / "kubeconfig"
    _kind(
        "create", "cluster", "--name", name, "--image", NODE, "--wait", "300s",
        "--config", str(scratch / "kind.yaml"), "--kubeconfig", str(kubeconfig),
    )  # fmt: skip
    try:
        built = Cluster(name, kubeconfig)
        for image in images:
            built.load(image, scratch)
        yield built
    finally:
        _kind("delete", "cluster", "--name", name)
