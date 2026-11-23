# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The Jenkinsfiles the Jenkins adapter writes, and how it reports a response.

The Jenkinsfiles are compared with the golden files under ``golden/``, which
the conformance suite also checks with Jenkins' declarative validator. To
accept a deliberate change, run the tests with ``DATADOUBLE_UPDATE_GOLDEN=1``
and review the diff.
"""

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

from datadouble.ci import (
    Agent,
    CiError,
    JobKind,
    Pipeline,
    PipelineOptions,
    Report,
    Side,
    pipeline,
)
from datadouble.outcome import ExitCode, Outcome
from datadouble.spec import NotifyOn, parse_spec
from datadouble_jenkins import NAME, Jenkins, adapter, pod, render

if TYPE_CHECKING:
    from pydantic import JsonValue

GOLDEN = Path(__file__).parent / "golden"

SPEC = """
name: shop
source:
  engine: postgres
  connection: {dsn: {env: SHOP_SOURCE_DSN}}
store: [{filesystem: {path: /srv/datadouble/store}}]
signing: {cosign: {key: {env: SHOP_COSIGN_KEY}}}
trust: {cosign: {public_key: {env: SHOP_COSIGN_PUB}}}
promote: {to: [{s3: {bucket: shop-export, prefix: "it's"}}]}
fetch:
  from: {s3: {bucket: shop-export, prefix: images}}
  staging: /srv/datadouble/staging
notify:
  when: [failure, warning]
  to:
    - webhook: {url: {env: ALERTS_WEBHOOK}}
    - email: {to: [data-team@example.com, oncall@example.com]}
