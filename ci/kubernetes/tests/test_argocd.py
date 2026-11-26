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
from pathlib import Path, PurePosixPath

import pytest
import yaml
from pydantic import JsonValue, TypeAdapter

from datadouble.ci import (
    Agent,
    CiError,
    GitSettings,
    GitSource,
    JobKind,
    MountedSettings,
    MountedSource,
    Pipeline,
    PipelineOptions,
    Report,
    Side,
    SpecSource,
    SpecSourceKind,
    pipeline,
    spec_path,
)
from datadouble.outcome import ExitCode, Outcome
from datadouble.spec import parse_spec
from datadouble_argocd import NAME, ArgoCd, adapter

GOLDEN = Path(__file__).parent / "golden"

SPEC = """
name: shop
sources:
  app:
    engine: postgres
    connection:
      dsn:
        env: SHOP_SOURCE_DSN
runtime:
  kubernetes:
    claim: datadouble
    mount: /srv/datadouble
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
    - filesystem:
        path: /srv/datadouble/export
fetch:
  from:
    filesystem:
      path: /srv/datadouble/export
  staging: /srv/datadouble/staging
notify:
  when: [failure, warning]
  to:
    - webhook:
        url:
          env: ALERTS_WEBHOOK
replicate:
  using:
    k8s-pv:
      path: /srv/datadouble/copies
  mode: MODE
  size: 1
"""
NOTIFY = """notify:
  when: [failure, warning]
  to:
    - webhook:
        url:
          env: ALERTS_WEBHOOK
"""
WITHOUT_NOTIFY = SPEC.replace(NOTIFY, "")
_YAML: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


def planned(
    *kinds: JobKind,
    mode: str = "on-demand",
    runner: str | None = "shop",
    side: Side | None = None,
    spec: str = SPEC,
    source: SpecSource = SpecSourceKind.CHECKOUT,
) -> Pipeline:
    text = spec.replace("MODE", mode)
    options = PipelineOptions(
        kinds=kinds,
        spec_path=spec_path(source, Path("datadouble/shop.yml"), Path()),
        spec_source=source,
        agent=Agent.KUBERNETES,
        image="ghcr.io/datadouble/datadouble:v1.2.3",
        version="1.2.3",
        owner="999:999",
        runner=runner,
        work=Path("/srv/datadouble/work"),
        side=side,
        spec_files=(("shop.yml", text),),
        lock_text="{}\n",
    )
    return pipeline(parse_spec(text), options)


