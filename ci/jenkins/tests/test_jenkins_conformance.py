# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The CI/CD conformance suite for Jenkins, against Jenkins in a container.

The suite starts Jenkins (``dev/ci/jenkins``) on the host's network, whose
built-in node runs jobs with the host's Docker daemon and its workspaces on
a host path, as a self-hosted agent's would. Each generated Jenkinsfile
becomes a Pipeline job of its name, checked out from a copy of the
repository; the shared library comes from a copy of ``ci/jenkins`` tagged
as the release the Jenkinsfiles name. A run's environment is the global
environment of the node, and its secrets are credentials, set through the
script console before each build.
"""

import base64
import json
import os
import re
import secrets
import shutil
import socket
import tempfile
import time
import uuid
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

import pytest
from pydantic import JsonValue, TypeAdapter

from datadouble import __version__
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
from datadouble_jenkins import NAME, REPOSITORY

pytestmark = pytest.mark.ci_conformance

CONTEXT = Path(__file__).resolve().parents[3] / "dev" / "ci" / "jenkins"
LIBRARY = Path(__file__).resolve().parents[1]
IMAGE = "datadouble-ci-jenkins:test"
_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])
_PARAMETER = re.compile(r"(string|booleanParam)\(name: '(\w+)'")
_RESULTS = {"SUCCESS": State.SUCCESS, "UNSTABLE": State.WARNING}


def _git(directory: Path, *arguments: str) -> str:
    done = run_program("git", ["-C", str(directory), *arguments])
    assert done.status == 0, done.stderr
    return done.stdout


def _commit(directory: Path, message: str) -> None:
    _git(directory, "add", "--all")
    if _git(directory, "status", "--porcelain"):
        _git(
            directory,
            "-c", "user.name=Conformance", "-c", "user.email=conformance@example.com",
            "commit", "--quiet", "--message", message,
        )  # fmt: skip


def _encoded(values: Mapping[str, str]) -> str:
    """Values as base64 JSON, which a Groovy string carries unchanged."""
    return base64.b64encode(json.dumps(dict(values)).encode()).decode()


def parse_outputs(text: str) -> dict[str, str]:
    """The step's outputs file: one ``name=value`` per line."""
    pairs = (line.partition("=") for line in text.splitlines())
    return {key: value for key, _, value in pairs}


def _wait(check: Callable[[], bool], what: str, seconds: int) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            message = f"timed out waiting for {what}"
            raise TimeoutError(message)
        time.sleep(3)


@dataclass
class Server:
    """Jenkins, its administrator's password, and a session for CSRF crumbs."""

    url: str
    password: str
    jar: Path
    work: Path
    """Where workspaces and the suite's git repositories live, at the same path in Jenkins."""
    container: str
    """Jenkins' container."""
    crumb: str = ""

    def request(self, *options: str) -> tuple[int, str]:
        done = run_program(
            "curl",
            [
                "--silent", "--show-error", "--user", f"admin:{self.password}",
                "--cookie", str(self.jar), "--cookie-jar", str(self.jar),
                "--write-out", "\\n%{http_code}", *options,
            ],
        )  # fmt: skip
        if done.status != 0:
            return 0, done.stderr  # no connection: Jenkins is not up yet, or has gone
        body, _, code = done.stdout.rpartition("\n")
        return int(code), body

    def get(self, path: str) -> dict[str, JsonValue]:
        """A JSON resource."""
        code, body = self.request(f"{self.url}{path}")
        assert code == 200, (path, code, body[:500])
        return _OBJECT.validate_json(body)

    def text(self, path: str) -> str:
        """A text resource, such as a build's log."""
        return self.request(f"{self.url}{path}")[1]

    def post(self, path: str, *options: str) -> str:
        """POST with the session's crumb; returns the response's headers and body."""
        if not self.crumb:
            issued = self.get("/crumbIssuer/api/json")
            self.crumb = f"{issued['crumbRequestField']}:{issued['crumb']}"
        code, body = self.request(
            "--include", "--header", self.crumb, "--request", "POST", *options, f"{self.url}{path}"
        )
        assert code < 400, (path, code, body[:500])
        return body

    def script(self, groovy: str) -> str:
        """Run Groovy in the script console."""
        return self.post("/scriptText", "--data-urlencode", f"script={groovy}")


