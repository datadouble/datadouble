# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The CI/CD conformance suite for GitLab CI, against GitLab CE in a container.

The suite starts GitLab CE and a GitLab Runner whose shell executor runs jobs
on the host's network with the host's Docker daemon, as a self-hosted
runner's would. Each repository becomes a GitLab project whose
``.gitlab-ci.yml`` includes the generated files, with the job template
included locally instead of from the release's tag. A run's environment and
secrets are the project's CI/CD variables, which merge request pipelines and
their stop jobs see too. Set ``DATADOUBLE_TEST_GITLAB_URL`` and
``DATADOUBLE_TEST_GITLAB_TOKEN`` to use a running GitLab, with a runner tagged
``prod-db`` and ``staging``, instead.
"""

import json
import os
import re
import secrets
import socket
import tempfile
import time
import uuid
import zipfile
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml
from pydantic import JsonValue, TypeAdapter

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
    docker,
)
from datadouble_gitlab import NAME, TEMPLATE, template

pytestmark = pytest.mark.ci_conformance

GITLAB = (
    "gitlab/gitlab-ce:19.5.3-ce.0"
    "@sha256:6863d156a1a477bd6f69fc674b671d0163cd8b06bf63bb17cc4a4a803b78b27c"
)
RUNNER_CONTEXT = Path(__file__).resolve().parents[3] / "dev" / "ci" / "gitlab"
RUNNER_IMAGE = "datadouble-ci-gitlab-runner:test"
_INCLUDE = re.compile(r"- remote: \S+\n\s+integrity: \S+\n")
_FINAL = frozenset({"success", "failed", "canceled", "skipped", "manual"})
_WORKFLOW = """\
workflow:
  rules:
    - if: $CI_PIPELINE_SOURCE == "push"
      when: never
    - when: always
include:
"""


_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])
_OBJECTS: TypeAdapter[list[dict[str, JsonValue]]] = TypeAdapter(list[dict[str, JsonValue]])


@dataclass
class Server:
    """A GitLab and its token, reached with curl."""

    url: str
    token: str

    def _curl(self, method: str, path: str, *options: str) -> str:
        done = run_program(
            "curl",
            [
                "--silent", "--show-error", "--fail-with-body", "--request", method,
                "--header", f"PRIVATE-TOKEN: {self.token}",
                "--header", "Content-Type: application/json",
                *options, f"{self.url}/api/v4{path}",
            ],
        )  # fmt: skip
        if done.status != 0:
            message = f"{method} {path} failed: {done.stdout.strip()} {done.stderr.strip()}"
            raise RuntimeError(message)
        return done.stdout

    def call(self, method: str, path: str, body: Mapping[str, JsonValue] | None = None) -> None:
        """Call the REST API for its effect."""
        self._curl(method, path, *(["--data", json.dumps(body)] if body is not None else []))

    def one(
        self, method: str, path: str, body: Mapping[str, JsonValue] | None = None
    ) -> dict[str, JsonValue]:
        """Call the REST API for one object."""
        data = ["--data", json.dumps(body)] if body is not None else []
        return _OBJECT.validate_json(self._curl(method, path, *data))

    def many(self, path: str) -> list[dict[str, JsonValue]]:
        """A list of objects."""
        return _OBJECTS.validate_json(self._curl("GET", path))

    def download(self, path: str, target: Path) -> bool:
        """Save a resource, such as a job's log or artifacts; False if there is none."""
        try:
            self._curl("GET", path, "--output", str(target))
        except RuntimeError:
            return False
        return True

    def text(self, path: str) -> str:
        """A plain-text resource; empty if there is none."""
        try:
            return self._curl("GET", path)
        except RuntimeError:
            return ""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def _wait(check: Callable[[], bool], what: str, seconds: int) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            message = f"timed out waiting for {what}"
            raise TimeoutError(message)
        time.sleep(5)


