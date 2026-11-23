# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The Jenkins CI/CD adapter.

``ci init`` writes a declarative Jenkinsfile per job,
``jenkins/datadouble-<spec>-<job>.Jenkinsfile``, for a Pipeline job of the
same name. Each loads this repository's shared library (``ci/jenkins``) at
the release's tag and calls its ``datadouble`` step once per command line.

Schedules are ``cron`` triggers in UTC, and a job that follows another
(``promote`` after ``build``) has an ``upstream`` trigger that accepts a
build that is unstable, Jenkins' warning state. Jenkins has no events for
pull requests that every installation shares, so on-demand copies take an
``OWNER`` parameter: a request's own pipeline starts ``provision`` and
``release`` with it. A retried build is a new build in Jenkins, so ``build``
takes a ``RETRY_KEY`` parameter: give the failed build's key (its
``BUILD_TAG``, shown in its log) to resume it. Secrets come from credentials
of the same name, which Jenkins masks in logs. The ``lock`` job's new lock
and its changes are archived as the build's artifacts.

The spec's ``notify`` email is Jenkins' ``mail`` step in the Jenkinsfile's
``post`` section, for failed, aborted (timed-out) and, when asked, unstable
builds; a mail that cannot be sent leaves the build's result alone. Its
webhooks Datadouble posts itself, linking ``BUILD_URL``.

Jobs run on agents with Docker by default: the step runs the pinned image
with ``docker run``. With Kubernetes agents (``--agent kubernetes``) each job
is a pod of the Kubernetes plugin instead, beside the engine pods of the
spec's ``kubernetes`` runtime. As for Argo CD, ``--runner`` names the side's
namespace, whose ``datadouble`` claim the pod mounts where the runtime does,
and the engine pods start there too when the runtime names no namespace;
the pinned image is one of its containers, running as the engine's user, and
the step runs ``datadouble`` in it. The production boundary mark is then only
the namespace's ``datadouble-boundary`` ConfigMap, mounted as a file, and
``--cloud`` names the plugin's cloud.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import assert_never

import yaml
from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from datadouble.ci import (
    CLAIM,
    Agent,
    CiError,
    GeneratedFile,
    Invocation,
    Job,
    JobKind,
    Pipeline,
    Placeholder,
    Report,
    Side,
    Trigger,
    claim_mount,
    runtime_namespace,
    sections,
)
from datadouble.outcome import ExitCode, Outcome
from datadouble.spec import NotifyOn

NAME = "jenkins"
REPOSITORY = "https://github.com/datadouble/datadouble.git"
"""Where Jenkinsfiles load the shared library from, at the release's tag."""
LIBRARY_PATH = "ci/jenkins/"
CONTAINER = "datadouble"
"""The Datadouble container of a Kubernetes agent's pod, which the step runs in."""
BOUNDARY = PurePosixPath("/etc/datadouble-boundary")
"""Where the pod mounts the namespace's ``datadouble-boundary`` ConfigMap, the only mark
the step reads on a Kubernetes agent."""
AGENT_COMMAND = 'umask 0000 && exec /usr/local/bin/jenkins-agent "$@"'
"""The plugin's agent container, started with a umask that lets Datadouble's container,
running as another user, write the workspace they share."""


@dataclass(frozen=True)
class Rule:
    """One rule of a Role over Kubernetes' core API group: verbs on resources."""

    resources: tuple[str, ...]
    verbs: tuple[str, ...]

    def describe(self) -> str:
        """The rule in words, such as ``get and list on persistentvolumeclaims``."""
        return f"{_series(self.verbs)} on {_series(self.resources)}"


CLOUD_RULES = (
    Rule(("pods",), ("create", "delete", "get", "list", "patch", "update", "watch")),
    Rule(("pods/exec",), ("create", "get")),
    Rule(("pods/log",), ("get", "list", "watch")),
    Rule(("events",), ("watch",)),
)
"""What the Kubernetes plugin's cloud credentials need in a side's namespace: the Role
the plugin's own documentation gives, with ``pods/exec`` for the ``container`` step."""
ACCOUNT_RULES = (
    Rule(
        ("pods", "pods/log", "pods/exec", "pods/portforward", "secrets"),
        ("get", "list", "watch", "create", "delete", "patch"),
    ),
    Rule(("persistentvolumeclaims",), ("get", "list")),
)
"""What a side's service account needs in its namespace, for the ``kubernetes`` runtime's
engine pods: the Helm chart's Role."""
VOLUME_RULES = (
    Rule(("persistentvolumes",), ("get", "list", "watch", "create", "delete", "patch")),
)
"""What the non-production service account also needs cluster-wide, for ``k8s-pv``."""
_PLACEHOLDERS = {
    Placeholder.RUN: "(params.RETRY_KEY ?: env.BUILD_TAG)",
    Placeholder.OWNER: "params.OWNER",
    Placeholder.GENERATION: "params.GENERATION",
    Placeholder.DRY_RUN: "(params.DRY_RUN ? '--dry-run' : '')",
}
_REQUESTS = frozenset({Trigger.REQUEST_OPENED, Trigger.REQUEST_CLOSED})
_HEADER = """\
// Generated by `datadouble ci init` (Datadouble {version}). Change the spec or
// the options and generate it again, rather than editing it by hand. Run it
// as the Pipeline job {job}.
"""


