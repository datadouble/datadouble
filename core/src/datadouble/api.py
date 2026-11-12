# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The JSON interface.

Every Datadouble operation is a JSON request answered by a JSON response. The
command-line interface, the CI/CD adapters and the agent skills only build
requests for this module, so behavior cannot differ between them.
"""

import platform
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Annotated, Literal, assert_never

import yaml
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from datadouble import __version__, jailer
from datadouble.announce import AnnounceError, announce, withdraw
from datadouble.anonymize import AnonymizeError
from datadouble.catalog import CatalogError, CatalogLock, read_lock, write_lock
from datadouble.channels import Channel
from datadouble.engine import EngineError
from datadouble.images import ImageError
from datadouble.jailer import JailerError
from datadouble.names import Generation, GroupName
from datadouble.planner import Plan, PlanError, PlanOptions, plan
from datadouble.promote import Promoted, PromoteError, promote
from datadouble.prune import PruneError, PruneResult, PruneTarget, prune
from datadouble.registry import Registry, RegistryError, installed
from datadouble.replicate import (
    Copy,
    Options,
    ReplicateError,
    ReplicateResult,
    claim,
    copies_of,
    release,
    replicate,
)
from datadouble.run import Run, RunError, RunOptions, RunResult, Stage
from datadouble.runtime import ContainerError
from datadouble.secrets import SecretError
from datadouble.settings import SettingsError
from datadouble.signing import SigningError
from datadouble.spec import (
    ImageFormat,
    Spec,
    SpecError,
    Strategy,
    StrictLevel,
    load_spec,
    spec_schema,
)
from datadouble.staging import Fetched, StagingError, activate, fetch
from datadouble.storage import StoreError
from datadouble.strict import StrictReport


class Command(StrEnum):
    """Every command the JSON interface accepts."""

    VERSION = "version"
    SCHEMA = "schema"
    LOCK = "lock"
    IMPORT_JAILER = "import-jailer"
    PLAN = "plan"
    RUN = "run"
    PROMOTE = "promote"
    FETCH = "fetch"
    ACTIVATE = "activate"
    REPLICATE = "replicate"
    CLAIM = "claim"
    RELEASE = "release"
    ANNOUNCE = "announce"
    COPIES = "copies"
    PRUNE = "prune"


class Outcome(StrEnum):
    """How an operation ended.

    Anything that could put real personal data into a copy is always a
    failure, never a warning.
    """

    SUCCESS = "success"
    WARNING = "warning"
    FAILURE = "failure"


class ExitCode(IntEnum):
    """Process exit codes, so CI systems can tell a warning from a failure."""

    SUCCESS = 0
    FAILURE = 1
    USAGE = 2
    WARNING = 3


class ErrorCode(StrEnum):
    """Machine-readable reasons a request failed."""

    INVALID_REQUEST = "invalid_request"
    INVALID_SPEC = "invalid_spec"
    INVALID_SETTINGS = "invalid_settings"
    CATALOG_LOCK = "catalog_lock"
    PLAN_FAILED = "plan_failed"
    ADAPTER_MISSING = "adapter_missing"
    UNRESOLVED_REFERENCE = "unresolved_reference"
    ENGINE_FAILED = "engine_failed"
    INVALID_SUBSET = "invalid_subset"
    CONTAINER_FAILED = "container_failed"
    ANONYMIZATION_FAILED = "anonymization_failed"
    STORE_FAILED = "store_failed"
    RUN_FAILED = "run_failed"
    PROMOTE_REFUSED = "promote_refused"
    SIGNATURE_FAILED = "signature_failed"
    STAGING_FAILED = "staging_failed"
    REPLICATE_FAILED = "replicate_failed"
    ANNOUNCE_FAILED = "announce_failed"
    PRUNE_FAILED = "prune_failed"


class _Message(BaseModel):
    """Base for every request and response: strict, immutable, no extra fields."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


# Requests


class VersionRequest(_Message):
    """Ask for the installed Datadouble version."""

    command: Literal[Command.VERSION]


class SchemaRequest(_Message):
    """Ask for the JSON Schema that specs are validated against."""

    command: Literal[Command.SCHEMA]


class _SpecRequest(_Message):
    spec: Path
    lock: Path | None = None
    """The catalog lock; ``catalog.lock`` next to the spec when omitted."""

    def lock_path(self) -> Path:
        return self.lock or self.spec.parent / "catalog.lock"


class LockRequest(_SpecRequest):
    """Introspect the source and write its catalog lock."""

    command: Literal[Command.LOCK]


