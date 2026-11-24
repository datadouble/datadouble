# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The CI/CD conformance suite for Jenkins with Kubernetes agents, on a kind cluster.

The suite starts a kind cluster (``datadouble_conformance.kind``) holding the
Datadouble, PostgreSQL and inbound agent images, and the test Jenkins on the
host's network, listening on the host's address on the cluster's network, so
that agent pods reach it over WebSocket. Its Kubernetes plugin's cloud talks
to the cluster with a service account token, and keeps finished pods, from
which the suite reads each build's outputs before removing them. Job
repositories are served to the pods by ``git daemon`` in Jenkins' container.

Jobs are generated with ``--agent kubernetes`` and run as written: each build
must have run in a pod whose spec holds everything the Jenkinsfile's pod
declares. Each side of a world has its own namespace (``--runner``), with
a ``datadouble`` claim over the world's directory, the service account and
Roles ``instructions`` describe (``datadouble_jenkins.ACCOUNT_RULES``,
``CLOUD_RULES``), and the ``datadouble-boundary`` ConfigMap when the run is
marked inside. One spec serves both sides: its ``kubernetes`` runtime names
no namespace, so engine pods start in the namespace of the job's pod, and
the suite checks that every engine pod a run starts is in that run's
namespace, from the pods' events. Jenkins' global environment always sets
``DATADOUBLE_BOUNDARY=inside``, so every refusal of an unmarked run also shows
that no Jenkins variable marks a pod.
"""

import base64
import json
import os
import re
import socket
import textwrap
from collections.abc import Generator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
import yaml
from pydantic import JsonValue, TypeAdapter

from datadouble.ci import CLAIM, Side
from datadouble_conformance.ci import (
    IMAGE_VARIABLE,
    POSTGRES,
    CiConformance,
    CiHarness,
    Event,
    JobRun,
    RunContext,
    State,
    World,
    docker,
)
from datadouble_conformance.kind import Cluster, kind_cluster, wait
from datadouble_jenkins import ACCOUNT_RULES, AGENT_COMMAND, CLOUD_RULES, CONTAINER, Rule
from test_jenkins_conformance import JenkinsContainer, Server, parse_outputs, start_jenkins

AGENT = "jenkins/inbound-agent:3391.va_37fa_a_305d6d-1-jdk25"
"""The inbound agent image, the plugin's default, loaded into the cluster and named
to the plugin, so that pods never pull it."""
CLOUD = "kind"
WORKSPACES = "/home/jenkins/agent/workspace"
"""Where the plugin's pods keep workspaces, in every container."""
SPEC_NAME = "shop"
_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])
_NAMESPACE = re.compile(r"^\s*namespace '([^']+)'$", re.MULTILINE)
_ESCAPED = re.compile(r"\\(.)")
_ENGINE = re.compile(r"datadouble-[0-9a-f]{12}")
"""The name the ``kubernetes`` runtime gives an engine pod."""


def generated_pod(jenkinsfile: str) -> dict[str, JsonValue]:
    """The pod a Kubernetes agent's Jenkinsfile declares, in its ``yaml '''...'''``."""
    lines = jenkinsfile.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "yaml '''")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "'''")
    text = textwrap.dedent("\n".join(lines[start + 1 : end]))
    return _OBJECT.validate_python(yaml.safe_load(_ESCAPED.sub(r"\1", text)))


def contains(expected: JsonValue, actual: JsonValue) -> bool:
    """Whether ``actual`` holds everything ``expected`` says, as Kubernetes keeps it.

    Kubernetes and the plugin add to a pod (defaults, the workspace volume, the
    service account's token), so mappings and lists of mappings may hold more;
    every value given must be there unchanged, and lists of plain values equal.
    """
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and contains(value, actual[key]) for key, value in expected.items()
        )
    if isinstance(expected, list) and all(isinstance(item, dict) for item in expected):
        return isinstance(actual, list) and all(
            any(contains(item, candidate) for candidate in actual) for item in expected
        )
    return expected == actual