def _series(words: tuple[str, ...]) -> str:
    """``a``, ``a and b``, ``a, b and c``."""
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} and {words[-1]}"


def _allowing(rules: tuple[Rule, ...]) -> str:
    """Rules in words, separated by semicolons."""
    described = [rule.describe() for rule in rules]
    if len(described) == 1:
        return described[0]
    return f"{'; '.join(described[:-1])}; and {described[-1]}"


def _quote(text: str) -> str:
    """A single-quoted Groovy string."""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _list(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_quote(value) for value in values) + "]"


def _stem(pipeline: Pipeline, kind: JobKind) -> str:
    suffix = f"-{pipeline.side}" if kind is JobKind.PRUNE else ""
    return f"datadouble-{pipeline.name}-{kind}{suffix}"


def _path(pipeline: Pipeline, kind: JobKind) -> PurePosixPath:
    return PurePosixPath(f"jenkins/{_stem(pipeline, kind)}.Jenkinsfile")


def _label(pipeline: Pipeline) -> str:
    labels = [label.strip() for label in (pipeline.runner or "").split(",") if label.strip()]
    if not labels:
        message = "name the agent labels the jobs run on (--runner)"
        raise CiError(message)
    return " && ".join(labels)


class _Runtime(BaseModel):
    """The spec's ``kubernetes`` runtime settings an agent's pod must agree with."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    claim: str
    mount: PurePosixPath
    node: str | None = None
    image_pull_secrets: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Placement:
    """Where a side's pods run: its namespace, and its claim's mount."""

    namespace: str
    mount: str
    node: str | None = None
    image_pull_secrets: tuple[str, ...] = ()


def _runtime(pipeline: Pipeline, namespace: str) -> _Runtime:
    settings = (pipeline.runtime or {}).get("kubernetes")
    starting = sorted(
        {str(j.kind) for j in pipeline.jobs if "runtime" in sections(j.kind, pipeline.side)}
    )
    if not isinstance(settings, dict):
        message = (
            f"{', '.join(starting)} start engine software, which Kubernetes agents have no"
            " Docker for; the spec's runtime must be kubernetes"
        )
        raise CiError(message)
    try:
        runtime = _Runtime.model_validate(settings)
    except ValidationError as error:
        fields = ", ".join(".".join(str(part) for part in e["loc"]) for e in error.errors())
        message = f"the spec's runtime.kubernetes settings are invalid: {fields}"
        raise CiError(message) from None
    runtime_namespace(pipeline, namespace)
    if runtime.claim != CLAIM:
        message = (
            f"the spec's runtime.kubernetes must name the claim {CLAIM}, which this side's pods"
            " mount, so that the engine pods share their files"
        )
        raise CiError(message)
    if not runtime.mount.is_absolute():
        message = "the spec's runtime.kubernetes.mount must be an absolute path"
        raise CiError(message)
    return runtime


def _placement(pipeline: Pipeline) -> _Placement:
    """The side's namespace, and its claim's mount, holding every path the spec names.

    As for Argo CD (decision 103), ``--runner`` names the side's namespace, whose
    ``datadouble`` claim holds the side's paths. Jobs that start engine software
    need the spec's ``kubernetes`` runtime on that claim, with every path under
    its mount, and its namespace left out (engine pods then start in the pod's
    own namespace) or this side's; other jobs mount the claim at the paths'
    common directory.
    """
    if not pipeline.runner:
        message = "name the Kubernetes namespace the jobs' pods run in (--runner)"
        raise CiError(message)
    namespace = pipeline.runner
    if not any("runtime" in sections(job.kind, pipeline.side) for job in pipeline.jobs):
        return _Placement(namespace, claim_mount(pipeline))
    runtime = _runtime(pipeline, namespace)
    mount = claim_mount(pipeline, str(runtime.mount))
    return _Placement(namespace, mount, runtime.node, runtime.image_pull_secrets)


def _account(pipeline: Pipeline) -> str:
    """The service account a side's pods run as."""
    return f"datadouble-{pipeline.name}-{pipeline.side}"


