# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The Applications the Argo CD adapter writes, and how it reports a response.

The Applications are compared with the golden files under ``golden/``, which
the conformance suite also renders with the chart and checks with
kubeconform. To accept a deliberate change, run the tests with
``DATADOUBLE_UPDATE_GOLDEN=1`` and review the diff.
"""

import io
import os
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from pydantic import JsonValue, TypeAdapter

from datadouble.ci import CiError, JobKind, Pipeline, PipelineOptions, Report, Side, pipeline
from datadouble.outcome import ExitCode, Outcome
from datadouble.spec import parse_spec
from datadouble_argocd import NAME, ArgoCd, adapter

GOLDEN = Path(__file__).parent / "golden"

SPEC = """
name: shop
source:
  engine: postgres
  connection: {dsn: {env: SHOP_SOURCE_DSN}}
runtime: {kubernetes: {namespace: shop, claim: datadouble, mount: /srv/datadouble}}
store: [{filesystem: {path: /srv/datadouble/store}}]
signing: {cosign: {key: {env: SHOP_COSIGN_KEY}}}
trust: {cosign: {public_key: {env: SHOP_COSIGN_PUB}}}
promote: {to: [{filesystem: {path: /srv/datadouble/export}}]}
fetch:
  from: {filesystem: {path: /srv/datadouble/export}}
  staging: /srv/datadouble/staging
