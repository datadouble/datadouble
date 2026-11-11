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

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from datadouble import __version__
from datadouble.anonymize import AnonymizeError
from datadouble.catalog import CatalogError, CatalogLock, read_lock, write_lock
from datadouble.engine import EngineError
from datadouble.images import ImageError
from datadouble.names import Generation
from datadouble.planner import Plan, PlanError, PlanOptions, plan
from datadouble.registry import Registry, RegistryError, installed
from datadouble.run import Run, RunError, RunOptions, RunResult, Stage
from datadouble.secrets import SecretError
from datadouble.settings import SettingsError
from datadouble.spec import ImageFormat, SpecError, Strategy, StrictLevel, load_spec, spec_schema
from datadouble.storage import StoreError
from datadouble.strict import StrictReport


class Command(StrEnum):
    """Every command the JSON interface accepts."""

    VERSION = "version"
    SCHEMA = "schema"
    LOCK = "lock"
    PLAN = "plan"
    RUN = "run"


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
    ANONYMIZATION_FAILED = "anonymization_failed"
    STORE_FAILED = "store_failed"
    RUN_FAILED = "run_failed"


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


type Request = VersionRequest | SchemaRequest | LockRequest | PlanRequest | RunRequest

_REQUEST: TypeAdapter[Request] = TypeAdapter(
    Annotated[
        VersionRequest | SchemaRequest | LockRequest | PlanRequest | RunRequest,
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
    VersionResponse | SchemaResponse | LockResponse | PlanResponse | RunResponse | ErrorResponse
)


# Handling

_ERRORS: tuple[tuple[type[Exception], ErrorCode], ...] = (
    (SettingsError, ErrorCode.INVALID_SETTINGS),
    (CatalogError, ErrorCode.CATALOG_LOCK),
    (RegistryError, ErrorCode.ADAPTER_MISSING),
    (SecretError, ErrorCode.UNRESOLVED_REFERENCE),
    (EngineError, ErrorCode.ENGINE_FAILED),
    (ImageError, ErrorCode.ENGINE_FAILED),
    (AnonymizeError, ErrorCode.ANONYMIZATION_FAILED),
    (StoreError, ErrorCode.STORE_FAILED),
    (RunError, ErrorCode.RUN_FAILED),
)
_HANDLED: tuple[type[Exception], ...] = (SpecError, PlanError, *(kind for kind, _ in _ERRORS))


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


def handle(request: Request, registry: Registry | None = None) -> Response:
    """Carry out a parsed request with the installed adapters (or ``registry``)."""
    try:
        match request:
            case VersionRequest():
                return VersionResponse(version=__version__, python=platform.python_version())
            case SchemaRequest():
                return SchemaResponse(schema=spec_schema())
            case LockRequest():
                return _write_lock(request, registry or installed())
            case PlanRequest():
                return _plan(request, registry or installed())
            case RunRequest():
                return _run(request, registry or installed())
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