def _ready(server: Server) -> bool:
    code, _ = server.request(f"{server.url}/computer/api/json")
    return code == 200


@contextmanager
def start_jenkins(
    tmp_path_factory: pytest.TempPathFactory,
    *,
    address: str = "127.0.0.1",
    docker_socket: bool = True,
    java_options: tuple[str, ...] = (),
) -> Generator[Server]:
    """Jenkins on the host's network, listening on ``address``; removed afterwards.

    With ``docker_socket`` its built-in node runs jobs with the host's Docker
    daemon, as a self-hosted agent with Docker would.
    """
    name = f"ddci-jenkins-{uuid.uuid4().hex[:8]}"
    work = Path(os.environ.get(WORK_VARIABLE, tempfile.gettempdir())) / name
    with socket.socket() as probe:
        probe.bind((address, 0))
        port = int(probe.getsockname()[1])
    password = secrets.token_hex(12)
    docker("build", "--quiet", "--tag", IMAGE, str(CONTEXT))
    docker(
        "run", "--rm", "--volume", f"{work.parent}:{work.parent}", "--entrypoint", "install",
        "--user", "0:0", IMAGE, "-d", "-m", "0777", str(work), str(work / "workspace"),
    )  # fmt: skip
    options = " ".join(
        (
            "-Djenkins.install.runSetupWizard=false",
            "-Dhudson.plugins.git.GitSCM.ALLOW_LOCAL_CHECKOUT=true",
            f"-Djenkins.model.Jenkins.workspacesDir={work}/workspace/${{ITEM_FULL_NAME}}",
            *java_options,
        )
    )
    daemon: tuple[str, ...] = ()
    if docker_socket:
        group = str(Path("/var/run/docker.sock").stat().st_gid)
        daemon = ("--group-add", group, "--volume", "/var/run/docker.sock:/var/run/docker.sock")
    docker(
        "run", "--detach", "--name", name, "--network", "host", *daemon,
        "--volume", f"{work.parent}:{work.parent}",
        "--env", "DDCI_JENKINS_PASSWORD",
        "--env", f"JENKINS_OPTS=--httpPort={port} --httpListenAddress={address}",
        "--env", f"JAVA_OPTS={options}", IMAGE,
        env={"DDCI_JENKINS_PASSWORD": password},
    )  # fmt: skip
    server = Server(
        f"http://{address}:{port}",
        password,
        tmp_path_factory.mktemp("jenkins") / "jar",
        work,
        name,
    )
    try:
        _wait(lambda: _ready(server), "Jenkins to start", 600)
        yield server
    finally:
        run_program("docker", ["rm", "--force", "--volumes", name])
        run_program(
            "docker",
            ["run", "--rm", "--volume", f"{work.parent}:{work.parent}", "--entrypoint", "rm",
             "--user", "0:0", IMAGE, "-rf", str(work)],
        )  # fmt: skip


@pytest.fixture(scope="session")
def jenkins(tmp_path_factory: pytest.TempPathFactory) -> Generator[Server]:
    """Jenkins for the suite, its built-in node running jobs with Docker."""
    with start_jenkins(tmp_path_factory) as server:
        yield server


def _library(server: Server) -> Path:
    """A git repository holding ``ci/jenkins`` at the tag the Jenkinsfiles name."""
    library = server.work / "library"
    if not (library / ".git").exists():
        shutil.copytree(LIBRARY / "vars", library / "ci" / "jenkins" / "vars")
        shutil.copytree(LIBRARY / "resources", library / "ci" / "jenkins" / "resources")
        _git(library, "init", "--quiet", "--initial-branch", "main")
        _commit(library, "The shared library")
        _git(library, "tag", f"v{__version__}")
    return library


@dataclass
class _Job:
    name: str
    parameterized: bool
    failed: dict[str, str] = field(default_factory=dict[str, str])
    """Run ID → the BUILD_TAG of a failed build, which a later run with the ID resumes."""