def _signs_in(url: str) -> bool:
    done = run_program(
        "curl", ["--silent", "--fail", "--output", "/dev/null", f"{url}/users/sign_in"]
    )
    return done.status == 0


def _start(name: str, url: str, port: int) -> Server:
    password = secrets.token_hex(16) + "Aa1!"
    config = (
        f"external_url '{url}'; prometheus_monitoring['enable'] = false;"
        " puma['worker_processes'] = 0; sidekiq['concurrency'] = 5;"
        " gitlab_rails['initial_root_password'] = ENV['GITLAB_ROOT_PASSWORD']"
    )
    docker(
        "run", "--detach", "--name", name, "--shm-size", "256m",
        "--publish", f"127.0.0.1:{port}:{port}",
        "--env", "GITLAB_ROOT_PASSWORD", "--env", f"GITLAB_OMNIBUS_CONFIG={config}", GITLAB,
        env={"GITLAB_ROOT_PASSWORD": password},
    )  # fmt: skip
    _wait(lambda: _signs_in(url), "GitLab to start", 1200)
    token = "glpat-" + secrets.token_hex(16)
    script = (
        "t = User.find_by_username('root').personal_access_tokens.create("
        "scopes: ['api', 'create_runner', 'write_repository'], name: 'conformance',"
        " expires_at: 2.days.from_now); t.set_token(ENV['DDCI_TOKEN']); t.save!"
    )
    docker(
        "exec", "--env", "DDCI_TOKEN", name, "gitlab-rails", "runner", script,
        env={"DDCI_TOKEN": token},
    )  # fmt: skip
    return Server(url, token)


def _start_runner(server: Server, name: str, work: Path) -> None:
    """A shell-executor runner on the host's network, seeing ``work`` at the same path."""
    builds = work / f"{name}-builds"
    created = server.one(
        "POST", "/user/runners",
        {"runner_type": "instance_type", "tag_list": ",".join(RUNNERS.values())},
    )  # fmt: skip
    docker("build", "--quiet", "--tag", RUNNER_IMAGE, str(RUNNER_CONTEXT))
    docker(
        "run", "--rm", "--volume", f"{work}:{work}", "--entrypoint", "install",
        "--user", "0:0", RUNNER_IMAGE, "-d", "-m", "0777", str(builds),
    )  # fmt: skip
    register = (
        'gitlab-runner register --non-interactive --url "$DDCI_URL" --token "$DDCI_RUNNER"'
        f" --executor shell --builds-dir {builds} && exec gitlab-runner run --user root"
    )
    docker(
        "run", "--detach", "--name", f"{name}-runner", "--network", "host",
        "--volume", "/var/run/docker.sock:/var/run/docker.sock",
        "--volume", f"{work}:{work}",
        "--env", "DDCI_URL", "--env", "DDCI_RUNNER", "--entrypoint", "sh",
        RUNNER_IMAGE, "-c", register,
        env={"DDCI_URL": server.url, "DDCI_RUNNER": str(created["token"])},
    )  # fmt: skip


@pytest.fixture(scope="session")
def gitlab() -> Generator[Server]:
    """GitLab CE and a runner tagged with the suite's runners, or a running GitLab."""
    url = os.environ.get("DATADOUBLE_TEST_GITLAB_URL")
    if url:
        yield Server(url, os.environ["DATADOUBLE_TEST_GITLAB_TOKEN"])
        return
    name = f"ddci-gitlab-{uuid.uuid4().hex[:8]}"
    port = _free_port()
    work = Path(os.environ.get(WORK_VARIABLE, tempfile.gettempdir()))
    try:
        server = _start(name, f"http://127.0.0.1:{port}", port)
        _start_runner(server, name, work)
        yield server
    finally:
        run_program("docker", ["rm", "--force", "--volumes", f"{name}-runner", name])
        run_program(
            "docker",
            [
                "run", "--rm", "--volume", f"{work}:{work}", "--entrypoint", "rm",
                "--user", "0:0", RUNNER_IMAGE, "-rf", str(work / f"{name}-builds"),
            ],
        )  # fmt: skip