CONFIG_MAP = MountedSource(
    mounted=MountedSettings(path=PurePosixPath("/etc/datadouble-spec"), config_map="shop-spec")
)
ON_DEMAND = (JobKind.FETCH, JobKind.PROVISION, JobKind.RELEASE, JobKind.PRUNE, JobKind.ACTIVATE)
SCENARIOS = {
    "production": planned(JobKind.BUILD, JobKind.PROMOTE, JobKind.PRUNE, JobKind.LOCK),
    "on-demand": planned(*ON_DEMAND),
    "pool": planned(JobKind.FETCH, JobKind.PROVISION, JobKind.RELEASE, mode="pool"),
    "mounted": planned(*ON_DEMAND, source=CONFIG_MAP),
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
    named = SPEC.replace(
        "    claim: datadouble\n", "    namespace: shop-staging\n    claim: datadouble\n"
    )
    with pytest.raises(CiError, match=r"namespace is shop-staging, but the production side's"):
        ArgoCd().generate(planned(JobKind.BUILD, spec=named))
    assert ArgoCd().generate(planned(JobKind.BUILD, spec=named, runner="shop-staging"))
    assert ArgoCd().generate(planned(JobKind.PROMOTE, spec=named))
    clouded = replace(planned(JobKind.FETCH), cloud="prod-k8s")
    with pytest.raises(CiError, match="a cloud is for Jenkins"):
        ArgoCd().generate(clouded)


def test_instructions() -> None:
    argocd = ArgoCd()
    production = argocd.instructions(SCENARIOS["production"])
    assert production[0] == (
        "in the namespace shop, create the PersistentVolumeClaim datadouble, holding"
        " /srv/datadouble, owned by 999:999"
    )
    assert any(step.startswith("inside the production boundary only") for step in production)
    assert production[-1].startswith("Datadouble posts notify's webhooks itself")
    assert "kube_job_status_failed" in production[-1]
    requests = argocd.instructions(SCENARIOS["on-demand"])
    assert requests[-2].startswith("for on-demand copies, make an ApplicationSet")
    quiet = planned(JobKind.PRUNE, side=Side.NON_PRODUCTION, spec=WITHOUT_NOTIFY)
    assert not any("Secret" in step for step in argocd.instructions(quiet))


def test_email_is_refused() -> None:
    mailed = SPEC.replace(
        "    - webhook:\n        url:\n          env: ALERTS_WEBHOOK\n",
        "    - email:\n        to: [data-team@example.com]\n",
    )
    with pytest.raises(CiError, match="argocd cannot send notify's email"):
        ArgoCd().generate(planned(JobKind.FETCH, spec=mailed))


def test_steps_name_their_job() -> None:
    application = yaml.safe_load(ArgoCd().generate(SCENARIOS["production"])[0].content)
    jobs = application["spec"]["source"]["helm"]["valuesObject"]["jobs"]
    build = next(job for job in jobs if job["name"] == "build")
    assert [step["job"] for step in build["steps"]] == ["build", "promote"]
    assert "ALERTS_WEBHOOK" in build["steps"][1]["environment"]


def test_run_time(tmp_path: Path) -> None:
    log = tmp_path / "termination-log"
    argocd = ArgoCd(log)
    assert argocd.vendor_hosted({"RUNNER_ENVIRONMENT": "github-hosted"}) is None
    argocd.mask("nothing happens")
    assert argocd.run_url({"BUILD_URL": "https://example.com"}) is None
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
    lock = tmp_path / "shop.lock"
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
    assert adapter().agents == (Agent.KUBERNETES,)


def test_jobs_without_host_paths() -> None:
    bare = planned(JobKind.ACTIVATE)
    bare = replace(bare, jobs=(replace(bare.jobs[0], mounts=()),))
    assert at(values_of(bare), "claim", "mount") == "/srv/datadouble"


def values_of(planned_jobs: Pipeline) -> JsonValue:
    (generated,) = ArgoCd().generate(planned_jobs)
    application = _YAML.validate_python(yaml.safe_load(generated.content))
    return at(application, "spec", "source", "helm", "valuesObject")


def test_pods_run_without_docker() -> None:
    production = SCENARIOS["production"]
    for changed in (replace(production, docker="podman"), replace(production, docker_socket="/s")):
        with pytest.raises(CiError, match="without Docker"):
            ArgoCd().generate(changed)


def test_values_carry_every_file_of_an_extending_spec() -> None:
    files = (("shop.yml", "name: shop\n"), ("shop-micro.yml", "extends: shop.yml\n"))
    micro = replace(SCENARIOS["production"], spec="datadouble/shop-micro.yml", spec_files=files)
    assert at(values_of(micro), "spec") == {
        "path": "datadouble/shop-micro.yml",
        "files": {"shop.yml": "name: shop\n", "shop-micro.yml": "extends: shop.yml\n"},
        "lock": "{}\n",
    }


def test_a_mounted_spec_directory() -> None:
    assert at(values("mounted"), "spec") == {
        "path": "/etc/datadouble-spec/shop.yml",
        "configMap": "shop-spec",
    }
    steps = ArgoCd().instructions(SCENARIOS["mounted"])
    assert steps[1].startswith("in shop, keep the ConfigMap shop-spec holding the spec's directory")
    unmapped = MountedSource(mounted=MountedSettings(path=PurePosixPath("/etc/datadouble-spec")))
    with pytest.raises(CiError, match="name the ConfigMap"):
        planned(JobKind.FETCH, source=unmapped)


def test_a_spec_from_another_repository_is_refused() -> None:
    git = GitSource(
        git=GitSettings(url="https://git.example.com/data/specs", ref="main", credential="T")
    )
    with pytest.raises(CiError, match=r"use the spec source checkout .* or mounted"):
        ArgoCd().generate(planned(JobKind.FETCH, source=git))
