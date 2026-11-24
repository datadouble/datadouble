# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The configuration the GitLab adapter writes, and how it reports a response.

The configuration is compared with the golden files under ``golden/``, which
the conformance suite also checks with GitLab's CI Lint. To accept a
deliberate change, run the tests with ``DATADOUBLE_UPDATE_GOLDEN=1`` and
review the diff.
"""

import base64
import hashlib
import os
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from pydantic import JsonValue, TypeAdapter

from datadouble.ci import Agent, CiError, JobKind, Pipeline, PipelineOptions, Report, Side, pipeline
from datadouble.outcome import ExitCode, Outcome
from datadouble.spec import FileSecret, NotifyOn, Webhook, WebhookTarget, parse_spec
from datadouble_gitlab import NAME, TEMPLATE_URL, GitLab, adapter, integrity, template

GOLDEN = Path(__file__).parent / "golden"
TEMPLATE_FILE = Path(__file__).parents[1] / "src" / "datadouble_gitlab" / "datadouble.yml"

SPEC = """
name: shop
sources:
  app:
    engine: postgres
    connection:
      dsn:
        env: SHOP_SOURCE_DSN
secrets:
  anonymization_key:
    env: SHOP_ANONYMIZATION_KEY
store:
  - filesystem:
      path: /srv/datadouble/store
signing:
  cosign:
    key:
      env: SHOP_COSIGN_KEY
trust:
  cosign:
    public_key:
      env: SHOP_COSIGN_PUB
promote:
  to:
    - s3:
        bucket: shop-export
        prefix: images
fetch:
  from:
    s3:
      bucket: shop-export
      prefix: images
  staging: /srv/datadouble/staging
notify:
  when: [failure, warning]
  to:
    - webhook:
        url:
          env: ALERTS_WEBHOOK
    - email:
        to: [data-team@example.com, oncall@example.com]
replicate:
  using:
    docker-volume: {}
  mode: MODE
  size: 1
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


SCENARIOS = {
    "production": planned(JobKind.BUILD, JobKind.PROMOTE, JobKind.PRUNE, JobKind.LOCK),
    "on-demand": planned(
        JobKind.FETCH,
        JobKind.PROVISION,
        JobKind.RELEASE,
        JobKind.PRUNE,
        JobKind.ACTIVATE,
        runner="staging,linux",
    ),
    "pool": planned(
        JobKind.FETCH, JobKind.PROVISION, JobKind.RELEASE, mode="pool", runner="staging"
    ),
}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_configuration_matches_the_golden_files(scenario: str) -> None:
    directory = GOLDEN / scenario
    generated = {f"{f.path.name}.golden": f.content for f in GitLab().generate(SCENARIOS[scenario])}
    if os.environ.get("DATADOUBLE_UPDATE_GOLDEN"):
        directory.mkdir(parents=True, exist_ok=True)
        for old in directory.glob("*.golden"):
            old.unlink()
        for name, content in generated.items():
            (directory / name).write_text(content, encoding="utf-8")
    on_disk = {path.name: path.read_text(encoding="utf-8") for path in directory.glob("*.golden")}
    assert generated == on_disk


_YAML: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


def jobs(scenario: str) -> dict[str, JsonValue]:
    (generated,) = GitLab().generate(SCENARIOS[scenario])
    return _YAML.validate_python(yaml.safe_load(generated.content))


def at(value: JsonValue, *path: str | int) -> JsonValue:
    """The value at a path of keys and indexes."""
    for step in path:
        if isinstance(step, int):
            assert isinstance(value, list)
            value = value[step]
        else:
            assert isinstance(value, dict)
            value = value[step]
    return value


def test_the_template_is_pinned() -> None:
    include = at(jobs("production"), "include", 0)
    digest = base64.b64encode(hashlib.sha256(TEMPLATE_FILE.read_bytes()).digest()).decode()
    assert include == {
        "remote": TEMPLATE_URL.format(version="1.2.3"),
        "integrity": f"sha256-{digest}",
    }
    assert integrity() == f"sha256-{digest}"
    assert template() == TEMPLATE_FILE.read_bytes()


