# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The CI/CD conformance suite for GitHub Actions, run locally with ``act``.

Jobs run on the host (``act``'s ``-self-hosted`` platform), as on a
self-hosted runner, using the action from this checkout and the Datadouble
image ``DATADOUBLE_TEST_CI_IMAGE``. ``act`` gets a clean environment, so
nothing from the test process, such as its boundary mark, leaks into a job;
each run says what its runner's environment holds. Secrets are shown in
``act``'s output (``--insecure-secrets``), so the suite sees anything
Datadouble itself would print.
"""

import json
import os
import re
import shutil
import tempfile
from collections.abc import Generator, Mapping
from pathlib import Path

import pytest
from pydantic import JsonValue

from datadouble.ci import Side
from datadouble.programs import run_program
from datadouble_conformance.ci import (
    RUNNERS,
    WORK_VARIABLE,
    CiConformance,
    CiHarness,
    Event,
    JobRun,
    RunContext,
    State,
)
from datadouble_github_actions import NAME

pytestmark = pytest.mark.ci_conformance

ACTION = Path(__file__).resolve().parents[1]
_USES = re.compile(r"uses: datadouble/datadouble/ci/github-actions@\S+")
_OUTPUT = re.compile(r"::set-output:: (\w[\w-]*)=(.*)")
_EVENTS = {
    Event.MANUAL: "workflow_dispatch",
    Event.SCHEDULE: "schedule",
    Event.REQUEST_OPENED: "pull_request",
    Event.REQUEST_CLOSED: "pull_request",
}
_REPOSITORY = "example/shop"


def _git(repository: Path, *arguments: str) -> None:
    done = run_program("git", ["-C", str(repository), *arguments])
    assert done.status == 0, done.stderr


class Act:
    """GitHub Actions, through ``act`` on this machine."""

    def __init__(self, cache: Path, artifacts: Path, events: Path) -> None:
        """Keep ``act``'s files under ``cache``, which the Docker daemon can see."""
        self.cache = cache
        self.artifacts = artifacts
        self.events = events

    @property
    def system(self) -> str:
        return NAME

    @property
    def hosted_runner(self) -> str:
        return "ubuntu-latest"

    @property
    def hosted_environment(self) -> Mapping[str, str]:
        return {"RUNNER_ENVIRONMENT": "github-hosted"}

    def prepare(self, repository: Path) -> None:
        local = repository / ".github" / "actions" / "datadouble"
        local.mkdir(parents=True, exist_ok=True)
        for name in ("action.yml", "run.sh"):
            shutil.copy2(ACTION / name, local / name)
        for workflow in (repository / ".github" / "workflows").glob("*.yml"):
            text = workflow.read_text(encoding="utf-8")
            workflow.write_text(_USES.sub("uses: ./.github/actions/datadouble", text), "utf-8")
        _git(repository, "init", "--quiet", "--initial-branch", "main")
        _git(repository, "remote", "add", "origin", f"https://github.com/{_REPOSITORY}.git")
        _git(repository, "add", "--all")
        _git(
            repository,
            "-c", "user.name=Conformance", "-c", "user.email=conformance@example.com",
            "commit", "--quiet", "--message", "Generated jobs",
        )  # fmt: skip

    def _payload(self, event: Event, context: RunContext) -> Path:
        payload: dict[str, object] = {"repository": {"full_name": _REPOSITORY}}
        if event is Event.MANUAL:
            payload["inputs"] = dict(context.inputs)
        if context.request is not None:
            payload["action"] = "opened" if event is Event.REQUEST_OPENED else "closed"
            payload["number"] = context.request
            payload["pull_request"] = {
                "number": context.request,
                "head": {"repo": {"full_name": _REPOSITORY}},
            }
        self.events.mkdir(parents=True, exist_ok=True)
        path = self.events / f"{len(list(self.events.iterdir()))}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def run(self, repository: Path, job: str, event: Event, context: RunContext) -> JobRun:
        (workflow,) = (repository / ".github" / "workflows").glob(f"datadouble-*-{job}.yml")
        clean = [
            f"PATH={os.environ['PATH']}",
            f"HOME={Path.home()}",
            f"XDG_CACHE_HOME={self.cache}",
        ]
        if "DOCKER_HOST" in os.environ:
            clean.append(f"DOCKER_HOST={os.environ['DOCKER_HOST']}")
        arguments = [
            "-i", *clean, "act", _EVENTS[event],
            "--directory", str(repository), "--workflows", str(workflow),
            "--eventpath", str(self._payload(event, context)),
            "--platform", "self-hosted=-self-hosted",
            "--platform", "prod-db=-self-hosted",
            "--platform", "staging=-self-hosted",
            "--artifact-server-path", str(self.artifacts),
            "--insecure-secrets",
            "--env", f"GITHUB_RUN_ID={context.run_id}",
        ]  # fmt: skip
        for name, value in context.environ.items():
            arguments += ["--env", f"{name}={value}"]
        for name, value in context.secrets.items():
            arguments += ["--secret", f"{name}={value}"]
        done = run_program("env", arguments)
        log = done.stdout + done.stderr
        outputs = {match.group(1): match.group(2) for match in _OUTPUT.finditer(log)}
        if done.status != 0:
            state = State.FAILURE
        elif "::warning" in log:
            state = State.WARNING
        else:
            state = State.SUCCESS
        return JobRun(state=state, outputs=outputs, log=log)

    def owner(self, repository: Path, request: int) -> str:
        del repository
        return f"pr-{request}"

    def runtime(self, network: str, base: Path) -> Mapping[str, JsonValue]:
        del base
        return {"docker": {"network": network}}

    def runner(self, side: Side, base: Path) -> str:
        del base
        return RUNNERS[side]

    def uploaded(self) -> list[Path]:
        return sorted(path for path in self.artifacts.rglob("*") if path.is_file())


@pytest.fixture
def ci_harness(tmp_path: Path) -> Generator[CiHarness]:
    work = Path(os.environ.get(WORK_VARIABLE, tempfile.gettempdir()))
    cache = work / f"act-cache-{tmp_path.name}"
    harness = Act(cache, tmp_path / "artifacts", tmp_path / "events")
    yield harness
    shutil.rmtree(cache, ignore_errors=True)


class TestGitHubActions(CiConformance):
    """GitHub Actions passes the CI/CD conformance suite."""
