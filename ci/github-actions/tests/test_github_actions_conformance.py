# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The CI/CD conformance suite for GitHub Actions, run locally with ``act``.

Jobs run on the host (``act``'s ``-self-hosted`` platform), as on a
self-hosted runner, using the action from this checkout and the Datadouble
image ``DATADOUBLE_TEST_CI_IMAGE``. ``act`` gets a clean environment, so
nothing from the test process, such as its boundary mark, leaks into a job;
each run says what its runner's environment holds. Secrets are shown in
``act``'s output (``--insecure-secrets``), so the suite sees anything
Datadouble itself would print. GitHub Actions is also the system whose
suite runs a build with another Docker command and socket (decision 126).
"""

import json
import os
import re
import shutil
import tempfile
import zipfile
from collections.abc import Generator, Mapping
from pathlib import Path

import pytest
from pydantic import JsonValue

from datadouble import api
from datadouble.ci import JobKind, Side
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
    World,
    ci_init,
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

    @property
    def init_fields(self) -> Mapping[str, str]:
        return {}

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
        """Every file in the artifact server, and the files inside the archives it holds."""
        found: list[Path] = []
        for path in sorted(self.artifacts.rglob("*")):
            if not path.is_file():
                continue
            found.append(path)
            if zipfile.is_zipfile(path):
                with zipfile.ZipFile(path) as archive:
                    found.extend(Path(name) for name in archive.namelist())
        return found

    @property
    def artifact_storage(self) -> bool:
        return True


@pytest.fixture
def ci_harness(tmp_path: Path) -> Generator[CiHarness]:
    work = Path(os.environ.get(WORK_VARIABLE, tempfile.gettempdir()))
    cache = work / f"act-cache-{tmp_path.name}"
    harness = Act(cache, tmp_path / "artifacts", tmp_path / "events")
    yield harness
    shutil.rmtree(cache, ignore_errors=True)


_DEFAULT_SOCKET = Path("/var/run/docker.sock")


def _host_socket() -> Path:
    """The Docker daemon's socket on this machine."""
    host = os.environ.get("DOCKER_HOST", "")
    return Path(host.removeprefix("unix://")) if host.startswith("unix://") else _DEFAULT_SOCKET


class TestGitHubActions(CiConformance):
    """GitHub Actions passes the CI/CD conformance suite."""

    def test_another_docker_command_and_socket(
        self, ci_harness: CiHarness, shop: World, tmp_path: Path
    ) -> None:
        """A build runs with the Docker command and socket ``ci init`` was given."""
        # The daemon must see the socket's path, so it lives where worlds do.
        work = Path(os.environ.get(WORK_VARIABLE, tempfile.gettempdir()))
        tools = work / f"docker-tools-{tmp_path.name}"
        tools.mkdir()
        try:
            socket = tools / "daemon.sock"
            socket.symlink_to(_host_socket())
            calls = tools / "calls"
            command = tools / "my-docker"
            record = f'printf "%s\\n" "$DOCKER_HOST" "$CONTAINER_HOST" "$@" > {calls}'
            command.write_text(f'#!/bin/sh\n{record}\nexec docker "$@"\n', encoding="utf-8")
            command.chmod(0o755)
            repository = shop.repository.parent / "docker-repository"
            shutil.copytree(shop.spec("on-demand").parent, repository / "datadouble")
            made = ci_init(
                shop, ci_harness.system, repository / "datadouble" / "on-demand.yml", JobKind.BUILD,
                runner=shop.runners[Side.PRODUCTION], work=str(shop.base / "work"),
                output=str(repository), docker=str(command), docker_socket=str(socket),
            )  # fmt: skip
            assert isinstance(made, api.CiInitResponse), made
            ci_harness.prepare(repository)
            built = ci_harness.run(repository, "build", Event.MANUAL, shop.context(run_id="501"))
            assert built.state is State.SUCCESS, built.log
            called = calls.read_text(encoding="utf-8").splitlines()
            assert called[:2] == [f"unix://{socket}", f"unix://{socket}"]
            assert called[2] == "run"
            assert f"{socket}:/var/run/docker.sock" in called
            assert "DOCKER_HOST=unix:///var/run/docker.sock" in called
        finally:
            shutil.rmtree(tools, ignore_errors=True)