def _config(remote: str, script: str, parameters: list[tuple[str, str]]) -> str:
    definitions = "".join(
        f"<hudson.model.{'Boolean' if kind == 'booleanParam' else 'String'}ParameterDefinition>"
        f"<name>{name}</name></hudson.model.{'Boolean' if kind == 'booleanParam' else 'String'}"
        "ParameterDefinition>"
        for kind, name in parameters
    )
    properties = (
        f"<hudson.model.ParametersDefinitionProperty><parameterDefinitions>{definitions}"
        "</parameterDefinitions></hudson.model.ParametersDefinitionProperty>"
        if parameters
        else ""
    )
    return (
        "<?xml version='1.1' encoding='UTF-8'?><flow-definition plugin='workflow-job'>"
        f"<properties>{properties}</properties>"
        "<definition class='org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition'>"
        "<scm class='hudson.plugins.git.GitSCM'><configVersion>2</configVersion>"
        f"<userRemoteConfigs><hudson.plugins.git.UserRemoteConfig><url>{escape(remote)}</url>"
        "</hudson.plugins.git.UserRemoteConfig></userRemoteConfigs>"
        "<branches><hudson.plugins.git.BranchSpec><name>*/main</name>"
        "</hudson.plugins.git.BranchSpec></branches></scm>"
        f"<scriptPath>{escape(script)}</scriptPath><lightweight>false</lightweight>"
        "</definition></flow-definition>"
    )


