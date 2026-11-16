# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Workflows the GitHub Actions adapter writes, and how it reports a response.

The workflows are compared with the golden files under ``golden/``, which
``make lint-workflows`` also checks with actionlint. To accept a deliberate
change, run the tests with ``DATADOUBLE_UPDATE_GOLDEN=1`` and review the diff.
"""

import io
import os
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from datadouble.ci import CiError, JobKind, Pipeline, PipelineOptions, Report, Side, pipeline
from datadouble.outcome import ExitCode, Outcome
from datadouble.spec import Spec, parse_spec
from datadouble_github_actions import NAME, GitHubActions, adapter, render

GOLDEN = Path(__file__).parent / "golden"

SPEC = """
name: shop
source:
  engine: postgres
  connection: {dsn: {env: SHOP_SOURCE_DSN}}
secrets: {anonymization_key: {env: SHOP_ANONYMIZATION_KEY}}
store: [{filesystem: {path: /srv/datadouble/store}}]
signing: {cosign: {keyless: true}}
trust: {cosign: {identity: "https://github.com/example/shop/.github/workflows/x.yml@refs/heads/main",
                 issuer: "https://token.actions.githubusercontent.com"}}
promote: {to: [{s3: {bucket: shop-export, prefix: images}}]}
fetch:
  from: {s3: {bucket: shop-export, prefix: images}}
  staging: /srv/datadouble/staging