def test_production_jobs() -> None:
    config = jobs("production")
    build = config["datadouble-shop-build"]
    assert at(build, "tags") == ["prod-db"]
    assert at(build, "timeout") == "720m"
    assert at(build, "rules") == [
        {
            "if": '$DATADOUBLE_JOB == "shop-build"'
            " && $CI_PIPELINE_SOURCE =~ /^(api|schedule|trigger|web)$/"
        }
    ]
    assert at(build, "variables", "DATADOUBLE_ENVIRONMENT") == (
        "ALERTS_WEBHOOK\nCOSIGN_PASSWORD\nSHOP_ANONYMIZATION_KEY\nSHOP_COSIGN_KEY\nSHOP_SOURCE_DSN\n"
    )
    assert at(build, "variables", "DATADOUBLE_CI_JOB") == "build"
    arguments = at(build, "variables", "DATADOUBLE_ARGUMENTS")
    assert isinstance(arguments, str)
    assert arguments.endswith("--retry-key\n$CI_PIPELINE_ID\n")
    promote = config["datadouble-shop-promote"]
    assert at(promote, "needs") == [{"job": "datadouble-shop-build", "optional": True}]
    assert at(promote, "rules", 1, "if") == (
        '$DATADOUBLE_JOB == "shop-build" && $CI_PIPELINE_SOURCE =~ /^(schedule|web|api|trigger)$/'
    )
    assert "datadouble-shop-prune-production-1" in config
    assert "datadouble-shop-prune-production-2" in config


def test_merge_request_copies() -> None:
    config = jobs("on-demand")
    assert at(config, "datadouble-shop-provision", "environment") == {
        "name": "datadouble/shop/mr-$CI_MERGE_REQUEST_IID",
        "on_stop": "datadouble-shop-release-1",
    }
    assert at(config, "datadouble-shop-release-1", "environment") == {
        "name": "datadouble/shop/mr-$CI_MERGE_REQUEST_IID",
        "action": "stop",
    }
    sweep = at(config, "datadouble-shop-release-2")
    assert isinstance(sweep, dict)
    assert "environment" not in sweep
    assert at(config, "variables", "DATADOUBLE_DRY_RUN", "options") == ["", "--dry-run"]
    alone = GitLab().generate(planned(JobKind.PROVISION, runner="staging"))
    assert "on_stop" not in alone[0].content


def notify_on_failure(planned_jobs: Pipeline) -> Pipeline:
    assert planned_jobs.notify is not None
    on_failure = planned_jobs.notify.model_copy(update={"when": (NotifyOn.FAILURE,)})
    return replace(planned_jobs, notify=on_failure)


def test_instructions() -> None:
    gitlab = GitLab()
    production = gitlab.instructions(SCENARIOS["production"])
    assert production[0] == (
        "include .gitlab/datadouble-shop-production.yml from .gitlab-ci.yml (include: local)"
    )
    assert any("`0 2 * * 1`" in step and "DATADOUBLE_JOB=shop-build" in step for step in production)
    assert production[-2] == "define each variable above as a masked, protected CI/CD variable"
    assert production[-1].startswith(
        "for notify's email, turn on the project's Pipeline status emails integration"
        " (Settings > Integrations) for data-team@example.com, oncall@example.com,"
    )
    requests = gitlab.instructions(SCENARIOS["on-demand"])
    assert any(step.startswith("run merge request pipelines") for step in requests)
    assert any(
        step.startswith("on-demand provision and release run in merge request pipelines")
        and "make ALERTS_WEBHOOK available there (not protected," in step
        for step in requests
    )
    assert not any("ALERTS_WEBHOOK" in step for step in production)
    on_demand = SCENARIOS["on-demand"]
    assert on_demand.notify is not None
    mounted = on_demand.notify.model_copy(
        update={"to": (WebhookTarget(webhook=Webhook(url=FileSecret(file=Path("/run/hook")))),)}
    )
    from_file = gitlab.instructions(replace(on_demand, notify=mounted))
    assert not any("merge request pipelines, which see" in step for step in from_file)
    quiet = gitlab.instructions(planned(JobKind.ACTIVATE, runner="staging"))
    assert quiet[0] == (
        "include .gitlab/datadouble-shop-non-production.yml from .gitlab-ci.yml (include: local)"
    )
    assert quiet[-1].endswith("list a webhook to hear about warnings")
    quiet_pipeline = planned(JobKind.ACTIVATE, runner="staging")
    failures = gitlab.instructions(notify_on_failure(quiet_pipeline))
    assert failures[-1].endswith("notifying only broken pipelines")
    assert quiet_pipeline.notify is not None
    recovering = quiet_pipeline.notify.model_copy(
        update={"when": (NotifyOn.FAILURE, NotifyOn.RECOVERY)}
    )
    recoveries = gitlab.instructions(replace(quiet_pipeline, notify=recovering))
    assert (
        "that integration never mails a recovery, so for recovery mail turn on the Fixed"
        in (recoveries[-1])
    )
    silent = replace(
        quiet_pipeline,
        notify=None,
        jobs=tuple(replace(job, environment=()) for job in quiet_pipeline.jobs),
    )
    assert gitlab.instructions(silent) == (
        "include .gitlab/datadouble-shop-non-production.yml from .gitlab-ci.yml (include: local)",
    )
    assert "DATADOUBLE_ENVIRONMENT" not in GitLab().generate(silent)[0].content
    assert gitlab.run_url({"CI_JOB_URL": "https://gitlab.example.com/shop/-/jobs/7"}) == (
        "https://gitlab.example.com/shop/-/jobs/7"
    )
    assert gitlab.run_url({}) is None