def pod(pipeline: Pipeline) -> dict[str, JsonValue]:
    """A Kubernetes agent's pod: the Datadouble image as the engine's user, on the claim.

    Only the Datadouble container runs as the engine's user: the plugin's agent
    container keeps its image's user, whose account and home SCM checkouts over
    SSH need. The two share the workspace, so the agent container starts with a
    umask that leaves what it creates, such as each ``sh`` step's control
    directory, writable by Datadouble's.
    """
    placement = _placement(pipeline)
    uid, _, gid = pipeline.user.partition(":")
    agent: dict[str, JsonValue] = {
        "name": "jnlp",
        "command": ["/bin/sh", "-c", AGENT_COMMAND, "jenkins-agent"],
    }
    container: dict[str, JsonValue] = {
        "name": CONTAINER,
        "image": pipeline.image,
        "command": ["sleep"],
        "args": ["infinity"],
        "securityContext": {
            "runAsUser": int(uid),
            "runAsGroup": int(gid),
            "runAsNonRoot": True,
            "allowPrivilegeEscalation": False,
        },
        "volumeMounts": [
            {"name": "work", "mountPath": placement.mount},
            {"name": "boundary", "mountPath": str(BOUNDARY), "readOnly": True},
        ],
    }
    spec: dict[str, JsonValue] = {
        "serviceAccountName": _account(pipeline),
        "containers": [agent, container],
        "volumes": [
            {"name": "work", "persistentVolumeClaim": {"claimName": CLAIM}},
            {"name": "boundary", "configMap": {"name": "datadouble-boundary", "optional": True}},
        ],
    }
    if placement.image_pull_secrets:
        spec["imagePullSecrets"] = [{"name": name} for name in placement.image_pull_secrets]
    if placement.node is not None:
        spec["nodeSelector"] = {"kubernetes.io/hostname": placement.node}
    return {"apiVersion": "v1", "kind": "Pod", "spec": spec}


def _multiline(text: str) -> list[str]:
    """A triple-single-quoted Groovy string, one line per element."""
    escaped = text.replace("\\", "\\\\").replace("'", "\\'")
    return ["'''", *escaped.splitlines(), "'''"]


def _agent(pipeline: Pipeline) -> list[str]:
    match pipeline.agent:
        case Agent.DOCKER:
            return [f"agent {{ label {_quote(_label(pipeline))} }}"]
        case Agent.KUBERNETES:
            placement = _placement(pipeline)
            manifest = yaml.safe_dump(pod(pipeline), sort_keys=False, width=100)
            lines = [f"cloud {_quote(pipeline.cloud)}"] if pipeline.cloud else []
            lines.append(f"namespace {_quote(placement.namespace)}")
            text = _multiline(manifest)
            lines.extend([f"yaml {text[0]}", *(f"    {line}" for line in text[1:])])
            return _block("agent", _block("kubernetes", lines))
        case _:
            assert_never(pipeline.agent)


def _argument(argument: str | Placeholder) -> str:
    return _PLACEHOLDERS[argument] if isinstance(argument, Placeholder) else _quote(argument)


def _parameters(job: Job) -> list[str]:
    lines: list[str] = []
    if Placeholder.GENERATION in job.parameters:
        lines.append("string(name: 'GENERATION', description: 'The staged generation to activate')")
    if Placeholder.DRY_RUN in job.parameters:
        lines.append(
            "booleanParam(name: 'DRY_RUN', defaultValue: false,"
            " description: 'Report what would be removed, without removing it')"
        )
    if _REQUESTS & set(job.triggers):
        lines.append(
            "string(name: 'OWNER', description: 'The request the copies are for, such as pr-7')"
        )
    if any(Placeholder.RUN in i.arguments for i in job.invocations):
        lines.append(
            "string(name: 'RETRY_KEY', description: 'To resume a failed build, its BUILD_TAG')"
        )
    return lines


def _triggers(pipeline: Pipeline, job: Job) -> list[str]:
    lines: list[str] = []
    if job.schedule is not None:
        lines.append(f"cron('TZ=UTC\\n{job.schedule}')")
    if job.after is not None:
        lines.append(
            f"upstream(upstreamProjects: {_quote(_stem(pipeline, job.after))},"
            " threshold: hudson.model.Result.UNSTABLE)"
        )
    return lines