class ImportJailerRequest(_SpecRequest):
    """Translate a Jailer subset into native spec settings, and lock the catalog."""

    command: Literal[Command.IMPORT_JAILER]
    model: Path
    """The extraction model (``.jm``)."""
    datamodel: Path
    """The data model folder."""
    output: Path
    """Where the native ``subset`` and ``exclude`` settings are written, as YAML."""


class _Overrides(_SpecRequest):
    strict: StrictLevel | None = None
    strategy: Strategy | None = None
    formats: tuple[ImageFormat, ...] | None = Field(default=None, min_length=1)

    def plan_options(self) -> PlanOptions:
        return PlanOptions(strict=self.strict, strategy=self.strategy, formats=self.formats)


class PlanRequest(_Overrides):
    """Plan a build against the live catalog without extracting anything."""

    command: Literal[Command.PLAN]


class RunRequest(_Overrides):
    """Run build stages, or resume a generation."""

    command: Literal[Command.RUN]
    stages: tuple[Stage, ...] = Field(min_length=1)
    work: Path = Path(".datadouble")
    resume: Generation | None = None


class _Groups(_Message):
    spec: Path
    groups: tuple[GroupName, ...] = ()
    """Image groups to work on; every group in the spec when empty."""


class PromoteRequest(_Groups):
    """Copy verified, signed images to the export stores (production)."""

    command: Literal[Command.PROMOTE]
    generation: Generation | None = None
    """The generation to promote; the newest in the build's store when omitted."""


class FetchRequest(_Groups):
    """Bring the export channel's images into staging, if they are new."""

    command: Literal[Command.FETCH]


class ActivateRequest(_Groups):
    """Point the staging channel at a staged generation (rollback)."""

    command: Literal[Command.ACTIVATE]
    generation: Generation


class ReplicateRequest(_Groups):
    """Fill the pool, or make on-demand copies for an owner; then announce them."""

    command: Literal[Command.REPLICATE]
    owner: str | None = None
    all_at_once: bool = False
    announce: bool = True


class ClaimRequest(_Groups):
    """Take an unclaimed pool copy for an owner; then announce it."""

    command: Literal[Command.CLAIM]
    owner: str = Field(min_length=1)
    announce: bool = True


class ReleaseRequest(_Groups):
    """Destroy copies by ID, owner or expiry, and withdraw their announcements."""

    command: Literal[Command.RELEASE]
    copy_ids: tuple[str, ...] = ()
    owner: str | None = None
    expired: bool = False


class AnnounceRequest(_Groups):
    """Announce existing copies again: an owner's, or every unclaimed pool copy."""

    command: Literal[Command.ANNOUNCE]
    owner: str | None = None


class CopiesRequest(_Groups):
    """List copies, without their credentials."""

    command: Literal[Command.COPIES]


class PruneRequest(_Message):
    """Apply retention to the build's stores, the export stores, or staging."""

    command: Literal[Command.PRUNE]
    spec: Path
    target: PruneTarget
    dry_run: bool = False


type Request = (
    VersionRequest
    | SchemaRequest
    | LockRequest
    | ImportJailerRequest
    | PlanRequest
    | RunRequest
    | PromoteRequest
    | FetchRequest
    | ActivateRequest
    | ReplicateRequest
    | ClaimRequest
    | ReleaseRequest
    | AnnounceRequest
    | CopiesRequest
    | PruneRequest
)

_REQUEST: TypeAdapter[Request] = TypeAdapter(
    Annotated[
        VersionRequest
        | SchemaRequest
        | LockRequest
        | ImportJailerRequest
        | PlanRequest
        | RunRequest
        | PromoteRequest
        | FetchRequest
        | ActivateRequest
        | ReplicateRequest
        | ClaimRequest
        | ReleaseRequest
        | AnnounceRequest
        | CopiesRequest
        | PruneRequest,
        Field(discriminator="command"),
    ]
)


# Responses


class VersionResponse(_Message):
    """The installed Datadouble version and the Python running it."""

    command: Literal[Command.VERSION] = Command.VERSION
    outcome: Literal[Outcome.SUCCESS] = Outcome.SUCCESS
    version: str
    python: str


class SchemaResponse(_Message):
    """The spec JSON Schema."""

    command: Literal[Command.SCHEMA] = Command.SCHEMA
    outcome: Literal[Outcome.SUCCESS] = Outcome.SUCCESS
    schema_: dict[str, JsonValue] = Field(alias="schema")

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", serialize_by_alias=True)


class LockResponse(_Message):
    """The catalog lock that was written."""

    command: Literal[Command.LOCK] = Command.LOCK
    outcome: Literal[Outcome.SUCCESS] = Outcome.SUCCESS
    lock: str
    fingerprint: str
    tables: int