replicate: {using: {k8s-pv: {path: /srv/datadouble/copies}}, mode: MODE, size: 1}
"""
_YAML: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


def planned(
    *kinds: JobKind,
    mode: str = "on-demand",
    runner: str | None = "shop",
    side: Side | None = None,
    spec: str = SPEC,
) -> Pipeline:
    text = spec.replace("MODE", mode)
    options = PipelineOptions(
        kinds=kinds,
        spec_path="datadouble/shop.yml",
        image="ghcr.io/datadouble/datadouble:v1.2.3",
        version="1.2.3",
        owner="999:999",
        runner=runner,
        work=Path("/srv/datadouble/work"),
        side=side,
        spec_text=text,
        lock_text="{}\n",
    )
    return pipeline(parse_spec(text), options)


ON_DEMAND = (JobKind.FETCH, JobKind.PROVISION, JobKind.RELEASE, JobKind.PRUNE, JobKind.ACTIVATE)
SCENARIOS = {
    "production": planned(JobKind.BUILD, JobKind.PROMOTE, JobKind.PRUNE, JobKind.LOCK),
    "on-demand": planned(*ON_DEMAND),
    "pool": planned(JobKind.FETCH, JobKind.PROVISION, JobKind.RELEASE, mode="pool"),
}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_applications_match_the_golden_files(scenario: str) -> None:
    directory = GOLDEN / scenario
    generated = {f"{f.path.name}.golden": f.content for f in ArgoCd().generate(SCENARIOS[scenario])}
    if os.environ.get("DATADOUBLE_UPDATE_GOLDEN"):
        directory.mkdir(parents=True, exist_ok=True)
        for old in directory.glob("*.golden"):
            old.unlink()
        for name, content in generated.items():
            (directory / name).write_text(content, encoding="utf-8")
    on_disk = {path.name: path.read_text(encoding="utf-8") for path in directory.glob("*.golden")}
    assert generated == on_disk


def at(value: JsonValue, *path: str | int) -> JsonValue:
    for step in path:
        if isinstance(step, int):
            assert isinstance(value, list)
            value = value[step]
        else:
            assert isinstance(value, dict)
            value = value[step]
    return value


def values(scenario: str) -> JsonValue:
    (generated,) = ArgoCd().generate(SCENARIOS[scenario])
    application = _YAML.validate_python(yaml.safe_load(generated.content))
    return at(application, "spec", "source", "helm", "valuesObject")


def test_production_values() -> None:
    production = values("production")
    assert at(production, "claim") == {"name": "datadouble", "mount": "/srv/datadouble"}
    assert at(production, "user") == {"uid": 999, "gid": 999}
    assert at(production, "secret") == "datadouble-shop"
    assert at(production, "persistentVolumes") is False
    assert at(production, "spec", "lock") == "{}\n"
    jobs = at(production, "jobs")
    assert isinstance(jobs, list)
    build, promote, prune, lock = jobs
    assert at(build, "schedule") == "0 2 * * 1"
    assert at(build, "steps", 0, "arguments", 0) == "run"
    assert at(build, "steps", 1, "arguments", 0) == "promote"
    assert at(build, "timeoutMinutes") == 780
    assert at(build, "steps", 0, "arguments", -1) == "$(DATADOUBLE_RUN)"
    assert at(promote, "schedule") == ""
    assert at(prune, "name") == "prune-production"
    assert isinstance(prune, dict)
    assert "artifacts" not in prune
    assert at(lock, "schedule") == ""
    assert at(lock, "artifacts") == "datadouble-lock"
    assert at(lock, "steps", 0, "arguments") == [
        "lock", "--spec", "datadouble/shop.yml", "--output", "datadouble-lock"
    ]  # fmt: skip


def test_request_hooks() -> None:
    jobs = at(values("on-demand"), "jobs")
    assert isinstance(jobs, list)
    hooks = {
        at(job, "name"): at(job, "hook") for job in jobs if isinstance(job, dict) and "hook" in job
    }
    assert hooks == {"request-copies": "PreSync", "request-release": "PostDelete"}
    names = [at(job, "name") for job in jobs]
    assert "release" in names
    assert "provision" not in names


def test_paths_share_one_claim() -> None:
    scattered = SPEC.replace("/srv/datadouble/export", "/elsewhere/export")
    with pytest.raises(CiError, match="must share a directory"):
        ArgoCd().generate(planned(JobKind.PROMOTE, spec=scattered))
    with pytest.raises(CiError, match="name the Kubernetes namespace"):
        ArgoCd().generate(planned(JobKind.FETCH, runner=None))


def test_instructions() -> None:
    argocd = ArgoCd()
    production = argocd.instructions(SCENARIOS["production"])
    assert production[0] == (
        "in the namespace shop, create the PersistentVolumeClaim datadouble, holding"
        " /srv/datadouble, owned by 999:999"
    )
    assert any(step.startswith("inside the production boundary only") for step in production)
    requests = argocd.instructions(SCENARIOS["on-demand"])
    assert requests[-1].startswith("for on-demand copies, make an ApplicationSet")
    quiet = planned(JobKind.PRUNE, side=Side.NON_PRODUCTION)
    assert not any("Secret" in step for step in argocd.instructions(quiet))


def test_run_time(tmp_path: Path) -> None:
    log = tmp_path / "termination-log"
    argocd = ArgoCd(log)
    assert argocd.vendor_hosted({"RUNNER_ENVIRONMENT": "github-hosted"}) is None
    argocd.mask("nothing happens")
    warned = Report(
        command="run", outcome=Outcome.WARNING, outputs={"outcome": "warning"}, summary=""
    )
    assert argocd.report(warned, None) is ExitCode.SUCCESS
    assert not log.exists()
    log.write_text("", encoding="utf-8")
    failed = Report(command="run", outcome=Outcome.FAILURE, outputs={"error": "a\nb"}, summary="")
    assert argocd.report(failed, None) is ExitCode.FAILURE
    assert log.read_text() == ""
    assert argocd.report(warned, tmp_path) is ExitCode.SUCCESS
    assert (tmp_path / "outputs").read_text() == "outcome=warning\n"


def test_published_files_go_to_the_log(tmp_path: Path) -> None:
    lock = tmp_path / "catalog.lock"
    lock.write_text("{}\n", encoding="utf-8")
    changes = tmp_path / "changes.txt"
    changes.write_text("no changes from the earlier lock\n", encoding="utf-8")
    stream = io.StringIO()
    locked = Report(
        command="lock",
        outcome=Outcome.SUCCESS,
        outputs={"outcome": "success", "changes": "0"},
        summary="",
        files=(lock, changes),
    )
    assert ArgoCd(tmp_path / "termination-log", stream).report(locked, None) is ExitCode.SUCCESS
    assert stream.getvalue() == (
        f"==> {lock} <==\n{{}}\n==> {changes} <==\nno changes from the earlier lock\n"
    )


def test_the_entry_point() -> None:
    assert adapter().name == NAME == "argocd"


def test_jobs_without_host_paths() -> None:
    bare = planned(JobKind.ACTIVATE)
    bare = replace(bare, jobs=(replace(bare.jobs[0], mounts=()),))
    assert at(values_of(bare), "claim", "mount") == "/srv/datadouble"


def values_of(planned_jobs: Pipeline) -> JsonValue:
    (generated,) = ArgoCd().generate(planned_jobs)
    application = _YAML.validate_python(yaml.safe_load(generated.content))
    return at(application, "spec", "source", "helm", "valuesObject")