def _when(invocation: Invocation) -> str | None:
    """Which builds run an invocation: those for a request, or the others."""
    if not invocation.when:
        return None
    if set(invocation.when) <= _REQUESTS:
        return "params.OWNER"
    return "!params.OWNER"


def _stage(pipeline: Pipeline, job: Job, index: int) -> list[str]:
    invocation = job.invocations[index]
    name = f"datadouble {invocation.arguments[0]}"
    if len(job.invocations) > 1:
        name += f" {index + 1}"
    arguments = ", ".join(_argument(a) for a in invocation.arguments)
    if pipeline.agent is Agent.KUBERNETES:
        where = [f"    container: {_quote(CONTAINER)},"]
    else:
        where = [
            f"    image: {_quote(pipeline.image)},",
            f"    user: {_quote(pipeline.user)},",
            *([f"    docker: {_quote(pipeline.docker)},"] if pipeline.docker is not None else []),
            *(
                [f"    dockerSocket: {_quote(pipeline.docker_socket)},"]
                if pipeline.docker_socket is not None
                else []
            ),
            f"    mounts: {_list(job.mounts)},",
        ]
    call = [
        "datadouble(",
        f"    job: {_quote(str(job.kind))},",
        *where,
        f"    environment: {_list(job.environment)},",
        f"    arguments: [{arguments}],",
        ")",
    ]
    if job.artifacts is not None:
        call.insert(-1, f"    artifacts: {_quote(job.artifacts)},")
    if job.environment:
        bindings = ", ".join(
            f"string(credentialsId: {_quote(name)}, variable: {_quote(name)})"
            for name in job.environment
        )
        call = [f"withCredentials([{bindings}]) {{", *(f"    {line}" for line in call), "}"]
    lines = [f"stage({_quote(name)}) {{"]
    condition = _when(invocation)
    if condition is not None:
        lines.append(f"    when {{ expression {{ {condition} }} }}")
    lines.append("    steps {")
    lines.extend(f"        {line}" for line in call)
    lines.extend(["    }", "}"])
    return lines


def _block(name: str, lines: list[str]) -> list[str]:
    return [f"{name} {{", *(f"    {line}" for line in lines), "}"] if lines else []


_CONDITIONS = {NotifyOn.FAILURE: ("failure", "aborted"), NotifyOn.WARNING: ("unstable",)}
"""The build results each outcome is: a timed-out build is aborted, and a warning unstable."""


def _post(pipeline: Pipeline, job: Job) -> list[str]:
    """``mail`` for the spec's email targets, in ``post``; a failed mail changes nothing."""
    notify = pipeline.notify
    emails = notify.emails() if notify is not None else ()
    if notify is None or not emails:
        return []
    lines: list[str] = []
    for outcome in NotifyOn:
        if outcome not in notify.when:
            continue
        for condition in _CONDITIONS[outcome]:
            subject = f"Datadouble {pipeline.name} {job.kind}: {condition}"
            mail = (
                f"mail(to: {_quote(', '.join(emails))}, subject: {_quote(subject)},"
                f" body: {_quote(subject + '.')}"
                " + (env.BUILD_URL ? ' The run: ' + env.BUILD_URL : ''))"
            )
            attempt = [
                "try {",
                f"    {mail}",
                "} catch (err) {",
                '    echo "notify\'s email could not be sent"',
                "}",
            ]
            lines.extend(_block(condition, _block("script", attempt)))
    return lines


def render(pipeline: Pipeline, job: Job) -> str:
    """One job's Jenkinsfile."""
    library = (
        f"library(identifier: {_quote(f'datadouble@v{pipeline.version}')}, retriever: modernSCM(\n"
        f"    scm: [$class: 'GitSCMSource', remote: {_quote(REPOSITORY)}],\n"
        f"    libraryPath: {_quote(LIBRARY_PATH)}))"
    )
    options = [
        "disableConcurrentBuilds()",
        f"timeout(time: {job.timeout_minutes}, unit: 'MINUTES')",
    ]
    stages: list[str] = []
    for index in range(len(job.invocations)):
        stages.extend(_stage(pipeline, job, index))
    body = [
        *_agent(pipeline),
        *_block("options", options),
        *_block("parameters", _parameters(job)),
        *_block("triggers", _triggers(pipeline, job)),
        *_block("stages", stages),
        *_block("post", _post(pipeline, job)),
    ]
    text = "\n".join([library, "", *_block("pipeline", body)])
    header = _HEADER.format(version=pipeline.version, job=_stem(pipeline, job.kind))
    return header + "\n" + text + "\n"