replicate: {using: {docker-volume: {}}, mode: MODE, size: 1}
announce: [{ci-outputs: {}}]
"""


def spec(mode: str = "on-demand") -> Spec:
    return parse_spec(SPEC.replace("MODE", mode))


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
    return pipeline(spec(mode), options)


def github() -> tuple[GitHubActions, io.StringIO]:
    stream = io.StringIO()
    return GitHubActions({}, stream), stream


SCENARIOS = {
    "production": planned(JobKind.BUILD, JobKind.PROMOTE, JobKind.PRUNE),
    "on-demand": planned(
        JobKind.FETCH,
        JobKind.PROVISION,
        JobKind.RELEASE,
        JobKind.PRUNE,
        JobKind.ACTIVATE,
        runner="staging,linux",
    ),
    "pool": planned(JobKind.FETCH, JobKind.PROVISION, mode="pool", runner=None),
}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_workflows_match_the_golden_files(scenario: str) -> None:
    ci, _ = github()
    directory = GOLDEN / scenario
    generated = {
        f"{file.path.name}.golden": file.content for file in ci.generate(SCENARIOS[scenario])
    }
    if os.environ.get("DATADOUBLE_UPDATE_GOLDEN"):
        directory.mkdir(parents=True, exist_ok=True)
        for old in directory.glob("*.golden"):
            old.unlink()
        for name, content in generated.items():
            (directory / name).write_text(content, encoding="utf-8")
    on_disk = {path.name: path.read_text(encoding="utf-8") for path in directory.glob("*.golden")}
    assert generated == on_disk
    for file in ci.generate(SCENARIOS[scenario]):
        assert str(file.path).startswith(".github/workflows/datadouble-shop-")


def test_workflows_say_what_they_do() -> None:
    ci, _ = github()
    files = {
        file.path.name: yaml.safe_load(file.content)
        for file in ci.generate(SCENARIOS["production"])
    }
    build = files["datadouble-shop-build.yml"]
    assert build["permissions"] == {"contents": "read", "id-token": "write"}
    (job,) = build["jobs"].values()
    assert job["runs-on"] == ["self-hosted", "prod-db"]
    assert job["timeout-minutes"] == 720
    step = job["steps"][1]
    assert step["uses"] == "datadouble/datadouble/ci/github-actions@v1.2.3"
    assert step["env"] == {
        "SHOP_ANONYMIZATION_KEY": "${{ secrets.SHOP_ANONYMIZATION_KEY }}",
        "SHOP_SOURCE_DSN": "${{ secrets.SHOP_SOURCE_DSN }}",
    }
    assert step["with"]["arguments"].splitlines()[-2:] == ["--retry-key", "${{ github.run_id }}"]
    promote = files["datadouble-shop-promote.yml"]
    assert promote[True]["workflow_run"]["workflows"] == ["Datadouble shop build"]
    prune = files["datadouble-shop-prune-production.yml"]
    assert "env" not in prune["jobs"]["prune"]["steps"][1]


def test_hosted_runners_are_refused() -> None:
    ci, _ = github()
    for runner in ("ubuntu-latest", "self-hosted,windows-2025"):
        with pytest.raises(CiError, match="is a GitHub-hosted runner"):
            ci.generate(planned(JobKind.FETCH, runner=runner))
    pool = ci.generate(planned(JobKind.PRUNE, runner="self-hosted", side=Side.NON_PRODUCTION))
    assert "runs-on: self-hosted\n" in pool[0].content


def test_vendor_hosted() -> None:
    ci, _ = github()
    assert ci.vendor_hosted({"RUNNER_ENVIRONMENT": "github-hosted"}) == "a GitHub-hosted runner"
    assert ci.vendor_hosted({"RUNNER_ENVIRONMENT": "self-hosted"}) is None
    assert ci.vendor_hosted({}) is None


def test_masks() -> None:
    ci, stream = github()
    ci.mask("one\n\ntwo")
    assert stream.getvalue() == "::add-mask::one\n::add-mask::two\n"


def report(outcome: Outcome, **changes: object) -> Report:
    fields: dict[str, object] = {
        "command": "run",
        "outcome": outcome,
        "outputs": {"outcome": str(outcome), "generation": "0193a7c21b4e9f30"},
        "summary": "generation 0193a7c21b4e9f30 (success)",
    }
    fields.update(changes)
    return Report.model_validate(fields)


def test_reports_into_a_directory(tmp_path: Path) -> None:
    ci, stream = github()
    code = ci.report(report(Outcome.WARNING, warnings=("100% sure\nor not",)), tmp_path)
    assert code is ExitCode.SUCCESS
    lines = (tmp_path / "outputs").read_text().splitlines()
    assert lines[0].startswith("outcome<<datadouble_")
    assert lines[1:3] == ["warning", lines[0].split("<<")[1]]
    assert lines[4:6] == ["0193a7c21b4e9f30", lines[3].split("<<")[1]]
    assert (tmp_path / "summary.md").read_text() == (
        "### Datadouble run: warning\n\n```text\ngeneration 0193a7c21b4e9f30 (success)\n```\n"
    )
    assert stream.getvalue() == "::warning title=Datadouble::100%25 sure%0Aor not\n"


def test_reports_to_the_runner(tmp_path: Path) -> None:
    stream = io.StringIO()
    environ = {
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
    }
    ci = GitHubActions(environ, stream)
    failed = report(Outcome.FAILURE, command="error", outputs={"error": "run_failed"}, message="no")
    assert ci.report(failed, None) is ExitCode.FAILURE
    assert "error<<" in (tmp_path / "output").read_text()
    assert (tmp_path / "summary").read_text().startswith("### Datadouble error: failure")
    assert stream.getvalue() == "::error title=Datadouble::no\n"
    quiet, stream = github()
    assert quiet.report(report(Outcome.SUCCESS), None) is ExitCode.SUCCESS
    assert stream.getvalue() == ""


def test_the_entry_point() -> None:
    assert adapter().name == NAME == "github-actions"


def test_jobs_without_host_paths() -> None:
    planned = SCENARIOS["pool"]
    fetch = replace(planned.jobs[0], mounts=())
    workflow = yaml.safe_load(render(planned, fetch))
    (step,) = workflow["jobs"]["fetch"]["steps"][1:]
    assert "mounts" not in step["with"]


def test_workflows_need_no_other_setup() -> None:
    ci, _ = github()
    assert ci.instructions(SCENARIOS["production"]) == ()
