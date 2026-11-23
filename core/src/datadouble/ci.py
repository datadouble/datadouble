# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""CI/CD adapters: generating jobs, and bridging a run to the CI system.

A CI/CD adapter contains no pipeline logic. The core decides what each job
runs (:func:`pipeline`): its ``datadouble`` command lines, triggers,
schedules and run-time parameters, the secrets it reads by name, and the
host paths its container needs. The adapter only renders that in its
system's own configuration, and at run time maps a response onto the
system's outputs, summary, outcome and log masking.

CI/CD adapters register under ``datadouble.ci``; the entry point names a
callable that returns a :class:`CiAdapter`.
"""

import re
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Protocol

from pydantic import BaseModel, ConfigDict, JsonValue

from datadouble.names import SpecName
from datadouble.outcome import ExitCode, Outcome
from datadouble.spec import Notify, ReplicateMode, Spec


class CiError(Exception):
    """Jobs that cannot be generated as asked."""


class JobKind(StrEnum):
    """The jobs the lifecycle splits into."""

    BUILD = "build"
    PROMOTE = "promote"
    FETCH = "fetch"
    PROVISION = "provision"
    RELEASE = "release"
    PRUNE = "prune"
    ACTIVATE = "activate"
    LOCK = "lock"


class Side(StrEnum):
    """Which side of the production boundary jobs run on."""

    PRODUCTION = "production"
    NON_PRODUCTION = "non-production"


class Trigger(StrEnum):
    """What starts a job."""

    SCHEDULE = "schedule"
    AFTER = "after"
    """Another job of the same spec finishing successfully."""
    REQUEST_OPENED = "request-opened"
    """A pull or merge request opened: on-demand copies are made for it."""
    REQUEST_CLOSED = "request-closed"
    MANUAL = "manual"


class Placeholder(StrEnum):
    """A command-line argument only the CI system knows when the job runs."""

    RUN = "run"
    """The CI system's ID for this run, the same when a run is retried."""
    OWNER = "owner"
    """Who on-demand copies are for: the pull or merge request."""
    GENERATION = "generation"
    """A generation the person starting the job chooses."""
    DRY_RUN = "dry-run"
    """``--dry-run`` when the person starting the job asks for it; otherwise nothing."""


type Argument = str | Placeholder


@dataclass(frozen=True, slots=True)
class Invocation:
    """One ``datadouble`` command line a job runs, after the global options."""

    arguments: tuple[Argument, ...]
    when: tuple[Trigger, ...] = ()
    """The triggers it runs for; every trigger when empty."""


@dataclass(frozen=True, slots=True)
class Job:
    """One job: what starts it and what it runs."""

    kind: JobKind
    triggers: tuple[Trigger, ...]
    invocations: tuple[Invocation, ...]
    schedule: str | None = None
    """A cron expression, in UTC."""
    after: JobKind | None = None
    parameters: tuple[Placeholder, ...] = ()
    """Values the person starting the job by hand gives."""
    timeout_minutes: int = 60
    mounts: tuple[str, ...] = ()
    """Host paths the job's container sees at the same path."""
    environment: tuple[str, ...] = ()
    """Environment variables the job reads secrets from, by name."""
    artifacts: str | None = None
    """A directory, relative to the workspace, the job writes files into for the CI system
    to publish as the job's artifact. The adapter makes it writable by the job's user and
    publishes it; the job never commits anything."""


@dataclass(frozen=True, slots=True)
class Pipeline:
    """Everything a CI/CD adapter needs to write one side's jobs for a spec."""

    spec: str
    """The spec's path from the repository root."""
    name: SpecName
    side: Side
    jobs: tuple[Job, ...]
    runner: str | None
    """The runner (label, tag, agent label) jobs run on; None for the system's default."""
    image: str
    """The pinned Datadouble image the jobs run."""
    version: str
    user: str
    """The ``uid:gid`` Datadouble runs as: the engine's owner (decision 86)."""
    identity_token: bool = False
    """Whether jobs need the CI system's identity token, for keyless signing in ``build``."""
    spec_text: str = ""
    """The spec file's text, for systems whose configuration carries it (Argo CD)."""
    lock_text: str = ""
    """The catalog lock's text, empty when there is none."""
    notify: Notify | None = None
    """The spec's ``notify`` section: Datadouble posts its webhooks itself, and the
    adapter writes its email in the system's own form, or explains the setup."""

    def environment(self) -> tuple[str, ...]:
        """Every environment variable a job reads a secret from."""
        return tuple(sorted({name for job in self.jobs for name in job.environment}))


class GeneratedFile(BaseModel):
    """A configuration file an adapter generated, relative to the repository root."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    path: PurePosixPath
    content: str


class CiBridge(BaseModel):
    """A request's CI system: whose outputs, summary and masking to use."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    system: str
    directory: Path | None = None
    """Where the adapter writes outputs and the summary for the native step to pick up."""
    job: JobKind | None = None
    """The generated job the request runs for: its spec's ``notify`` section applies."""


