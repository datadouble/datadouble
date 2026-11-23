# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The Jenkinsfiles the Jenkins adapter writes, and how it reports a response.

The Jenkinsfiles are compared with the golden files under ``golden/``, which
the conformance suite also checks with Jenkins' declarative validator. To
accept a deliberate change, run the tests with ``DATADOUBLE_UPDATE_GOLDEN=1``
and review the diff.
"""

import os
from dataclasses import replace
from pathlib import Path

import pytest

from datadouble.ci import CiError, JobKind, Pipeline, PipelineOptions, Report, Side, pipeline
from datadouble.outcome import ExitCode, Outcome
from datadouble.spec import NotifyOn, parse_spec
from datadouble_jenkins import NAME, Jenkins, adapter, render

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


def planned(
    *kinds: JobKind,
    mode: str = "on-demand",
    runner: str | None = "prod-db",
    side: Side | None = None,
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
    )
    return pipeline(parse_spec(SPEC.replace("MODE", mode)), options)


ON_DEMAND = (JobKind.FETCH, JobKind.PROVISION, JobKind.RELEASE, JobKind.PRUNE, JobKind.ACTIVATE)
SCENARIOS = {
    "production": planned(JobKind.BUILD, JobKind.PROMOTE, JobKind.PRUNE, JobKind.LOCK),
    "on-demand": planned(*ON_DEMAND, runner="staging,linux"),
    "pool": planned(
        JobKind.FETCH, JobKind.PROVISION, JobKind.RELEASE, mode="pool", runner="staging"
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


def test_another_docker_command_and_socket() -> None:
    changed = replace(SCENARIOS["production"], docker="podman", docker_socket="/run/d.sock")
    build = render(changed, changed.jobs[0])
    assert "    docker: 'podman',\n" in build
    assert "    dockerSocket: '/run/d.sock',\n" in build
    assert "docker:" not in jenkinsfile("production", JobKind.BUILD)