@dataclass
class _Project:
    id: int
    spec: str
    requests: dict[int, int] = field(default_factory=dict[int, int])
    """Merge request number → its internal ID."""
    failed: dict[tuple[str, str], int] = field(default_factory=dict[tuple[str, str], int])
    """(job, run ID) → the pipeline of a failed run, which a later run with the ID retries."""

    @property
    def path(self) -> str:
        return f"/projects/{self.id}"


def _git(repository: Path, *arguments: str) -> str:
    done = run_program("git", ["-C", str(repository), *arguments])
    assert done.status == 0, done.stderr
    return done.stdout


def _push(repository: Path) -> None:
    """Commit and push what changed, such as a spec a test rewrote: GitLab runs main."""
    _git(repository, "add", "--all")
    if _git(repository, "status", "--porcelain"):
        _git(
            repository,
            "-c", "user.name=Conformance", "-c", "user.email=conformance@example.com",
            "commit", "--quiet", "--message", "Change the jobs",
        )  # fmt: skip
        _git(repository, "push", "--quiet", "origin", "main")


def _state(jobs: list[dict[str, JsonValue]]) -> State:
    """A job allowed to fail (exit code 3) is GitLab's warning state."""
    statuses = {str(job["status"]) for job in jobs}
    failed = [job for job in jobs if job["status"] == "failed"]
    if failed and all(job["allow_failure"] for job in failed):
        return State.WARNING
    return State.SUCCESS if statuses <= {"success"} else State.FAILURE


def _latest(jobs: list[dict[str, JsonValue]]) -> list[dict[str, JsonValue]]:
    """The newest attempt at each job, after retries."""
    newest: dict[str, dict[str, JsonValue]] = {}
    for job in sorted(jobs, key=lambda j: int(str(j["id"]))):
        newest[str(job["name"])] = job
    return list(newest.values())