class Report(BaseModel):
    """What a CI system is told about a response. Never holds data values."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    command: str
    outcome: Outcome
    outputs: dict[str, str]
    summary: str
    """For people, as the CLI prints it."""
    message: str | None = None
    """Why a request failed."""
    warnings: tuple[str, ...] = ()
    files: tuple[Path, ...] = ()
    """Files the command wrote for the job's artifact (the ``lock`` job's lock and changes),
    for systems whose generated configuration cannot publish them (Argo CD prints them)."""


class CiAdapter(Protocol):
    """A CI/CD system."""

    @property
    def name(self) -> str:
        """The name requests use for this system."""
        ...

    def generate(self, pipeline: Pipeline) -> tuple[GeneratedFile, ...]:
        """The system's configuration for the pipeline's jobs."""
        ...

    def instructions(self, pipeline: Pipeline) -> tuple[str, ...]:
        """Setup the system's configuration cannot hold, such as GitLab's pipeline schedules."""
        ...

    def vendor_hosted(self, environ: Mapping[str, str]) -> str | None:
        """A description of the runner, if ``environ`` shows the vendor hosts it."""
        ...

    def run_url(self, environ: Mapping[str, str]) -> str | None:
        """The web page of the running job, from the system's variables in ``environ``."""
        ...

    def mask(self, value: str) -> None:
        """Have the system hide a resolved secret value in its logs."""
        ...

    def report(self, report: Report, directory: Path | None) -> ExitCode:
        """Write outputs, the summary and annotations; return the exit code to use."""
        ...


type CiFactory = Callable[[], CiAdapter]


# Log masking

_MASK: ContextVar[Callable[[str], None] | None] = ContextVar("datadouble_mask", default=None)


@contextmanager
def masking(mask: Callable[[str], None]) -> Generator[None]:
    """Pass every secret value resolved inside the block to ``mask``."""
    token = _MASK.set(mask)
    try:
        yield
    finally:
        _MASK.reset(token)


def mask(value: str) -> None:
    """Hand a resolved secret value to the CI system's log masking, if there is one."""
    callback = _MASK.get()
    if callback is not None:
        callback(value)


# Jobs

IMAGE = "ghcr.io/datadouble/datadouble"
"""Where release images are published; tagged ``v<version>``."""
PRODUCTION_JOBS = frozenset({JobKind.BUILD, JobKind.PROMOTE, JobKind.LOCK})
NON_PRODUCTION_JOBS = frozenset(
    {JobKind.FETCH, JobKind.PROVISION, JobKind.RELEASE, JobKind.ACTIVATE}
)
_SECTIONS: dict[tuple[JobKind, Side], tuple[str, ...]] = {
    (JobKind.BUILD, Side.PRODUCTION): (
        "source",
        "files",
        "package",
        "store",
        "signing",
        "secrets",
        "runtime",
        "notify",
    ),
    (JobKind.PROMOTE, Side.PRODUCTION): ("store", "promote", "trust", "notify"),
    (JobKind.LOCK, Side.PRODUCTION): ("source", "notify"),
    (JobKind.PRUNE, Side.PRODUCTION): ("store", "promote", "notify"),
    (JobKind.FETCH, Side.NON_PRODUCTION): ("fetch", "trust", "notify"),
    (JobKind.PROVISION, Side.NON_PRODUCTION): (
        "fetch",
        "replicate",
        "announce",
        "runtime",
        "notify",
    ),
    (JobKind.RELEASE, Side.NON_PRODUCTION): ("fetch", "replicate", "announce", "runtime", "notify"),
    (JobKind.PRUNE, Side.NON_PRODUCTION): ("fetch", "replicate", "notify"),
    (JobKind.ACTIVATE, Side.NON_PRODUCTION): ("fetch", "notify"),
}
"""The spec sections each job reads: its secrets and host paths come only from these,
so a job never receives another's credentials. Every job reads ``notify``, whose
webhooks it posts to."""
_PRUNE_TARGETS = {Side.PRODUCTION: ("store", "export"), Side.NON_PRODUCTION: ("staging",)}
_STAGES = "plan,extract,load,transform,package,verify,store"
_HOST_PATH = re.compile(r"/[^\s*?\[\]{}:]+")
_BUILD_MINUTES = 12 * 60
LOCK_ARTIFACTS = "datadouble-lock"
"""Where the ``lock`` job writes the new lock and its changes, in the workspace."""