replicate: {using: {docker-volume: {}}, mode: MODE, size: 1}
"""
KUBERNETES = (
    SPEC.replace("docker-volume", "k8s-pv")
    + "runtime: {kubernetes: {claim: datadouble, mount: /srv/datadouble"
)


def planned(
    *kinds: JobKind,
    mode: str = "on-demand",
    runner: str | None = "prod-db",
    side: Side | None = None,
    agent: Agent = Agent.DOCKER,
    runtime: str = "",
    cloud: str | None = None,
) -> Pipeline:
    options = PipelineOptions(
        kinds=kinds,
        spec_path="datadouble/shop.yml",
        image="ghcr.io/datadouble/datadouble:v1.2.3",
        version="1.2.3",
        owner="999:999",
        runner=runner,
        work=Path("/srv/datadouble/work"),
        side=side,
        agent=agent,
        cloud=cloud,
    )
    kubernetes = KUBERNETES + runtime + "}}\n"
    text = kubernetes if agent is Agent.KUBERNETES else SPEC
    return pipeline(parse_spec(text.replace("MODE", mode)), options)


ON_DEMAND = (JobKind.FETCH, JobKind.PROVISION, JobKind.RELEASE, JobKind.PRUNE, JobKind.ACTIVATE)
SCENARIOS = {
    "production": planned(JobKind.BUILD, JobKind.PROMOTE, JobKind.PRUNE, JobKind.LOCK),
    "on-demand": planned(*ON_DEMAND, runner="staging,linux"),
    "pool": planned(
        JobKind.FETCH, JobKind.PROVISION, JobKind.RELEASE, mode="pool", runner="staging"
    ),
    "kubernetes-production": planned(
        JobKind.BUILD,
        JobKind.PROMOTE,
        JobKind.PRUNE,
        JobKind.LOCK,
        runner="shop-production",
        cloud="prod-k8s",
        agent=Agent.KUBERNETES,
        runtime=", image_pull_secrets: [registry]",
    ),
    "kubernetes-on-demand": planned(
        *ON_DEMAND, runner="shop-staging", agent=Agent.KUBERNETES, runtime=", node: worker-3"
    ),
}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_jenkinsfiles_match_the_golden_files(scenario: str) -> None:
    directory = GOLDEN / scenario
    generated = {
        f"{f.path.name}.golden": f.content for f in Jenkins().generate(SCENARIOS[scenario])
    }
    if os.environ.get("DATADOUBLE_UPDATE_GOLDEN"):
        directory.mkdir(parents=True, exist_ok=True)
        for old in directory.glob("*.golden"):
            old.unlink()
        for name, content in generated.items():
            (directory / name).write_text(content, encoding="utf-8")
    on_disk = {path.name: path.read_text(encoding="utf-8") for path in directory.glob("*.golden")}
    assert generated == on_disk


def jenkinsfile(scenario: str, kind: JobKind) -> str:
    planned_jobs = SCENARIOS[scenario]
    (job,) = [job for job in planned_jobs.jobs if job.kind is kind]
    return render(planned_jobs, job)


def test_production_jenkinsfiles() -> None:
    build = jenkinsfile("production", JobKind.BUILD)
    assert "library(identifier: 'datadouble@v1.2.3', retriever: modernSCM(" in build
    assert "libraryPath: 'ci/jenkins/'" in build
    assert "agent { label 'prod-db' }" in build
    assert "cron('TZ=UTC\\n0 2 * * 1')" in build
    assert "(params.RETRY_KEY ?: env.BUILD_TAG)" in build
    assert "string(credentialsId: 'COSIGN_PASSWORD', variable: 'COSIGN_PASSWORD')" in build
    assert "timeout(time: 720, unit: 'MINUTES')" in build
    promote = jenkinsfile("production", JobKind.PROMOTE)
    assert (
        "upstream(upstreamProjects: 'datadouble-shop-build',"
        " threshold: hudson.model.Result.UNSTABLE)"
    ) in promote
    assert "'it\\'s'" not in promote  # the prefix is in the spec, not the Jenkinsfile
    prune = jenkinsfile("production", JobKind.PRUNE)
    assert "stage('datadouble prune 1')" in prune
    assert "(params.DRY_RUN ? '--dry-run' : '')" in prune
    assert "COSIGN_PASSWORD" not in prune
    assert "string(credentialsId: 'ALERTS_WEBHOOK', variable: 'ALERTS_WEBHOOK')" in prune
    assert "job: 'prune'," in prune


def test_email_is_mailed_after_the_build() -> None:
    planned_jobs = SCENARIOS["production"]
    build = render(planned_jobs, planned_jobs.jobs[0])
    post = build[build.index("    post {") :]
    mail = (
        "mail(to: 'data-team@example.com, oncall@example.com',"
        " subject: 'Datadouble shop build: {0}', body: 'Datadouble shop build: {0}.'"
        " + (env.BUILD_URL ? ' The run: ' + env.BUILD_URL : ''))"
    )
    for condition in ("failure", "aborted", "unstable"):
        assert f"        {condition} {{\n            script {{\n                try {{\n" in post
        assert mail.format(condition) in post
    assert 'echo "notify\'s email could not be sent"' in post
    assert planned_jobs.notify is not None
    failures = replace(
        planned_jobs, notify=planned_jobs.notify.model_copy(update={"when": (NotifyOn.FAILURE,)})
    )
    only = render(failures, failures.jobs[0])
    assert "failure {" in only
    assert "unstable {" not in only
    webhooks = planned_jobs.notify.model_copy(update={"to": planned_jobs.notify.to[:1]})
    assert "post {" not in render(replace(planned_jobs, notify=webhooks), planned_jobs.jobs[0])
    assert "post {" not in render(replace(planned_jobs, notify=None), planned_jobs.jobs[0])


def test_request_jenkinsfiles() -> None:
    release = jenkinsfile("on-demand", JobKind.RELEASE)
    assert "string(name: 'OWNER'" in release
    assert "when { expression { params.OWNER } }" in release
    assert "when { expression { !params.OWNER } }" in release
    assert "agent { label 'staging && linux' }" in release
    activate = jenkinsfile("on-demand", JobKind.ACTIVATE)
    assert "arguments: ['activate', params.GENERATION, '--spec', 'datadouble/shop.yml']" in activate


def test_quoting() -> None:
    planned_jobs = SCENARIOS["production"]
    build = replace(planned_jobs.jobs[0], mounts=("/srv/it's\\here",))
    assert "mounts: ['/srv/it\\'s\\\\here']" in render(planned_jobs, build)


def test_instructions() -> None:
    jenkins = Jenkins()
    production = jenkins.instructions(SCENARIOS["production"])
    assert production[0] == (
        "create the Pipeline job datadouble-shop-build, running"
        " jenkins/datadouble-shop-build.Jenkinsfile from this repository (Pipeline script from"
        " SCM), and build it once so Jenkins reads its triggers"
    )
    assert production[-1] == "add each variable above as a Secret text credential with that ID"
    requests = jenkins.instructions(SCENARIOS["on-demand"])
    assert requests[-1].startswith(
        "start datadouble-shop-provision and datadouble-shop-release from each request's own"
    )
    quiet = jenkins.instructions(planned(JobKind.PRUNE, runner="staging", side=Side.NON_PRODUCTION))
    assert len(quiet) == 2
    silent = planned(JobKind.PRUNE, runner="staging", side=Side.NON_PRODUCTION)
    silent = replace(
        silent, notify=None, jobs=tuple(replace(job, environment=()) for job in silent.jobs)
    )
    assert len(jenkins.instructions(silent)) == 1
    assert "withCredentials" not in render(silent, silent.jobs[0])


def test_labels_are_required() -> None:
    with pytest.raises(CiError, match="name the agent labels"):
        Jenkins().generate(planned(JobKind.FETCH, runner=None))


def test_run_time() -> None:
    jenkins = Jenkins()
    assert jenkins.vendor_hosted({"RUNNER_ENVIRONMENT": "github-hosted"}) is None
    jenkins.mask("nothing happens")
    assert jenkins.run_url({"BUILD_URL": "https://jenkins.example.com/job/x/7/"}) == (
        "https://jenkins.example.com/job/x/7/"
    )
    assert jenkins.run_url({}) is None


def report(outcome: Outcome, **outputs: str) -> Report:
    fields = {"outcome": str(outcome), **outputs}
    return Report(command="run", outcome=outcome, outputs=fields, summary="")


def test_reports(tmp_path: Path) -> None:
    jenkins = Jenkins()
    assert jenkins.report(report(Outcome.SUCCESS, generation="0193"), tmp_path) is ExitCode.SUCCESS
    assert (tmp_path / "outputs").read_text() == "outcome=success\ngeneration=0193\n"
    assert jenkins.report(report(Outcome.WARNING, note="two\nlines"), tmp_path) is ExitCode.WARNING
    assert (tmp_path / "outputs").read_text() == "outcome=warning\n"
    assert jenkins.report(report(Outcome.FAILURE), None) is ExitCode.FAILURE


def test_the_entry_point() -> None:
    assert adapter().name == NAME == "jenkins"
    assert adapter().agents == (Agent.DOCKER, Agent.KUBERNETES)


def test_kubernetes_jenkinsfiles() -> None:
    build = jenkinsfile("kubernetes-production", JobKind.BUILD)
    assert "        kubernetes {\n            cloud 'prod-k8s'\n" in build
    assert "            namespace 'shop-production'\n            yaml '''\n" in build
    assert "label" not in build
    assert "container: 'datadouble'," in build
    assert "image:" not in build.split("stages {")[1]
    assert "mounts:" not in build
    assert "withCredentials([string(credentialsId: 'ALERTS_WEBHOOK'" in build
    assert "string(credentialsId: 'COSIGN_PASSWORD'" in build
    assert "    job: 'build',\n                        container: 'datadouble'," in build
    lock = jenkinsfile("kubernetes-production", JobKind.LOCK)
    assert "artifacts: 'datadouble-lock'," in lock
    assert "post {" in lock
    fetch = jenkinsfile("kubernetes-on-demand", JobKind.FETCH)
    assert "cloud" not in fetch
    assert "namespace 'shop-staging'" in fetch
    assert "kubernetes.io/hostname: worker-3" in fetch


def test_kubernetes_pods() -> None:
    production = SCENARIOS["kubernetes-production"]
    manifest = pod(production)
    rendered = jenkinsfile("kubernetes-production", JobKind.BUILD)
    embedded = rendered.split("yaml '''\n")[1].split("'''")[0]
    assert yaml.safe_load(embedded) == manifest
    spec = manifest["spec"]
    assert isinstance(spec, dict)
    assert spec["serviceAccountName"] == "datadouble-shop-production"
    assert "securityContext" not in spec
    assert spec["imagePullSecrets"] == [{"name": "registry"}]
    assert spec["volumes"] == [
        {"name": "work", "persistentVolumeClaim": {"claimName": "datadouble"}},
        {"name": "boundary", "configMap": {"name": "datadouble-boundary", "optional": True}},
    ]
    assert "nodeSelector" not in spec
    containers = spec["containers"]
    assert isinstance(containers, list)
    agent, container = containers
    assert agent == {
        "name": "jnlp",
        "command": [
            "/bin/sh",
            "-c",
            'umask 0000 && exec /usr/local/bin/jenkins-agent "$@"',
            "jenkins-agent",
        ],
    }
    assert container == {
        "name": "datadouble",
        "image": "ghcr.io/datadouble/datadouble:v1.2.3",
        "command": ["sleep"],
        "args": ["infinity"],
        "securityContext": {
            "runAsUser": 999,
            "runAsGroup": 999,
            "runAsNonRoot": True,
            "allowPrivilegeEscalation": False,
        },
        "volumeMounts": [
            {"name": "work", "mountPath": "/srv/datadouble"},
            {"name": "boundary", "mountPath": "/etc/datadouble-boundary", "readOnly": True},
        ],
    }


def test_kubernetes_instructions() -> None:
    jenkins = Jenkins()
    production = jenkins.instructions(SCENARIOS["kubernetes-production"])
    assert production[0] == (
        "keep the production side in its own namespace, shop-production, apart from the other"
        " side's, ideally in its own cluster: its claim holds that side's data"
    )
    assert production[1] == (
        "install the Kubernetes plugin and set up the cloud prod-k8s, whose credentials need a"
        " Role in shop-production allowing create, delete, get, list, patch, update and watch"
        " on pods; create and get on pods/exec; get, list and watch on pods/log; and watch on"
        " events"
    )
    assert production[2] == (
        "in shop-production, create the PersistentVolumeClaim datadouble (ReadWriteMany, or"
        " ReadWriteOnce with the runtime's node set so every pod runs on one node), holding"
        " /srv/datadouble, owned by 999:999"
    )
    assert "service account datadouble-shop-production" in production[3]
    assert production[3] == (
        "in shop-production, create the service account datadouble-shop-production, bound to a"
        " Role allowing get, list, watch, create, delete and patch on pods, pods/log, pods/exec,"
        " pods/portforward and secrets; and get and list on persistentvolumeclaims"
    )
    assert production[4].startswith("inside the production boundary only, create the ConfigMap")
    assert production[5].startswith("create the Pipeline job datadouble-shop-build")
    on_demand = jenkins.instructions(SCENARIOS["kubernetes-on-demand"])
    assert "its default cloud" in on_demand[1]
    assert "ReadWriteOnce is enough" in on_demand[2]
    assert on_demand[4].endswith(
        "ClusterRole allowing get, list, watch, create, delete and patch on persistentvolumes"
    )
    assert not any("datadouble-boundary" in step for step in on_demand)


def kubernetes(*kinds: JobKind, spec: str, runner: str | None = "shop-staging") -> Pipeline:
    options = PipelineOptions(
        kinds=kinds,
        spec_path="datadouble/shop.yml",
        image="ghcr.io/datadouble/datadouble:v1.2.3",
        version="1.2.3",
        owner="999:999",
        runner=runner,
        agent=Agent.KUBERNETES,
    )
    return pipeline(parse_spec(spec.replace("MODE", "pool")), options)


def test_kubernetes_needs_a_namespace() -> None:
    with pytest.raises(CiError, match=r"name the Kubernetes namespace the jobs' pods run in"):
        Jenkins().generate(kubernetes(JobKind.FETCH, spec=SPEC, runner=None))


def test_kubernetes_without_engines_needs_no_runtime() -> None:
    manifest = json.dumps(pod(kubernetes(JobKind.FETCH, JobKind.ACTIVATE, spec=SPEC)))
    assert '"mountPath": "/srv/datadouble/staging"' in manifest
    assert "nodeSelector" not in manifest


def test_kubernetes_engines_need_the_kubernetes_runtime() -> None:
    docker = kubernetes(JobKind.PROVISION, JobKind.RELEASE, spec=SPEC)
    with pytest.raises(CiError, match="provision, release start engine software"):
        Jenkins().generate(docker)
    other = replace(docker, runtime={"docker": {"network": "host"}})
    with pytest.raises(CiError, match="the spec's runtime must be kubernetes"):
        Jenkins().instructions(other)
    missing = replace(docker, runtime={"kubernetes": {"node": "worker-3"}})
    with pytest.raises(CiError, match="settings are invalid: claim, mount"):
        Jenkins().generate(missing)
    elsewhere: dict[str, JsonValue] = {
        "namespace": "shop-production",
        "claim": "datadouble",
        "mount": "/srv",
    }
    with pytest.raises(
        CiError, match=r"namespace is shop-production, but the non-production side's pods run in"
    ):
        Jenkins().generate(replace(docker, runtime={"kubernetes": elsewhere}))
    own = replace(docker, runtime={"kubernetes": {**elsewhere, "namespace": "shop-staging"}})
    assert "namespace 'shop-staging'" in Jenkins().generate(own)[0].content
    claim: dict[str, JsonValue] = {"claim": "work", "mount": "/srv"}
    with pytest.raises(CiError, match="must name the claim datadouble"):
        Jenkins().generate(replace(docker, runtime={"kubernetes": claim}))
    relative: dict[str, JsonValue] = {
        "namespace": "shop-staging",
        "claim": "datadouble",
        "mount": "srv",
    }
    with pytest.raises(CiError, match="must be an absolute path"):
        Jenkins().generate(replace(docker, runtime={"kubernetes": relative}))


def test_kubernetes_paths_must_be_on_the_claim() -> None:
    settings: dict[str, JsonValue] = {
        "namespace": "shop-production",
        "claim": "datadouble",
        "mount": "/srv/datadouble/work",
    }
    elsewhere = replace(SCENARIOS["kubernetes-production"], runtime={"kubernetes": settings})
    with pytest.raises(CiError, match=r"mounted at /srv/datadouble/work; these are not under it"):
        Jenkins().generate(elsewhere)


def test_another_docker_command_and_socket() -> None:
    changed = replace(SCENARIOS["production"], docker="podman", docker_socket="/run/d.sock")
    build = render(changed, changed.jobs[0])
    assert "    docker: 'podman',\n" in build
    assert "    dockerSocket: '/run/d.sock',\n" in build
    assert "docker:" not in jenkinsfile("production", JobKind.BUILD)