class ImportJailerResponse(_Message):
    """Where the translated settings and the lock were written, and what was ignored."""

    command: Literal[Command.IMPORT_JAILER] = Command.IMPORT_JAILER
    outcome: Outcome
    output: str
    lock: str
    report: tuple[str, ...]


class PlanResponse(_Message):
    """A plan, with any warnings."""

    command: Literal[Command.PLAN] = Command.PLAN
    outcome: Literal[Outcome.SUCCESS, Outcome.WARNING]
    plan: Plan


class RunResponse(_Message):
    """What a run did; ``failure`` when an image failed verification."""

    command: Literal[Command.RUN] = Command.RUN
    outcome: Outcome
    result: RunResult


class PromoteResponse(_Message):
    """The images promoted."""

    command: Literal[Command.PROMOTE] = Command.PROMOTE
    outcome: Literal[Outcome.SUCCESS] = Outcome.SUCCESS
    promoted: tuple[Promoted, ...]


class FetchResponse(_Message):
    """What fetch did per image line."""

    command: Literal[Command.FETCH] = Command.FETCH
    outcome: Literal[Outcome.SUCCESS] = Outcome.SUCCESS
    fetched: tuple[Fetched, ...]


class ActivateResponse(_Message):
    """The staging channels as they now point."""

    command: Literal[Command.ACTIVATE] = Command.ACTIVATE
    outcome: Literal[Outcome.SUCCESS] = Outcome.SUCCESS
    channels: tuple[Channel, ...]


class CopiesResponse(_Message):
    """Copies made, claimed, released or listed, and where they were announced."""

    command: Literal[
        Command.REPLICATE, Command.CLAIM, Command.RELEASE, Command.ANNOUNCE, Command.COPIES
    ]
    outcome: Literal[Outcome.SUCCESS] = Outcome.SUCCESS
    result: ReplicateResult
    announced: tuple[str, ...] = ()
    withdrawn: tuple[str, ...] = ()


class PruneResponse(_Message):
    """What prune removed, or would remove."""

    command: Literal[Command.PRUNE] = Command.PRUNE
    outcome: Literal[Outcome.SUCCESS] = Outcome.SUCCESS
    result: PruneResult
    bytes: int


class ErrorDetail(_Message):
    """One problem found in a request."""

    location: str
    problem: str


class ErrorResponse(_Message):
    """A request that could not be carried out."""

    outcome: Literal[Outcome.FAILURE] = Outcome.FAILURE
    error: ErrorCode
    message: str
    details: tuple[ErrorDetail, ...] = ()
    strict: StrictReport | None = None
    """For strict-mode failures: each new object and how to resolve it."""


type Response = (
    VersionResponse
    | SchemaResponse
    | LockResponse
    | ImportJailerResponse
    | PlanResponse
    | RunResponse
    | PromoteResponse
    | FetchResponse
    | ActivateResponse
    | CopiesResponse
    | PruneResponse
    | ErrorResponse
)


# Handling

_ERRORS: tuple[tuple[type[Exception], ErrorCode], ...] = (
    (SettingsError, ErrorCode.INVALID_SETTINGS),
    (CatalogError, ErrorCode.CATALOG_LOCK),
    (RegistryError, ErrorCode.ADAPTER_MISSING),
    (SecretError, ErrorCode.UNRESOLVED_REFERENCE),
    (EngineError, ErrorCode.ENGINE_FAILED),
    (JailerError, ErrorCode.INVALID_SUBSET),
    (ImageError, ErrorCode.ENGINE_FAILED),
    (ContainerError, ErrorCode.CONTAINER_FAILED),
    (AnonymizeError, ErrorCode.ANONYMIZATION_FAILED),
    (StoreError, ErrorCode.STORE_FAILED),
    (RunError, ErrorCode.RUN_FAILED),
    (SigningError, ErrorCode.SIGNATURE_FAILED),
    (StagingError, ErrorCode.STAGING_FAILED),
    (ReplicateError, ErrorCode.REPLICATE_FAILED),
    (AnnounceError, ErrorCode.ANNOUNCE_FAILED),
    (PruneError, ErrorCode.PRUNE_FAILED),
)
_HANDLED: tuple[type[Exception], ...] = (
    SpecError,
    PlanError,
    PromoteError,
    *(kind for kind, _ in _ERRORS),
)