def side_of(kinds: Iterable[JobKind], side: Side | None) -> Side:
    """The side the jobs run on; ``side`` is needed only for ``prune`` alone."""
    chosen = set(kinds)
    production = chosen & PRODUCTION_JOBS
    others = chosen & NON_PRODUCTION_JOBS
    if production and others:
        message = (
            f"{', '.join(sorted(production))} run in production and"
            f" {', '.join(sorted(others))} outside it; generate each side separately"
        )
        raise CiError(message)
    found = Side.PRODUCTION if production else Side.NON_PRODUCTION if others else side
    if found is None:
        message = "prune runs on either side; name the side"
        raise CiError(message)
    if side is not None and side is not found:
        message = f"those jobs run on the {found} side, not {side}"
        raise CiError(message)
    return found


def _sections(spec: Spec, kind: JobKind, side: Side) -> list[JsonValue]:
    dumped = spec.model_dump(mode="json", by_alias=True, exclude_none=True)
    # Of package, only accounts' password references name secrets or files: a
    # plain-text password or a user name that looks like a path must never become
    # a mount, and so reach a command line.
    dumped["package"] = [
        account.password.model_dump(mode="json")
        for account in spec.package.accounts
        if not isinstance(account.password, str)
    ]
    return [dumped[name] for name in _SECTIONS[kind, side] if name in dumped]


def _walk(value: JsonValue) -> Iterator[JsonValue]:
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def secret_names(spec: Spec, kind: JobKind, side: Side) -> tuple[str, ...]:
    """The environment variables a job's spec sections read secrets from."""
    names: set[str] = set()
    for section in _sections(spec, kind, side):
        for value in _walk(section):
            if isinstance(value, dict) and len(value) == 1:
                name = value.get("env")
                if isinstance(name, str):
                    names.add(name)
    return tuple(sorted(names))


def host_paths(
    spec: Spec, kind: JobKind, side: Side, extra: Iterable[Path] = ()
) -> tuple[str, ...]:
    """Absolute paths a job's spec sections name, with none inside another."""
    found = {str(path) for path in extra}
    found |= {
        value.rstrip("/")
        for section in _sections(spec, kind, side)
        for value in _walk(section)
        if isinstance(value, str) and _HOST_PATH.fullmatch(value) and value != "/"
    }
    ordered = sorted(found)
    return tuple(
        path
        for path in ordered
        if not any(path.startswith(other + "/") for other in ordered if other != path)
    )


def _replicate_mode(spec: Spec) -> ReplicateMode:
    if spec.replicate is None:
        message = "provision and release need the spec's `replicate` section"
        raise CiError(message)
    return spec.replicate.mode


@dataclass(frozen=True, slots=True)
class _Context:
    spec: str
    model: Spec
    side: Side
    chosen: frozenset[JobKind]
    work: str


def _build(context: _Context) -> Job:
    arguments = ("run", _STAGES, "--spec", context.spec, "--work", context.work)
    return Job(
        JobKind.BUILD,
        (Trigger.SCHEDULE, Trigger.MANUAL),
        (Invocation((*arguments, "--retry-key", Placeholder.RUN)),),
        schedule="0 2 * * 1",
        timeout_minutes=_BUILD_MINUTES,
    )


def _promote(context: _Context) -> Job:
    after = JobKind.BUILD if JobKind.BUILD in context.chosen else None
    return Job(
        JobKind.PROMOTE,
        (Trigger.AFTER, Trigger.MANUAL) if after else (Trigger.MANUAL,),
        (Invocation(("promote", "--spec", context.spec)),),
        after=after,
    )


def _fetch(context: _Context) -> Job:
    return Job(
        JobKind.FETCH,
        (Trigger.SCHEDULE, Trigger.MANUAL),
        (Invocation(("fetch", "--spec", context.spec)),),
        schedule="*/15 * * * *",
    )


def _provision(context: _Context) -> Job:
    if _replicate_mode(context.model) is ReplicateMode.ON_DEMAND:
        return Job(
            JobKind.PROVISION,
            (Trigger.REQUEST_OPENED,),
            (Invocation(("replicate", "--spec", context.spec, "--owner", Placeholder.OWNER)),),
        )
    after = JobKind.FETCH if JobKind.FETCH in context.chosen else None
    triggers = (Trigger.SCHEDULE, Trigger.MANUAL)
    return Job(
        JobKind.PROVISION,
        (*triggers, Trigger.AFTER) if after else triggers,
        (Invocation(("replicate", "--spec", context.spec)),),
        schedule="0 * * * *",
        after=after,
    )


