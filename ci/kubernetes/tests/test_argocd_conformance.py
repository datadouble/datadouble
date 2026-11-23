# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The CI/CD conformance suite for Argo CD, on a kind cluster.

The suite starts a kind cluster whose node sees the work directory at the
same path, loads the Datadouble and PostgreSQL images into it, and installs
Argo CD (its core install) and a Helm repository serving this checkout's
chart. Each world gets a namespace whose PersistentVolumeClaim is a hostPath
volume over the world's directory, so the host sees what jobs write, and the
node joins the world's Docker network to reach its source. Generated
Applications install the chart from that repository rather than from the
release's tag. A run's environment is the namespace's ``datadouble-boundary``
ConfigMap and its secrets the spec's Secret.

Kubernetes Jobs have no warning state, so a job that finished with a warning
says so in its containers' termination messages, which the suite reads.
"""

import json
import os
import tempfile
import time
import uuid
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml
from pydantic import JsonValue, TypeAdapter

from datadouble import __version__
from datadouble.ci import Side
from datadouble.programs import run_program
from datadouble_argocd import CLAIM, NAME
from datadouble_conformance.ci import (
    IMAGE_VARIABLE,
    POSTGRES,
    WORK_VARIABLE,
    CiConformance,
    CiHarness,
    Event,
    JobRun,
    RunContext,
    State,
    docker,
)

pytestmark = pytest.mark.ci_conformance

NODE = "kindest/node@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5"
ARGOCD = "https://raw.githubusercontent.com/argoproj/argo-cd/v3.5.6/manifests/core-install.yaml"
CHART = Path(__file__).resolve().parents[1] / "chart"
CHARTS_URL = "http://charts.argocd.svc.cluster.local:8080"
_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


def _wait(check: Callable[[], bool], what: str, seconds: int) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            message = f"timed out waiting for {what}"
            raise TimeoutError(message)
        time.sleep(3)


@dataclass
class Cluster:
    """A kind cluster and its kubeconfig."""

    name: str
    kubeconfig: Path

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


def _kind(*arguments: str) -> None:
    done = run_program("kind", arguments)
    if done.status != 0:
        message = f"kind {arguments[0]} failed: {done.stderr.strip()}"
        raise RuntimeError(message)


def _load(name: str, image: str, scratch: Path) -> None:
    """Import an image into the node; `kind load` fails on multi-platform images."""
    archive = scratch / "image.tar"
    if not docker("image", "ls", "--quiet", image).strip():
        docker("pull", "--quiet", image)
    docker("save", "--output", str(archive), image)
    done = run_program(
        "sh",
        [
            "-c",
            (
                f"docker exec --interactive {name}-control-plane ctr --namespace=k8s.io images"
                f" import --snapshotter=overlayfs - < {archive}"
            ),
        ],
    )
    archive.unlink()
    assert done.status == 0, done.stderr


def _charts(cluster: Cluster, scratch: Path) -> None:
    """A Helm repository in the cluster serving this checkout's chart."""
    packaged = scratch / "charts"
    packaged.mkdir()
    done = run_program("helm", ["package", str(CHART), "--destination", str(packaged)])
    assert done.status == 0, done.stderr
    done = run_program("helm", ["repo", "index", str(packaged), "--url", CHARTS_URL])
    assert done.status == 0, done.stderr
    cluster.kubectl(
        "--namespace", "argocd", "create", "configmap", "charts",
        *(f"--from-file={path}" for path in sorted(packaged.iterdir())),
    )  # fmt: skip
    labels: dict[str, JsonValue] = {"app": "charts"}
    container: dict[str, JsonValue] = {
        "name": "charts",
        "image": os.environ[IMAGE_VARIABLE],
        "command": ["python", "-m", "http.server", "8080", "--directory", "/charts"],
        "volumeMounts": [{"name": "charts", "mountPath": "/charts"}],
    }
    cluster.apply(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "charts", "namespace": "argocd"},
            "spec": {
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "containers": [container],
                        "volumes": [{"name": "charts", "configMap": {"name": "charts"}}],
                    },
                },
            },
        }
    )
    cluster.apply(
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "charts", "namespace": "argocd"},
            "spec": {"selector": labels, "ports": [{"port": 8080}]},
        }
    )
    cluster.kubectl(
        "--namespace", "argocd", "rollout", "status", "deployment/charts", "--timeout", "300s"
    )