def test_runners() -> None:
    gitlab = GitLab()
    with pytest.raises(CiError, match="selects GitLab's hosted runners"):
        gitlab.generate(planned(JobKind.FETCH, runner="saas-linux-small-amd64"))
    with pytest.raises(CiError, match="name the runner tags"):
        gitlab.generate(planned(JobKind.FETCH, runner=None))


def test_vendor_hosted() -> None:
    gitlab = GitLab()
    hosted = {"CI_RUNNER_TAGS": '["saas-linux-small-amd64", "linux"]'}
    assert gitlab.vendor_hosted(hosted) == "a GitLab-hosted runner (saas-linux-small-amd64)"
    assert gitlab.vendor_hosted({"CI_RUNNER_TAGS": '["prod-db"]'}) is None
    assert gitlab.vendor_hosted({}) is None
    gitlab.mask("nothing happens")


def report(outcome: Outcome, **outputs: str) -> Report:
    return Report(
        command="run", outcome=outcome, outputs={"outcome": str(outcome), **outputs}, summary=""
    )


def test_reports(tmp_path: Path) -> None:
    gitlab = GitLab()
    assert gitlab.report(report(Outcome.SUCCESS, generation="0193"), tmp_path) is ExitCode.SUCCESS
    assert (tmp_path / "outputs").read_text() == (
        "DATADOUBLE_OUTCOME=success\nDATADOUBLE_GENERATION=0193\n"
    )
    assert gitlab.report(report(Outcome.WARNING, note="two\nlines"), tmp_path) is ExitCode.WARNING
    assert (tmp_path / "outputs").read_text() == "DATADOUBLE_OUTCOME=warning\n"
    assert gitlab.report(report(Outcome.FAILURE), None) is ExitCode.FAILURE


def test_the_entry_point() -> None:
    assert adapter().name == NAME == "gitlab"
    assert adapter().agents == (Agent.DOCKER,)


def test_jobs_without_host_paths() -> None:
    planned = SCENARIOS["production"]
    bare = replace(planned, jobs=(replace(planned.jobs[0], mounts=()),))
    (generated,) = GitLab().generate(bare)
    assert "DATADOUBLE_MOUNTS" not in generated.content


def test_another_docker_command_and_socket() -> None:
    changed = replace(SCENARIOS["production"], docker="podman", docker_socket="/run/d.sock")
    (generated,) = GitLab().generate(changed)
    config = _YAML.validate_python(yaml.safe_load(generated.content))
    variables = at(config, "datadouble-shop-build", "variables")
    assert isinstance(variables, dict)
    assert variables["DATADOUBLE_DOCKER"] == "podman"
    assert variables["DATADOUBLE_DOCKER_SOCKET"] == "/run/d.sock"
    assert "DATADOUBLE_DOCKER" not in str(jobs("production"))