def _release(context: _Context) -> Job:
    sweep = ("release", "--spec", context.spec, "--expired")
    if _replicate_mode(context.model) is ReplicateMode.ON_DEMAND:
        closed = Invocation(
            ("release", "--spec", context.spec, "--owner", Placeholder.OWNER),
            when=(Trigger.REQUEST_CLOSED,),
        )
        return Job(
            JobKind.RELEASE,
            (Trigger.REQUEST_CLOSED, Trigger.SCHEDULE, Trigger.MANUAL),
            (closed, Invocation(sweep, when=(Trigger.SCHEDULE, Trigger.MANUAL))),
            schedule="30 3 * * *",
        )
    return Job(
        JobKind.RELEASE,
        (Trigger.SCHEDULE, Trigger.MANUAL),
        (Invocation(sweep),),
        schedule="30 3 * * *",
    )


def _prune(context: _Context) -> Job:
    return Job(
        JobKind.PRUNE,
        (Trigger.SCHEDULE, Trigger.MANUAL),
        tuple(
            Invocation(("prune", "--spec", context.spec, "--target", target, Placeholder.DRY_RUN))
            for target in _PRUNE_TARGETS[context.side]
        ),
        schedule="0 4 * * *",
        parameters=(Placeholder.DRY_RUN,),
    )


def _lock(context: _Context) -> Job:
    """Re-lock by hand: the new lock and its changes are published for review, never committed."""
    return Job(
        JobKind.LOCK,
        (Trigger.MANUAL,),
        (Invocation(("lock", "--spec", context.spec, "--output", LOCK_ARTIFACTS)),),
        artifacts=LOCK_ARTIFACTS,
    )


def _activate(context: _Context) -> Job:
    return Job(
        JobKind.ACTIVATE,
        (Trigger.MANUAL,),
        (Invocation(("activate", Placeholder.GENERATION, "--spec", context.spec)),),
        parameters=(Placeholder.GENERATION,),
    )


_JOBS: dict[JobKind, Callable[[_Context], Job]] = {
    JobKind.BUILD: _build,
    JobKind.PROMOTE: _promote,
    JobKind.FETCH: _fetch,
    JobKind.PROVISION: _provision,
    JobKind.RELEASE: _release,
    JobKind.PRUNE: _prune,
    JobKind.ACTIVATE: _activate,
    JobKind.LOCK: _lock,
}


def _implicit(spec: Spec, kind: JobKind) -> tuple[str, ...]:
    """Secrets the job's programs read without the spec naming them."""
    keyed = spec.signing is not None and spec.signing.cosign.key is not None
    return ("COSIGN_PASSWORD",) if kind is JobKind.BUILD and keyed else ()


@dataclass(frozen=True, slots=True)
class PipelineOptions:
    """What ``ci init`` was asked for, beyond the spec."""

    kinds: tuple[JobKind, ...]
    spec_path: str
    image: str
    version: str
    owner: str
    runner: str | None = None
    work: Path | None = None
    side: Side | None = None
    spec_text: str = ""
    lock_text: str = ""
    environment: tuple[str, ...] = ()
    """More environment variables every job passes on, for credentials adapters read
    from the environment rather than the spec, such as ``AWS_ACCESS_KEY_ID``."""


def pipeline(spec: Spec, options: PipelineOptions) -> Pipeline:
    """The jobs to generate for one side of the boundary, decided here for every system."""
    side = side_of(options.kinds, options.side)
    chosen = frozenset(options.kinds)
    if side is Side.PRODUCTION and not options.runner:
        message = (
            "build, promote and lock run only inside the production boundary; name the"
            " runner they run on (decision 19)"
        )
        raise CiError(message)
    extra: list[Path] = []
    if JobKind.BUILD in chosen:
        if options.work is None or not options.work.is_absolute():
            message = (
                "build needs a work directory: an absolute path in production scratch space,"
                " kept between runs so a retried build resumes"
            )
            raise CiError(message)
        extra.append(options.work)
    if JobKind.PROMOTE in chosen and spec.promote is None:
        message = "promote needs the spec's `promote` section"
        raise CiError(message)
    if JobKind.FETCH in chosen and spec.fetch is None:
        message = "fetch needs the spec's `fetch` section"
        raise CiError(message)
    spec_path = options.spec_path
    context = _Context(spec_path, spec, side, chosen, str(options.work))
    jobs = tuple(
        replace(
            _JOBS[kind](context),
            mounts=host_paths(spec, kind, side, extra if kind is JobKind.BUILD else ()),
            environment=tuple(
                sorted(
                    {*secret_names(spec, kind, side), *_implicit(spec, kind), *options.environment}
                )
            ),
        )
        for kind in JobKind
        if kind in chosen
    )
    return Pipeline(
        spec=spec_path,
        name=spec.name,
        side=side,
        jobs=jobs,
        runner=options.runner,
        image=options.image,
        version=options.version,
        user=options.owner,
        identity_token=(
            JobKind.BUILD in chosen and spec.signing is not None and spec.signing.cosign.keyless
        ),
        spec_text=options.spec_text,
        lock_text=options.lock_text,
        notify=spec.notify,
    )