class GitLabCe:
    """GitLab CI, through GitLab CE's API."""

    def __init__(self, server: Server, scratch: Path) -> None:
        """Drive ``server``; download artifacts into ``scratch``."""
        self.server = server
        self.scratch = scratch
        self.projects: dict[Path, _Project] = {}

    @property
    def system(self) -> str:
        return NAME

    @property
    def hosted_runner(self) -> str:
        return "saas-linux-small-amd64"

    @property
    def hosted_environment(self) -> Mapping[str, str]:
        return {"CI_RUNNER_TAGS": '["saas-linux-small-amd64"]'}

    def prepare(self, repository: Path) -> None:
        generated = sorted((repository / ".gitlab").glob("datadouble-*.yml"))
        (repository / ".gitlab" / TEMPLATE).write_bytes(template())
        for path in generated:
            text = _INCLUDE.sub(f"- local: .gitlab/{TEMPLATE}\n", path.read_text("utf-8"))
            path.write_text(text, encoding="utf-8")
        includes = "".join(f"  - local: .gitlab/{path.name}\n" for path in generated)
        (repository / ".gitlab-ci.yml").write_text(_WORKFLOW + includes, encoding="utf-8")
        spec = re.match(r"datadouble-(.+?)-(?:non-)?production\.yml", generated[0].name)
        assert spec is not None
        name = f"shop-{uuid.uuid4().hex[:8]}"
        created = self.server.one("POST", "/projects", {"name": name, "visibility": "private"})
        self.projects[repository] = _Project(int(str(created["id"])), spec.group(1))
        remote = self.server.url.replace("://", f"://root:{self.server.token}@")
        _git(repository, "init", "--quiet", "--initial-branch", "main")
        _git(repository, "remote", "add", "origin", f"{remote}/root/{name}.git")
        _push(repository)

    def _variables(self, project: _Project, values: Mapping[str, str]) -> None:
        """Make the project's CI/CD variables exactly ``values``."""
        existing = {str(v["key"]) for v in self.server.many(f"{project.path}/variables")}
        for key in existing - set(values):
            self.server.call("DELETE", f"{project.path}/variables/{key}")
        for key, value in values.items():
            body: dict[str, JsonValue] = {"key": key, "value": value, "raw": True}
            if key in existing:
                self.server.call("PUT", f"{project.path}/variables/{key}", body)
            else:
                self.server.call("POST", f"{project.path}/variables", body)

    def _jobs(self, project: _Project, pipeline: int, prefix: str) -> list[dict[str, JsonValue]]:
        jobs = self.server.many(f"{project.path}/pipelines/{pipeline}/jobs?per_page=100")
        pattern = re.compile(rf"{re.escape(prefix)}(-\d+)?")
        return [job for job in jobs if pattern.fullmatch(str(job["name"]))]

    def _newest(self, path: str, what: str) -> int:
        """The first pipeline a list holds, once it holds one."""
        _wait(lambda: bool(self.server.many(path)), what, 120)
        return int(str(self.server.many(path)[0]["id"]))

    def _pipeline(self, project: _Project, event: Event, job: str, context: RunContext) -> int:
        selector = f"{project.spec}-{job}"
        variables: list[JsonValue] = [{"key": "DATADOUBLE_JOB", "value": selector}]
        if "generation" in context.inputs:
            generation = context.inputs["generation"]
            variables.append({"key": "DATADOUBLE_GENERATION", "value": generation})
        if event is Event.SCHEDULE:
            schedule = self.server.one(
                "POST", f"{project.path}/pipeline_schedules",
                {"description": selector, "ref": "main", "cron": "0 0 1 1 *", "active": False},
            )  # fmt: skip
            path = f"{project.path}/pipeline_schedules/{schedule['id']}"
            for variable in variables:
                assert isinstance(variable, dict)
                self.server.call("POST", f"{path}/variables", variable)
            self.server.call("POST", f"{path}/play")
            return self._newest(f"{path}/pipelines", "the scheduled pipeline")
        body: dict[str, JsonValue] = {"ref": "main", "variables": variables}
        return int(str(self.server.one("POST", f"{project.path}/pipeline", body)["id"]))

    def _request(self, project: _Project, event: Event, number: int) -> int:
        """Open a merge request, or close it, and return its pipeline."""
        if event is Event.REQUEST_OPENED:
            branch = f"request-{number}"
            # A request needs a change for GitLab to give it a merge request pipeline.
            change: dict[str, JsonValue] = {
                "branch": branch,
                "start_branch": "main",
                "commit_message": f"Request {number}",
                "actions": [
                    {"action": "create", "file_path": f"request-{number}.txt", "content": "x\n"}
                ],
            }
            self.server.call("POST", f"{project.path}/repository/commits", change)
            body: dict[str, JsonValue] = {
                "source_branch": branch,
                "target_branch": "main",
                "title": f"Request {number}",
            }
            opened = self.server.one("POST", f"{project.path}/merge_requests", body)
            project.requests[number] = int(str(opened["iid"]))
        path = f"{project.path}/merge_requests/{project.requests[number]}"
        pipeline = self._newest(f"{path}/pipelines", "the merge request pipeline")
        if event is Event.REQUEST_CLOSED:
            self.server.call("PUT", path, {"state_event": "close"})
        return pipeline

    def _settled(self, project: _Project, pipeline: int, prefix: str, waiting: set[str]) -> bool:
        """Whether the run's jobs are done; a retry first leaves ``failed`` behind."""
        statuses = {str(j["status"]) for j in _latest(self._jobs(project, pipeline, prefix))}
        if "failed" in waiting and "failed" not in statuses:
            waiting.discard("failed")
        return bool(statuses) and statuses <= _FINAL and not statuses & waiting

    def run(self, repository: Path, job: str, event: Event, context: RunContext) -> JobRun:
        project = self.projects[repository]
        _push(repository)
        self._variables(project, {**context.environ, **context.secrets})
        prefix = f"datadouble-{project.spec}-{job}"
        retried = project.failed.pop((job, context.run_id), None)
        if retried is not None:
            for failed in self._jobs(project, retried, prefix):
                if failed["status"] == "failed":
                    self.server.call("POST", f"{project.path}/jobs/{failed['id']}/retry")
            pipeline = retried
        elif context.request is not None:
            pipeline = self._request(project, event, context.request)
        else:
            pipeline = self._pipeline(project, event, job, context)
        # A stop job waits as `manual` until closing the request starts it.
        waiting = {"manual"} if event is Event.REQUEST_CLOSED else set[str]()
        if retried is not None:
            waiting.add("failed")
        _wait(lambda: self._settled(project, pipeline, prefix, waiting), prefix, 1800)
        jobs = _latest(self._jobs(project, pipeline, prefix))
        log = "\n".join(self.server.text(f"{project.path}/jobs/{j['id']}/trace") for j in jobs)
        outputs: dict[str, str] = {}
        for found in jobs:
            dotenv = self.server.text(f"{project.path}/jobs/{found['id']}/artifacts/datadouble.env")
            for line in dotenv.splitlines():
                key, _, value = line.partition("=")
                outputs[key.removeprefix("DATADOUBLE_").lower()] = value
        state = _state(jobs)
        if state is State.FAILURE:
            project.failed[(job, context.run_id)] = pipeline
        return JobRun(state=state, outputs=outputs, log=log)

    def owner(self, repository: Path, request: int) -> str:
        return f"mr-{self.projects[repository].requests[request]}"

    def runtime(self, network: str, base: Path) -> Mapping[str, JsonValue]:
        del base
        return {"docker": {"network": network}}

    def runner(self, side: Side, base: Path) -> str:
        del base
        return RUNNERS[side]

    def uploaded(self) -> list[Path]:
        """Every file in jobs' artifacts except the outputs (``datadouble.env``)."""
        found: list[Path] = []
        for project in self.projects.values():
            for job in self.server.many(f"{project.path}/jobs?per_page=100"):
                artifacts = _OBJECTS.validate_python(job.get("artifacts", []))
                kinds = {str(artifact["file_type"]) for artifact in artifacts}
                found.extend(
                    Path(kind) for kind in kinds - {"trace", "dotenv", "archive", "metadata"}
                )
                archive = self.scratch / f"{project.id}-{job['id']}.zip"
                if "archive" in kinds and self.server.download(
                    f"{project.path}/jobs/{job['id']}/artifacts", archive
                ):
                    with zipfile.ZipFile(archive) as unpacked:
                        names = set(unpacked.namelist()) - {"datadouble.env"}
                    found.extend(Path(name) for name in sorted(names))
        return found

    @property
    def artifact_storage(self) -> bool:
        return True


@pytest.fixture
def ci_harness(gitlab: Server, tmp_path: Path) -> CiHarness:
    return GitLabCe(gitlab, tmp_path)


GOLDEN = Path(__file__).parent / "golden"


def test_golden_files_pass_ci_lint(gitlab: Server) -> None:
    """GitLab's CI Lint accepts every golden file, with the template it includes."""
    lint = gitlab.one("POST", "/projects", {"name": f"lint-{uuid.uuid4().hex[:8]}"})
    included = _OBJECT.validate_python(yaml.safe_load(template()))
    files = sorted(GOLDEN.glob("*/*.golden"))
    assert files
    for golden in files:
        config = _OBJECT.validate_python(yaml.safe_load(golden.read_text("utf-8")))
        del config["include"]
        content = yaml.safe_dump({**included, **config}, sort_keys=False)
        result = gitlab.one("POST", f"/projects/{lint['id']}/ci/lint", {"content": content})
        assert result["valid"] is True, (golden.name, result["errors"])


class TestGitLab(CiConformance):
    """GitLab CI passes the CI/CD conformance suite."""