@pytest.fixture(scope="session")
def cluster(tmp_path_factory: pytest.TempPathFactory) -> Generator[Cluster]:
    """A kind cluster with Argo CD and the chart, removed afterwards."""
    name = f"ddci-{uuid.uuid4().hex[:8]}"
    scratch = tmp_path_factory.mktemp("cluster")
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
        for image in (os.environ[IMAGE_VARIABLE], POSTGRES):
            _load(name, image, scratch)
        built.kubectl("create", "namespace", "argocd")
        built.kubectl("--namespace", "argocd", "apply", "--server-side", "--filename", ARGOCD)
        for workload in (
            "statefulset/argocd-application-controller",
            "deployment/argocd-repo-server",
        ):
            built.kubectl(
                "--namespace", "argocd", "rollout", "status", workload, "--timeout", "600s"
            )
        # The core install leaves out the default project, which the server makes.
        built.apply(
            {
                "apiVersion": "argoproj.io/v1alpha1",
                "kind": "AppProject",
                "metadata": {"name": "default", "namespace": "argocd"},
                "spec": {
                    "sourceRepos": ["*"],
                    "destinations": [{"namespace": "*", "server": "*"}],
                    "clusterResourceWhitelist": [{"group": "*", "kind": "*"}],
                },
            }
        )
        _charts(built, scratch)
        yield built
    finally:
        _kind("delete", "cluster", "--name", name)


@dataclass
class _World:
    namespace: str
    network: str
    applied: dict[str, str] = field(default_factory=dict[str, str])
    """Release → the spec and lock text its Application carries."""


def _result(steps: list["_Step"]) -> tuple[State, dict[str, str]]:
    """A run's state and outputs: its job's own steps, the leading ones of one job.

    A leader's pod also runs its follower (build, then promote), whose result is
    the follower's own.
    """
    own = [step for step in steps if step.command == steps[0].command] if steps else []
    outputs: dict[str, str] = {}
    for step in own:
        for line in step.message.splitlines():
            field_name, _, value = line.partition("=")
            if field_name != "outcome" or outputs.get("outcome") != "warning":
                outputs[field_name] = value
    if not own or any(step.status != 0 for step in own):
        return State.FAILURE, outputs
    if outputs.get("outcome") == "warning":
        return State.WARNING, outputs
    return State.SUCCESS, outputs


def _pod_steps(pod: JsonValue) -> list["_Step"]:
    """A pod's containers, in order: their command, exit status and message."""
    found: list[_Step] = []
    for spec_key, status_key in (
        ("initContainers", "initContainerStatuses"),
        ("containers", "containerStatuses"),
    ):
        specs = _at(pod, "spec", spec_key) or []
        statuses = _at(pod, "status", status_key) or []
        assert isinstance(specs, list)
        assert isinstance(statuses, list)
        for container, status in zip(specs, statuses, strict=False):
            arguments = _at(container, "args")
            assert isinstance(arguments, list)
            code = _at(status, "state", "terminated", "exitCode")
            message = _at(status, "state", "terminated", "message")
            # `--ci argocd --ci-job <job> <command> ...`: a step belongs to its job.
            job = arguments.index("--ci-job") + 1 if "--ci-job" in arguments else 2
            found.append(
                _Step(
                    command=str(arguments[job]),
                    status=code if isinstance(code, int) else -1,
                    message=message if isinstance(message, str) else "",
                )
            )
    return found


@dataclass(frozen=True)
class _Step:
    """One container of a job's pod."""

    command: str
    """The job the step belongs to (``--ci-job``)."""
    status: int
    message: str