def _error(error: Exception) -> ErrorResponse:
    match error:
        case SpecError():
            return ErrorResponse(
                error=ErrorCode.INVALID_SPEC,
                message=str(error),
                details=tuple(ErrorDetail(location=loc, problem=msg) for loc, msg in error.details),
            )
        case PlanError():
            return ErrorResponse(
                error=ErrorCode.PLAN_FAILED,
                message=str(error),
                details=tuple(ErrorDetail(location="plan", problem=p) for p in error.problems),
                strict=error.strict,
            )
        case PromoteError():
            return ErrorResponse(
                error=ErrorCode.PROMOTE_REFUSED,
                message=str(error),
                details=tuple(ErrorDetail(location="promote", problem=p) for p in error.problems),
            )
        case _:
            code = next(code for kind, code in _ERRORS if isinstance(error, kind))
            return ErrorResponse(error=code, message=str(error))


def _lock(request: _SpecRequest) -> CatalogLock | None:
    path = request.lock_path()
    return read_lock(path) if path.exists() else None


def _write_lock(request: LockRequest, registry: Registry) -> LockResponse:
    spec = load_spec(request.spec)
    engine = registry.engine(spec.source.engine)
    with engine.connect(spec.source.connection) as session:
        lock = CatalogLock.of(session.catalog())
    path = request.lock_path()
    write_lock(lock, path)
    return LockResponse(
        lock=str(path), fingerprint=lock.fingerprint, tables=len(lock.catalog.tables)
    )


def _import_jailer(request: ImportJailerRequest, registry: Registry) -> ImportJailerResponse:
    spec = load_spec(request.spec)
    engine = registry.engine(spec.source.engine)
    with engine.connect(spec.source.connection) as session:
        catalog = session.catalog()
    translated = jailer.load(
        request.model, request.datamodel, catalog, case_sensitive=spec.case_sensitive
    )
    settings = {
        "exclude": list(translated.exclude),
        "subset": translated.subset.model_dump(mode="json", exclude_defaults=True),
    }
    request.output.write_text(yaml.safe_dump(settings, sort_keys=False), "utf-8")
    lock = CatalogLock.of(catalog)
    write_lock(lock, request.lock_path())
    return ImportJailerResponse(
        outcome=Outcome.WARNING if translated.report else Outcome.SUCCESS,
        output=str(request.output),
        lock=str(request.lock_path()),
        report=translated.report,
    )


def _plan(request: PlanRequest, registry: Registry) -> PlanResponse:
    spec = load_spec(request.spec)
    lock = _lock(request)
    engine = registry.engine(spec.source.engine)
    with engine.connect(spec.source.connection) as session:
        planned = plan(spec, session.catalog(), lock, engine.capabilities(), request.plan_options())
    if planned.warnings:
        return PlanResponse(outcome=Outcome.WARNING, plan=planned)
    return PlanResponse(outcome=Outcome.SUCCESS, plan=planned)


def _run(request: RunRequest, registry: Registry) -> RunResponse:
    spec = load_spec(request.spec)
    options = RunOptions(
        stages=request.stages,
        work=request.work,
        resume=request.resume,
        plan=request.plan_options(),
    )
    result = Run(spec, _lock(request), registry, options).execute()
    if result.failed():
        outcome = Outcome.FAILURE
    elif result.warnings:
        outcome = Outcome.WARNING
    else:
        outcome = Outcome.SUCCESS
    return RunResponse(outcome=outcome, result=result)


def _copies(request: _Groups, registry: Registry) -> CopiesResponse:
    spec = load_spec(request.spec)
    match request:
        case ReplicateRequest():
            options = Options(
                groups=request.groups, owner=request.owner, all_at_once=request.all_at_once
            )
            result = replicate(spec, registry, options)
            fresh = _with_secrets(spec, request.groups, result.created)
            announced = announce(spec, registry, fresh) if request.announce else ()
            return CopiesResponse(command=request.command, result=result, announced=announced)
        case ClaimRequest():
            result = claim(spec, registry, Options(groups=request.groups, owner=request.owner))
            taken = _with_secrets(spec, request.groups, result.created)
            announced = announce(spec, registry, taken) if request.announce else ()
            public = result.model_copy(update={"created": tuple(c.public() for c in taken)})
            return CopiesResponse(command=request.command, result=public, announced=announced)
        case ReleaseRequest():
            options = Options(
                groups=request.groups,
                owner=request.owner,
                copy_ids=request.copy_ids,
                expired=request.expired,
            )
            result = release(spec, registry, options)
            withdrawn = withdraw(spec, registry, tuple(c.id for c in result.removed))
            return CopiesResponse(command=request.command, result=result, withdrawn=withdrawn)
        case AnnounceRequest():
            everything = copies_of(spec, request.groups, with_secrets=True)
            chosen = tuple(
                c
                for c in everything
                if (c.owner == request.owner if request.owner else not c.claimed)
            )
            announced = announce(spec, registry, chosen)
            result = ReplicateResult(copies=tuple(c.public() for c in everything))
            return CopiesResponse(command=request.command, result=result, announced=announced)
        case _:
            result = ReplicateResult(copies=copies_of(spec, request.groups))
            return CopiesResponse(command=Command.COPIES, result=result)