def _kubernetes(pipeline: Pipeline) -> list[str]:
    """What Kubernetes agents need: the plugin's cloud, the claim, RBAC and the boundary mark."""
    placement = _placement(pipeline)
    namespace = placement.namespace
    cloud = f"the cloud {pipeline.cloud}" if pipeline.cloud else "its default cloud"
    access = (
        "ReadWriteOnce is enough, since every pod runs on the runtime's node"
        if placement.node
        else "ReadWriteMany, or ReadWriteOnce with the runtime's node set so every pod runs on"
        " one node"
    )
    account = _account(pipeline)
    steps = [
        (
            f"keep the {pipeline.side} side in its own namespace, {namespace}, apart from the"
            " other side's, ideally in its own cluster: its claim holds that side's data"
        ),
        (
            f"install the Kubernetes plugin and set up {cloud}, whose credentials need a Role"
            f" in {namespace} allowing {_allowing(CLOUD_RULES)}"
        ),
        (
            f"in {namespace}, create the PersistentVolumeClaim {CLAIM} ({access}),"
            f" holding {placement.mount}, owned by {pipeline.user}"
        ),
        (
            f"in {namespace}, create the service account {account}, bound to a Role allowing"
            f" {_allowing(ACCOUNT_RULES)}"
        ),
    ]
    if pipeline.side is Side.NON_PRODUCTION:
        steps.append(
            f"for the k8s-pv replicate adapter, also bind {account} to a ClusterRole allowing"
            f" {_allowing(VOLUME_RULES)}"
        )
    else:
        steps.append(
            f"inside the production boundary only, create the ConfigMap datadouble-boundary"
            f" in {namespace} with DATADOUBLE_BOUNDARY=inside; nothing else marks the pods"
        )
    return steps


class Jenkins:
    """Jenkins: a Jenkinsfile per job, the shared library's step, build results."""

    @property
    def name(self) -> str:
        """``jenkins``."""
        return NAME

    @property
    def agents(self) -> tuple[Agent, ...]:
        """Agents with Docker by default, or Kubernetes pods."""
        return (Agent.DOCKER, Agent.KUBERNETES)

    def generate(self, pipeline: Pipeline) -> tuple[GeneratedFile, ...]:
        """A Jenkinsfile per job."""
        return tuple(
            GeneratedFile(path=_path(pipeline, job.kind), content=render(pipeline, job))
            for job in pipeline.jobs
        )

    def instructions(self, pipeline: Pipeline) -> tuple[str, ...]:
        """The Pipeline jobs to create, credentials, requests' own pipelines, and pods' setup."""
        steps = _kubernetes(pipeline) if pipeline.agent is Agent.KUBERNETES else []
        steps += [
            f"create the Pipeline job {_stem(pipeline, job.kind)}, running"
            f" {_path(pipeline, job.kind)} from this repository (Pipeline script from SCM),"
            " and build it once so Jenkins reads its triggers"
            for job in pipeline.jobs
        ]
        if pipeline.environment():
            steps.append("add each variable above as a Secret text credential with that ID")
        requested = [job for job in pipeline.jobs if _REQUESTS & set(job.triggers)]
        if requested:
            names = " and ".join(_stem(pipeline, job.kind) for job in requested)
            steps.append(
                f"start {names} from each request's own pipeline, with OWNER naming the"
                " request: build(job: '...', parameters: [string(name: 'OWNER', value: ...)])"
            )
        return tuple(steps)

    def vendor_hosted(self, environ: Mapping[str, str]) -> str | None:
        """Jenkins agents are always the team's own."""
        del environ
        return None

    def run_url(self, environ: Mapping[str, str]) -> str | None:
        """The build's page, ``BUILD_URL``, when Jenkins knows its own URL."""
        return environ.get("BUILD_URL") or None

    def mask(self, value: str) -> None:
        """Jenkins masks credentials it binds; nothing to do at run time."""
        del value

    def report(self, report: Report, directory: Path | None) -> ExitCode:
        """Outputs for the step to return; a warning exits with 3, for ``unstable``."""
        if directory is not None:
            lines = (
                f"{name}={value}\n" for name, value in report.outputs.items() if "\n" not in value
            )
            (directory / "outputs").write_text("".join(lines), encoding="utf-8")
        match report.outcome:
            case Outcome.SUCCESS:
                return ExitCode.SUCCESS
            case Outcome.WARNING:
                return ExitCode.WARNING
            case Outcome.FAILURE:
                return ExitCode.FAILURE
            case _:
                assert_never(report.outcome)


def adapter() -> Jenkins:
    """The ``jenkins`` entry point."""
    return Jenkins()