class ArgoCdOnKind:
    """Argo CD on kind, through kubectl."""

    def __init__(self, cluster: Cluster) -> None:
        """Drive ``cluster``."""
        self.cluster = cluster
        self.worlds: dict[Path, _World] = {}
        self.repositories: dict[Path, Path] = {}

    @property
    def system(self) -> str:
        return NAME

    @property
    def hosted_runner(self) -> str | None:
        return None

    @property
    def hosted_environment(self) -> Mapping[str, str]:
        # Clusters are never a vendor's, but Datadouble asks every installed adapter.
        return {"RUNNER_ENVIRONMENT": "github-hosted"}

    def runtime(self, network: str, base: Path) -> Mapping[str, JsonValue]:
        namespace = base.name
        docker("network", "connect", network, f"{self.cluster.name}-control-plane")
        self.cluster.kubectl("create", "namespace", namespace)
        self.cluster.apply(
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
        self.cluster.apply(
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
        self.worlds[base] = _World(namespace, network)
        return {"kubernetes": {"namespace": namespace, "claim": CLAIM, "mount": str(base)}}

    def runner(self, side: Side, base: Path) -> str:
        del side
        return base.name

    def _spec(self, repository: Path) -> Path:
        """The repository's spec: on-demand copies', or the pool's in a pool repository."""
        on_demand = repository / "datadouble" / "on-demand.yml"
        return on_demand if on_demand.exists() else repository / "datadouble" / "pool.yml"

    def _world(self, repository: Path) -> _World:
        spec = yaml.safe_load(self._spec(repository).read_text("utf-8"))
        return self.worlds[Path(spec["runtime"]["kubernetes"]["mount"])]

    def _application(self, path: Path) -> dict[str, JsonValue]:
        """A generated Application, installing the chart from the suite's repository."""
        application = _OBJECT.validate_python(yaml.safe_load(path.read_text("utf-8")))
        spec = application["spec"]
        assert isinstance(spec, dict)
        source = spec["source"]
        assert isinstance(source, dict)
        del source["path"]
        source.update({"repoURL": CHARTS_URL, "chart": "datadouble", "targetRevision": __version__})
        # The suite starts every job itself; a schedule firing mid-test would race it.
        jobs = _at(source, "helm", "valuesObject", "jobs")
        assert isinstance(jobs, list)
        for job in jobs:
            if isinstance(job, dict) and "schedule" in job:
                job["schedule"] = ""
        return application

    def prepare(self, repository: Path) -> None:
        self.repositories[repository] = repository
        for path in sorted((repository / "argocd").glob("*.yaml")):
            application = self._application(path)
            self.cluster.apply(application)
            metadata = application["metadata"]
            assert isinstance(metadata, dict)
            self._synced(str(metadata["name"]))

    def _synced(self, name: str) -> None:
        def synced() -> bool:
            found = self.cluster.json("--namespace", "argocd", "get", "application", name)
            status = found.get("status")
            return isinstance(status, dict) and _at(status, "sync", "status") == "Synced"

        _wait(synced, f"Argo CD to sync {name}", 600)

    def run(self, repository: Path, job: str, event: Event, context: RunContext) -> JobRun:
        world = self._world(repository)
        self._environment(world, context)
        release = self._release(repository, job)
        self._refresh(repository, release, context)
        if event is Event.REQUEST_OPENED:
            assert context.request is not None
            name = self._open(repository, release, context.request)
        elif event is Event.REQUEST_CLOSED:
            assert context.request is not None
            name = self._close(release, context.request)
        else:
            name = f"{job}-{context.run_id}"
            cronjob = f"cronjob/datadouble-{release}-{job}"
            self.cluster.kubectl(
                "--namespace", world.namespace, "delete", "job", name, "--ignore-not-found"
            )
            self.cluster.kubectl(
                "--namespace", world.namespace, "create", "job", f"--from={cronjob}", name
            )
        return self._finished(world.namespace, name)

    def _release(self, repository: Path, job: str) -> str:
        production = {"build", "promote", "prune-production", "lock"}
        spec = yaml.safe_load(self._spec(repository).read_text("utf-8"))
        side = Side.PRODUCTION if job in production else Side.NON_PRODUCTION
        return f"{spec['name']}-{side}"

    def _environment(self, world: _World, context: RunContext) -> None:
        """The namespace's boundary ConfigMap and the spec's Secret, exactly as given."""
        for kind, name, data in (
            ("configmap", "datadouble-boundary", context.environ),
            ("secret", "datadouble-shop", context.secrets),
        ):
            self.cluster.kubectl(
                "--namespace", world.namespace, "delete", kind, name, "--ignore-not-found"
            )
            if data:
                literals = [f"--from-literal={key}={value}" for key, value in data.items()]
                kind_arguments = ["generic"] if kind == "secret" else []
                self.cluster.kubectl(
                    "--namespace", world.namespace, "create", kind, *kind_arguments, name, *literals
                )

    def _refresh(self, repository: Path, release: str, context: RunContext) -> None:
        """Carry the current spec, lock and generation into the Application's values."""
        lock = repository / "datadouble" / "catalog.lock"
        spec = self._spec(repository).read_text("utf-8")
        values: dict[str, JsonValue] = {
            "spec": {
                "content": spec,
                "lock": lock.read_text("utf-8") if lock.exists() else "",
            },
            "generation": context.inputs.get("generation", ""),
        }
        name = f"datadouble-{release}"
        patch = {"spec": {"source": {"helm": {"valuesObject": values}}}}
        self.cluster.kubectl(
            "--namespace", "argocd", "patch", "application", name, "--type", "merge",
            "--patch", json.dumps(patch),
        )  # fmt: skip
        self.cluster.kubectl(
            "--namespace", "argocd", "annotate", "application", name, "--overwrite",
            "argocd.argoproj.io/refresh=hard",
        )  # fmt: skip
        self._synced(name)
        world = self._world(repository)
        self._current(world.namespace, release, spec, bool(lock.exists()))
        if context.inputs.get("generation"):
            self._applied(world.namespace, release, context.inputs["generation"])

    def _current(self, namespace: str, release: str, spec: str, locked: bool) -> None:
        """Wait for the side's spec ConfigMap and CronJobs to match the values just set.

        Argo CD can report an Application synced before it has applied a change.
        """
        name = f"datadouble-{release}"

        def current() -> bool:
            found = self.cluster.json("--namespace", namespace, "get", "configmap", name)
            if _at(found, "data", "spec") != spec:
                return False
            cronjobs = self.cluster.json("--namespace", namespace, "get", "cronjobs")
            return _mounts_lock(cronjobs, f"{name}-") == locked

        _wait(current, f"{name} to carry the current spec", 300)

    def _applied(self, namespace: str, release: str, generation: str) -> None:
        """Wait for the CronJobs to carry a generation the Application's values now name."""
        cronjob = f"cronjob/datadouble-{release}-activate"

        def applied() -> bool:
            found = self.cluster.json("--namespace", namespace, "get", cronjob)
            return generation in json.dumps(found)

        _wait(applied, f"{cronjob} to carry {generation}", 300)

    def _open(self, repository: Path, release: str, request: int) -> str:
        """A request's Application, as an ApplicationSet would make it."""
        path = repository / "argocd" / f"datadouble-{release}.yaml"
        application = self._application(path)
        metadata = application["metadata"]
        assert isinstance(metadata, dict)
        name = f"datadouble-{release}-pr-{request}"
        metadata["name"] = name
        metadata["finalizers"] = ["resources-finalizer.argocd.argoproj.io"]
        helm = _at(application, "spec", "source", "helm")
        assert isinstance(helm, dict)
        helm["releaseName"] = f"{release}-pr-{request}"
        values = helm["valuesObject"]
        assert isinstance(values, dict)
        values["owner"] = f"pr-{request}"
        self.cluster.apply(application)
        return f"datadouble-{release}-pr-{request}-request-copies"

    def _close(self, release: str, request: int) -> str:
        self.cluster.kubectl(
            "--namespace", "argocd", "delete", "application", f"datadouble-{release}-pr-{request}",
            "--wait=false",
        )  # fmt: skip
        return f"datadouble-{release}-pr-{request}-request-release"

    def _finished(self, namespace: str, name: str) -> JobRun:
        conditions: list[str] = []

        def finished() -> bool:
            found = self.cluster.kubectl(
                "--namespace", namespace, "get", "job", name, "--output", "json", check=False
            )
            if not found:
                return False
            status = _OBJECT.validate_json(found).get("status")
            if not isinstance(status, dict):
                return False
            # The Job's own conditions, not its pod counts: between a failed pod
            # and its retry (backoffLimit) nothing is active, yet the Job runs on.
            found_conditions = status.get("conditions") or []
            assert isinstance(found_conditions, list)
            for condition in found_conditions:
                if _at(condition, "status") != "True":
                    continue
                if _at(condition, "type") == "Complete":
                    conditions.append("succeeded")
                elif _at(condition, "type") == "Failed":
                    conditions.append("failed")
            return bool(conditions)

        _wait(finished, f"the job {name}", 1800)
        log = self.cluster.kubectl(
            "--namespace", namespace, "logs", f"job/{name}", "--all-containers", check=False
        )
        steps = self._steps(namespace, name)
        log += "\ntermination messages:\n" + "\n".join(step.message for step in steps)
        state, outputs = _result(steps)
        return JobRun(state=state, outputs=outputs, log=log)

    def _steps(self, namespace: str, name: str) -> list[_Step]:
        """The containers of the newest attempt that reported anything."""
        pods = self.cluster.json(
            "--namespace", namespace, "get", "pods", "--selector", f"job-name={name}"
        )
        items = pods.get("items")
        assert isinstance(items, list)
        if not items:
            return []
        ordered = sorted(items, key=lambda item: str(_at(item, "metadata", "creationTimestamp")))
        attempts = [_pod_steps(pod) for pod in ordered]
        reported = [steps for steps in attempts if any(step.message for step in steps)]
        return reported[-1] if reported else attempts[-1]

    def owner(self, repository: Path, request: int) -> str:
        del repository
        return f"pr-{request}"

    def uploaded(self) -> list[Path]:
        return []

    @property
    def artifact_storage(self) -> bool:
        """Kubernetes keeps no job artifacts: the lock job prints its files to the log."""
        return False


def _mounts_lock(cronjobs: JsonValue, prefix: str) -> bool:
    """Whether any of one side's CronJobs (named ``prefix``...) mounts the catalog lock."""
    items = _at(cronjobs, "items")
    assert isinstance(items, list)
    for cronjob in items:
        if not str(_at(cronjob, "metadata", "name")).startswith(prefix):
            continue
        pod = _at(cronjob, "spec", "jobTemplate", "spec", "template", "spec")
        assert isinstance(pod, dict)
        for key in ("initContainers", "containers"):
            containers = pod.get(key) or []
            assert isinstance(containers, list)
            for container in containers:
                mounts = _at(container, "volumeMounts")
                assert isinstance(mounts, list)
                if any(_at(mount, "subPath") == "lock" for mount in mounts):
                    return True
    return False


def _at(value: JsonValue, *path: str) -> JsonValue:
    for step in path:
        if not isinstance(value, dict):
            return None
        value = value.get(step)
    return value


GOLDEN = Path(__file__).parent / "golden"


def test_golden_files_render_and_validate(tmp_path: Path) -> None:
    files = sorted(GOLDEN.glob("*/*.golden"))
    assert files
    for golden in files:
        application = yaml.safe_load(golden.read_text("utf-8"))
        for owner in ("", "pr-7"):
            values = application["spec"]["source"]["helm"]["valuesObject"]
            values["owner"] = owner
            (tmp_path / "values.yaml").write_text(yaml.safe_dump(values), encoding="utf-8")
            rendered = run_program(
                "helm", ["template", "shop", str(CHART), "--values", str(tmp_path / "values.yaml")]
            )
            assert rendered.status == 0, rendered.stderr
            (tmp_path / "rendered.yaml").write_text(rendered.stdout, encoding="utf-8")
            documents = [d for d in yaml.safe_load_all(rendered.stdout) if d]
            jobs = [d for d in documents if d["kind"] in {"Job", "CronJob"}]
            if owner:
                # A request's Application has its hooks and nothing else that runs,
                # and a ConfigMap, so that it has something to sync.
                assert [d["kind"] for d in documents if d["kind"] != "Job"] == ["ConfigMap"]
                assert all(d["kind"] == "Job" for d in jobs), golden.name
                hooks = {d["metadata"]["annotations"]["argocd.argoproj.io/hook"] for d in jobs}
                assert hooks <= {"PreSync", "PostDelete"}, golden.name
            else:
                assert all(d["kind"] == "CronJob" for d in jobs), golden.name
            checked = run_program(
                "kubeconform", ["-strict", "-summary", str(tmp_path / "rendered.yaml")]
            )
            assert checked.status == 0, (golden.name, checked.stdout, checked.stderr)
    linted = run_program("helm", ["lint", str(CHART)])
    assert linted.status == 0, linted.stdout


@pytest.fixture
def ci_harness(cluster: Cluster) -> CiHarness:
    return ArgoCdOnKind(cluster)


class TestArgoCd(CiConformance):
    """Argo CD passes the CI/CD conformance suite."""