def _role(name: str, namespace: str, rules: tuple[Rule, ...]) -> dict[str, JsonValue]:
    return _OBJECT.validate_python(
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {"name": name, "namespace": namespace},
            "rules": [
                {"apiGroups": [""], "resources": list(r.resources), "verbs": list(r.verbs)}
                for r in rules
            ],
        }
    )


def _binding(name: str, namespace: str, account: str, home: str) -> dict[str, JsonValue]:
    """Bind the Role ``name`` in ``namespace`` to the service account ``account`` of ``home``."""
    return _OBJECT.validate_python(
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": name, "namespace": namespace},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name},
            "subjects": [{"kind": "ServiceAccount", "name": account, "namespace": home}],
        }
    )


def _encoded(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _free_port(address: str) -> int:
    with socket.socket() as probe:
        probe.bind((address, 0))
        return int(probe.getsockname()[1])


def _listening(address: str, port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(2)
        return probe.connect_ex((address, port)) == 0


@dataclass
class Stage:
    """The cluster, Jenkins with its cloud, and the git daemon pods check out from."""

    cluster: Cluster
    server: Server
    git: str
    """The git daemon's ``git://`` base URL."""


def _cloud(cluster: Cluster, server: Server) -> None:
    """The plugin's cloud, authenticated by a token of the service account ``jenkins``."""
    cluster.kubectl("create", "namespace", "jenkins")
    cluster.kubectl("--namespace", "jenkins", "create", "serviceaccount", "jenkins")
    token = cluster.kubectl(
        "--namespace", "jenkins", "create", "token", "jenkins", "--duration", "6h"
    ).strip()
    config = yaml.safe_load(cluster.kubeconfig.read_text("utf-8"))
    endpoint = config["clusters"][0]["cluster"]
    groovy = (
        "import jenkins.model.Jenkins\n"
        "import org.csanchez.jenkins.plugins.kubernetes.KubernetesCloud\n"
        "import org.csanchez.jenkins.plugins.kubernetes.pod.retention.Always\n"
        "import com.cloudbees.plugins.credentials.*\n"
        "import com.cloudbees.plugins.credentials.domains.Domain\n"
        "import org.jenkinsci.plugins.plaincredentials.impl.StringCredentialsImpl\n"
        "import hudson.util.Secret\n"
        "def decode(String text) { new String(Base64.decoder.decode(text), 'UTF-8') }\n"
        "def j = Jenkins.get()\n"
        "SystemCredentialsProvider.getInstance().getStore().addCredentials(Domain.global(),\n"
        "  new StringCredentialsImpl(CredentialsScope.GLOBAL, 'kind-cloud', 'kind-cloud',\n"
        f"    Secret.fromString(decode('{_encoded(token)}'))))\n"
        f"def cloud = new KubernetesCloud('{CLOUD}')\n"
        f"cloud.serverUrl = decode('{_encoded(endpoint['server'])}')\n"
        f"cloud.serverCertificate = decode('{endpoint['certificate-authority-data']}')\n"
        "cloud.credentialsId = 'kind-cloud'\n"
        f"cloud.jenkinsUrl = '{server.url}/'\n"
        "cloud.webSocket = true\n"
        "cloud.podRetention = new Always()\n"
        "cloud.containerCapStr = '20'\n"
        "j.clouds.add(cloud)\n"
        "j.save()\n"
        "println 'done'\n"
    )
    assert "done" in server.script(groovy)


def _git_daemon(server: Server, address: str) -> str:
    """Serve the suite's repositories to the pods; their base URL."""
    repositories = server.work / "repositories"
    repositories.mkdir(exist_ok=True)
    port = _free_port(address)
    docker(
        "exec", "--detach", server.container, "git", "daemon", "--reuseaddr", "--export-all",
        f"--listen={address}", f"--port={port}", f"--base-path={repositories}", str(repositories),
    )  # fmt: skip
    wait(lambda: _listening(address, port), "git daemon to listen", 60)
    return f"git://{address}:{port}"


@pytest.fixture(scope="session")
def stage(tmp_path_factory: pytest.TempPathFactory) -> Generator[Stage]:
    """The cluster and Jenkins for the suite, removed afterwards."""
    scratch = tmp_path_factory.mktemp("cluster")
    images = [os.environ[IMAGE_VARIABLE], POSTGRES, AGENT]
    with kind_cluster(scratch, images) as cluster:
        address = cluster.gateway()
        agent = (
            "-Dorg.csanchez.jenkins.plugins.kubernetes.pipeline.PodTemplateStepExecution"
            f".defaultImage={AGENT}"
        )
        with start_jenkins(
            tmp_path_factory, address=address, docker_socket=False, java_options=(agent,)
        ) as server:
            _cloud(cluster, server)
            yield Stage(cluster, server, _git_daemon(server, address))


@dataclass(frozen=True)
class _Generated:
    """A job's namespace and the pod its Jenkinsfile declares."""

    namespace: str
    pod: dict[str, JsonValue]


class JenkinsOnKind(JenkinsContainer):
    """Jenkins whose jobs run as Kubernetes agent pods on kind."""

    def __init__(self, stage: Stage) -> None:
        """Drive ``stage``."""
        super().__init__(stage.server)
        self.stage = stage
        self.cluster = stage.cluster
        self.generated: dict[str, _Generated] = {}
        """Jenkins job → what its Jenkinsfile declares."""
        self.worlds: dict[str, tuple[str, ...]] = {}
        """A side's namespace → both of its world's."""
        self.engines: dict[str, set[str]] = {}
        """A namespace → the engine pods that have started in it."""

    @property
    def init_fields(self) -> Mapping[str, str]:
        return {"agent": "kubernetes", "cloud": CLOUD}

    def remote(self, repository: Path) -> str:
        return f"{self.stage.git}/{repository.name}"

    def runner(self, side: Side, base: Path) -> str:
        return f"{base.name}-{side}"

    def runtime(self, network: str, base: Path) -> Mapping[str, JsonValue]:
        """A namespace per side, each with its claim and the accounts and Roles instructions list.

        The runtime names no namespace, so each side's engine pods start in its own.
        """
        docker("network", "connect", network, self.cluster.node)
        namespaces = tuple(self.runner(side, base) for side in Side)
        for side, namespace in zip(Side, namespaces, strict=True):
            self.cluster.kubectl("create", "namespace", namespace)
            self.cluster.claim(namespace, base)
            self.cluster.apply(_role("jenkins-agents", namespace, CLOUD_RULES))
            self.cluster.apply(_binding("jenkins-agents", namespace, "jenkins", "jenkins"))
            account = f"datadouble-{SPEC_NAME}-{side}"
            self.cluster.kubectl("--namespace", namespace, "create", "serviceaccount", account)
            self.cluster.apply(_role(account, namespace, ACCOUNT_RULES))
            self.cluster.apply(_binding(account, namespace, account, namespace))
            self.worlds[namespace] = namespaces
            self.engines[namespace] = set()
        return {"kubernetes": {"claim": CLAIM, "mount": str(base)}}

    def engine_pods(self, namespace: str) -> set[str]:
        """Engine pods that have started in ``namespace``, as its events record them."""
        events = self.cluster.json(
            "--namespace", namespace, "get", "events", "--field-selector", "involvedObject.kind=Pod"
        )  # fmt: skip
        items = events.get("items")
        assert isinstance(items, list)
        names: set[str] = set()
        for item in items:
            involved = item.get("involvedObject") if isinstance(item, dict) else None
            name = involved.get("name") if isinstance(involved, dict) else None
            if isinstance(name, str) and _ENGINE.fullmatch(name):
                names.add(name)
        return names

    def prepare(self, repository: Path) -> None:
        jobs = repository / "jenkins"
        super().prepare(repository)
        suffix = f"-{self.remotes[repository].name}"
        for (owner, _), known in self.jobs.items():
            if owner != repository:
                continue
            text = (jobs / f"{known.name.removesuffix(suffix)}.Jenkinsfile").read_text("utf-8")
            namespace = _NAMESPACE.search(text)
            assert namespace is not None, known.name
            self.generated[known.name] = _Generated(namespace.group(1), generated_pod(text))

    def run(self, repository: Path, job: str, event: Event, context: RunContext) -> JobRun:
        """Mark the job's namespace as the run says, and Jenkins always, then run the job.

        Every engine pod the run started must be in the job's own namespace.
        """
        namespace = self.generated[self.jobs[(repository, job)].name].namespace
        self.cluster.kubectl(
            "--namespace", namespace, "delete", "configmap", "datadouble-boundary",
            "--ignore-not-found",
        )  # fmt: skip
        mark = context.environ.get("DATADOUBLE_BOUNDARY")
        if mark is not None:
            self.cluster.kubectl(
                "--namespace", namespace, "create", "configmap", "datadouble-boundary",
                f"--from-literal=DATADOUBLE_BOUNDARY={mark}",
            )  # fmt: skip
        environ = {**context.environ, "DATADOUBLE_BOUNDARY": "inside"}
        done = super().run(repository, job, event, replace(context, environ=environ))
        for other in self.worlds[namespace]:
            started = self.engine_pods(other) - self.engines[other]
            self.engines[other] |= started
            assert other == namespace or not started, (job, namespace, other, started)
        return done

    def outputs(self, name: str, number: int) -> dict[str, str]:
        """Read the outputs from the build's pod, kept by the cloud, then remove the pod."""
        generated = self.generated[name]
        pods = self.cluster.json("--namespace", generated.namespace, "get", "pods")
        items = pods.get("items")
        assert isinstance(items, list)
        run = f"job/{name}/{number}/"
        found = [
            item
            for item in items
            if isinstance(item, dict)
            and isinstance(item.get("metadata"), dict)
            and _annotation(item, "runUrl") == run
        ]
        assert found, f"{run} ran in no pod"
        live = found[-1]
        assert contains(generated.pod["spec"], live.get("spec")), (
            f"{run}'s pod is not the one its Jenkinsfile declares",
            generated.pod,
            live.get("spec"),
        )
        pod_name = str(_annotation(live, "name", key="metadata"))
        text = self.cluster.kubectl(
            "--namespace", generated.namespace, "exec", pod_name, "--container", CONTAINER, "--",
            "cat", f"{WORKSPACES}/{name}/.datadouble-outputs",
            check=False,
        )  # fmt: skip
        self._sweep(generated.namespace, items)
        return parse_outputs(text)

    def _sweep(self, namespace: str, pods: list[JsonValue]) -> None:
        """Remove the kept pods of every finished build, including builds a trigger started.

        The cloud keeps finished pods, whose Datadouble container sleeps on, so
        ``promote`` started by ``build``'s trigger would leave its pod running.
        """
        for item in pods:
            if not isinstance(item, dict):
                continue
            run = _annotation(item, "runUrl")
            if not isinstance(run, str):
                continue
            if self.server.get(f"/{run}api/json")["building"] is False:
                self.cluster.kubectl(
                    "--namespace", namespace, "delete", "pod",
                    str(_annotation(item, "name", key="metadata")), "--wait=false",
                    "--ignore-not-found",
                )  # fmt: skip


def _annotation(item: dict[str, JsonValue], name: str, *, key: str = "annotations") -> JsonValue:
    """A pod's annotation ``name``, or with ``key="metadata"`` its metadata field."""
    metadata = item.get("metadata")
    assert isinstance(metadata, dict)
    if key == "metadata":
        return metadata.get(name)
    annotations = metadata.get("annotations")
    return annotations.get(name) if isinstance(annotations, dict) else None


@pytest.fixture
def ci_harness(stage: Stage) -> CiHarness:
    return JenkinsOnKind(stage)


@pytest.mark.ci_conformance
# A job here starts a pod as well as its engines', so the lifecycle outlasts the
# default limit; the CI job's own limit still bounds the whole suite.
@pytest.mark.timeout(1800)
class TestJenkinsKubernetes(CiConformance):
    """Jenkins with Kubernetes agents passes the CI/CD conformance suite."""

    def test_each_side_s_engine_pods_start_in_its_namespace(
        self, ci_harness: CiHarness, shop: World
    ) -> None:
        """One spec, naming no namespace, starts each side's engine pods in that side's."""
        assert isinstance(ci_harness, JenkinsOnKind)
        kubernetes = shop.runtime["kubernetes"]
        assert isinstance(kubernetes, dict)
        assert "namespace" not in kubernetes
        production, non_production = (shop.runners[side] for side in Side)
        assert production != non_production
        for job in ("build", "promote", "fetch"):
            done = ci_harness.run(shop.repository, job, Event.MANUAL, shop.context(run_id="601"))
            assert done.state is State.SUCCESS, done.log
        assert ci_harness.engines[production], "build started no engine pod in its namespace"
        provisioned = ci_harness.run(
            shop.repository, "provision", Event.REQUEST_OPENED, shop.context(request=9)
        )
        assert provisioned.state is State.SUCCESS, provisioned.log
        # run() has checked that no run started an engine pod in the other side's namespace.
        assert not ci_harness.engines[production] & ci_harness.engines[non_production]

    def test_an_unmarked_production_namespace_is_refused(
        self, ci_harness: CiHarness, shop: World
    ) -> None:
        """``build``, ``promote`` and ``lock`` need the ConfigMap; Jenkins' mark is ignored."""
        unmarked = shop.context(run_id="501", environ={"RUNNER_ENVIRONMENT": "self-hosted"})
        for job in ("build", "promote", "lock"):
            refused = ci_harness.run(shop.repository, job, Event.MANUAL, unmarked)
            assert refused.state is State.FAILURE, refused.log
            assert refused.outputs.get("error") == "outside_boundary", (job, refused.log)
        assert shop.generations(shop.store) == []
        locked = ci_harness.run(shop.repository, "lock", Event.MANUAL, shop.context(run_id="502"))
        assert locked.state is State.SUCCESS, locked.log


GOLDEN = Path(__file__).parent / "golden"


def _golden(job: str) -> str:
    return (
        GOLDEN / "kubernetes-production" / f"datadouble-shop-{job}.Jenkinsfile.golden"
    ).read_text("utf-8")


def test_the_generated_pod_is_read_from_the_jenkinsfile() -> None:
    declared = generated_pod(_golden("build"))
    spec = declared["spec"]
    assert isinstance(spec, dict)
    containers = spec["containers"]
    assert isinstance(containers, list)
    assert [c["name"] for c in containers if isinstance(c, dict)] == ["jnlp", CONTAINER]
    agent = containers[0]
    assert isinstance(agent, dict)
    assert agent["command"] == ["/bin/sh", "-c", AGENT_COMMAND, "jenkins-agent"]


def test_a_live_pod_may_add_but_not_change() -> None:
    declared = generated_pod(_golden("build"))
    live = json.loads(json.dumps(declared))
    live["spec"]["volumes"].append({"name": "workspace-volume", "emptyDir": {}})
    live["spec"]["containers"][1]["volumeMounts"].insert(
        0, {"name": "workspace-volume", "mountPath": "/home/jenkins/agent"}
    )
    live["spec"]["containers"][0]["image"] = AGENT
    live["spec"]["volumes"][1]["configMap"]["defaultMode"] = 420
    assert contains(declared["spec"], live["spec"])
    moved = json.loads(json.dumps(live))
    moved["spec"]["containers"][1]["volumeMounts"][1]["mountPath"] = "/elsewhere"
    assert not contains(declared["spec"], moved["spec"])
    reordered = json.loads(json.dumps(live))
    reordered["spec"]["containers"][1]["args"] = ["infinity", "sleep"]
    reordered["spec"]["containers"][1]["command"] = ["infinity"]
    assert not contains(declared["spec"], reordered["spec"])
    assert not contains({"a": 1}, [1])
    assert not contains([{"a": 1}], {"a": 1})


def test_the_lock_job_runs_in_a_pod_like_build() -> None:
    lock = _golden("lock")
    record = "'/srv/datadouble/work/.datadouble-notify/shop.lock.json'"
    assert f"['lock', '--ci-record', {record}, '--spec', 'datadouble/shop.yml'," in lock
    assert "artifacts: 'datadouble-lock'," in lock
    assert generated_pod(lock)["spec"] == generated_pod(_golden("build"))["spec"]