def _with_secrets(
    spec: Spec, groups: tuple[GroupName, ...], chosen: tuple[Copy, ...]
) -> tuple[Copy, ...]:
    ids = {c.id for c in chosen}
    return tuple(c for c in copies_of(spec, groups, with_secrets=True) if c.id in ids)


def _operate(request: Request, registry: Registry) -> Response:
    match request:
        case PromoteRequest():
            spec = load_spec(request.spec)
            promoted = promote(spec, registry, groups=request.groups, generation=request.generation)
            return PromoteResponse(promoted=promoted)
        case FetchRequest():
            spec = load_spec(request.spec)
            return FetchResponse(fetched=fetch(spec, registry, groups=request.groups))
        case ActivateRequest():
            spec = load_spec(request.spec)
            return ActivateResponse(
                channels=activate(spec, request.generation, groups=request.groups)
            )
        case PruneRequest():
            spec = load_spec(request.spec)
            result = prune(spec, registry, request.target, dry_run=request.dry_run)
            return PruneResponse(result=result, bytes=result.bytes)
        case (
            ReplicateRequest()
            | ClaimRequest()
            | ReleaseRequest()
            | AnnounceRequest()
            | CopiesRequest()
        ):
            return _copies(request, registry)
        case _:  # pragma: no cover - handle() routes the build commands itself
            message = f"{request.command} is not an operation"
            raise RunError(message)


def _build(
    request: LockRequest | ImportJailerRequest | PlanRequest | RunRequest, registry: Registry
) -> Response:
    match request:
        case LockRequest():
            return _write_lock(request, registry)
        case ImportJailerRequest():
            return _import_jailer(request, registry)
        case PlanRequest():
            return _plan(request, registry)
        case RunRequest():
            return _run(request, registry)
        case _:
            assert_never(request)


def handle(request: Request, registry: Registry | None = None) -> Response:
    """Carry out a parsed request with the installed adapters (or ``registry``)."""
    try:
        match request:
            case VersionRequest():
                return VersionResponse(version=__version__, python=platform.python_version())
            case SchemaRequest():
                return SchemaResponse(schema=spec_schema())
            case LockRequest() | ImportJailerRequest() | PlanRequest() | RunRequest():
                return _build(request, registry or installed())
            case (
                PromoteRequest()
                | FetchRequest()
                | ActivateRequest()
                | ReplicateRequest()
                | ClaimRequest()
                | ReleaseRequest()
                | AnnounceRequest()
                | CopiesRequest()
                | PruneRequest()
            ):
                return _operate(request, registry or installed())
            case _:
                assert_never(request)
    except _HANDLED as error:
        return _error(error)


_TAG_ERRORS = frozenset({"union_tag_invalid", "union_tag_not_found"})


def _location(kind: str, loc: tuple[int | str, ...]) -> str:
    """Where a problem is, without the union branch the request was matched to."""
    if kind in _TAG_ERRORS:
        return "command"
    parts = list(loc[1:]) if loc and loc[0] in set(Command) else list(loc)
    return ".".join(str(part) for part in parts) or "(request)"


def parse_request(raw: str | bytes) -> Request | ErrorResponse:
    """Parse a JSON request, or explain why it is not a valid one."""
    try:
        return _REQUEST.validate_json(raw)
    except ValidationError as error:
        details = tuple(
            ErrorDetail(location=_location(item["type"], item["loc"]), problem=item["msg"])
            for item in error.errors(include_url=False)
        )
        return ErrorResponse(
            error=ErrorCode.INVALID_REQUEST,
            message="The request is not a valid Datadouble request.",
            details=details,
        )


def handle_json(raw: str | bytes, registry: Registry | None = None) -> Response:
    """Parse and carry out a JSON request."""
    parsed = parse_request(raw)
    if isinstance(parsed, ErrorResponse):
        return parsed
    return handle(parsed, registry)


def exit_code(response: Response) -> ExitCode:
    """The process exit code for a response."""
    match response.outcome:
        case Outcome.SUCCESS:
            return ExitCode.SUCCESS
        case Outcome.WARNING:
            return ExitCode.WARNING
        case Outcome.FAILURE:
            return ExitCode.FAILURE
        case _:
            assert_never(response.outcome)