class JenkinsContainer:
    """Jenkins, through its REST API and script console."""

    def __init__(self, server: Server) -> None:
        """Drive ``server``."""
        self.server = server
        self.jobs: dict[tuple[Path, str], _Job] = {}
        self.remotes: dict[Path, Path] = {}
        self.builds: list[str] = []

    @property
    def system(self) -> str:
        return NAME

    @property
    def hosted_runner(self) -> str | None:
        return None

    @property
    def hosted_environment(self) -> Mapping[str, str]:
        # Jenkins agents are never a vendor's, but Datadouble asks every installed
        # adapter, so a GitHub-hosted machine is refused even under Jenkins.
        return {"RUNNER_ENVIRONMENT": "github-hosted"}

    @property
    def init_fields(self) -> Mapping[str, str]:
        return {}

    def remote(self, repository: Path) -> str:
        """The URL jobs check ``repository``, a bare copy, out from."""
        return f"file://{repository}"

    def prepare(self, repository: Path) -> None:
        library = _library(self.server)
        for path in (repository / "jenkins").glob("*.Jenkinsfile"):
            text = path.read_text("utf-8").replace(REPOSITORY, f"file://{library}")
            path.write_text(text, encoding="utf-8")
        remote = self.server.work / "repositories" / uuid.uuid4().hex[:8]
        _git(repository, "init", "--quiet", "--initial-branch", "main")
        _commit(repository, "Generated jobs")
        run_program("git", ["clone", "--quiet", "--bare", str(repository), str(remote)])
        _git(repository, "remote", "add", "origin", str(remote))
        self.remotes[repository] = remote
        for path in sorted((repository / "jenkins").glob("*.Jenkinsfile")):
            stem = path.name.removesuffix(".Jenkinsfile")
            name = f"{stem}-{remote.name}"
            parameters = _PARAMETER.findall(path.read_text("utf-8"))
            config = _config(self.remote(remote), f"jenkins/{path.name}", parameters)
            self.server.post(
                f"/createItem?name={name}", "--header", "Content-Type: application/xml",
                "--data-binary", config,
            )  # fmt: skip
            job = stem.split("-", 2)[2]
            self.jobs[(repository, job)] = _Job(name, bool(parameters))

    def _environment(self, values: Mapping[str, str], secret: Mapping[str, str]) -> None:
        """Set the node's global environment to ``values`` and credentials to ``secret``."""
        groovy = (
            "import jenkins.model.Jenkins\n"
            "import hudson.slaves.EnvironmentVariablesNodeProperty\n"
            "import com.cloudbees.plugins.credentials.*\n"
            "import com.cloudbees.plugins.credentials.domains.Domain\n"
            "import org.jenkinsci.plugins.plaincredentials.impl.StringCredentialsImpl\n"
            "import hudson.util.Secret\n"
            "def decode(String text) {\n"
            "  new groovy.json.JsonSlurper().parseText(\n"
            "    new String(Base64.decoder.decode(text), 'UTF-8'))\n"
            "}\n"
            "def j = Jenkins.get()\n"
            "j.globalNodeProperties.removeAll(EnvironmentVariablesNodeProperty)\n"
            "def p = new EnvironmentVariablesNodeProperty()\n"
            f"p.envVars.putAll(decode('{_encoded(values)}'))\n"
            "j.globalNodeProperties.add(p)\n"
            "def store = SystemCredentialsProvider.getInstance().getStore()\n"
            f"def secrets = decode('{_encoded(secret)}')\n"
            "secrets.each { k, v ->\n"
            "  def old = store.getCredentials(Domain.global()).find { it.id == k }\n"
            "  if (old) { store.removeCredentials(Domain.global(), old) }\n"
            "  store.addCredentials(Domain.global(), new StringCredentialsImpl("
            "CredentialsScope.GLOBAL, k, k, Secret.fromString(v)))\n"
            "}\n"
            "j.save()\n"
            "println 'done'\n"
        )
        assert "done" in self.server.script(groovy)

    def run(self, repository: Path, job: str, event: Event, context: RunContext) -> JobRun:
        _commit(repository, "Change the jobs")
        _git(repository, "push", "--quiet", "origin", "main")
        known = self.jobs[(repository, job)]
        self._environment(context.environ, context.secrets)
        parameters: dict[str, str] = {}
        if "generation" in context.inputs:
            parameters["GENERATION"] = context.inputs["generation"]
        if context.request is not None:
            parameters["OWNER"] = f"pr-{context.request}"
        retried = known.failed.pop(context.run_id, None)
        if retried is not None:
            parameters["RETRY_KEY"] = retried
        del event  # Jenkins starts every job the same way; the parameters differ
        data = [
            option for k, v in parameters.items() for option in ("--data-urlencode", f"{k}={v}")
        ]
        endpoint = "buildWithParameters" if known.parameterized else "build"
        headers = self.server.post(f"/job/{known.name}/{endpoint}", *data)
        location = re.search(r"(?im)^location: \S+/queue/item/(\d+)/", headers)
        assert location is not None, headers
        number = self._started(int(location.group(1)))
        build = f"/job/{known.name}/{number}"
        _wait(lambda: self.server.get(f"{build}/api/json")["building"] is False, build, 1800)
        details = self.server.get(f"{build}/api/json")
        log = self.server.text(f"{build}/consoleText")
        outputs = self.outputs(known.name, number)
        state = _RESULTS.get(str(details["result"]), State.FAILURE)
        if state is State.FAILURE:
            known.failed[context.run_id] = f"jenkins-{known.name}-{number}"
        self.builds.append(build)
        return JobRun(state=state, outputs=outputs, log=log)

    def _started(self, item: int) -> int:
        found: list[int] = []

        def started() -> bool:
            queued = self.server.get(f"/queue/item/{item}/api/json")
            executable = queued.get("executable")
            if isinstance(executable, dict):
                found.append(int(str(executable["number"])))
            return bool(found)

        _wait(started, "the build to start", 600)
        return found[0]

    def outputs(self, name: str, number: int) -> dict[str, str]:
        """A build's outputs, which the step left in its workspace."""
        del number  # the built-in node keeps one workspace per job
        path = self.server.work / "workspace" / name / ".datadouble-outputs"
        if not path.exists():
            return {}
        return parse_outputs(path.read_text("utf-8"))

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
        found: list[Path] = []
        for build in self.builds:
            artifacts = self.server.get(f"{build}/api/json").get("artifacts", [])
            assert isinstance(artifacts, list)
            found.extend(Path(str(a["relativePath"])) for a in artifacts if isinstance(a, dict))
        return found

    @property
    def artifact_storage(self) -> bool:
        return True


GOLDEN = Path(__file__).parent / "golden"


def test_golden_files_pass_the_declarative_validator(jenkins: Server) -> None:
    files = sorted(GOLDEN.glob("*/*.golden"))
    assert files
    for golden in files:
        answer = jenkins.post(
            "/pipeline-model-converter/validate",
            "--data-urlencode", f"jenkinsfile@{golden}",
        )  # fmt: skip
        assert "Jenkinsfile successfully validated." in answer, (golden.name, answer[-500:])


@pytest.fixture
def ci_harness(jenkins: Server) -> CiHarness:
    return JenkinsContainer(jenkins)


class TestJenkins(CiConformance):
    """Jenkins passes the CI/CD conformance suite."""
