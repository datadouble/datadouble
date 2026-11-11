# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The JSON interface.

Every Datadouble operation is a JSON request answered by a JSON response. The
command-line interface, the CI/CD adapters and the agent skills only build
requests for this module, so behavior cannot differ between them.
"""

import platform
from enum import IntEnum, StrEnum
from typing import Literal, assert_never

from pydantic import BaseModel, ConfigDict, ValidationError

from datadouble import __version__


class Command(StrEnum):
    """Every command the JSON interface accepts."""

    VERSION = "version"


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


class _Message(BaseModel):
    """Base for every request and response: strict, immutable, no extra fields."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class VersionRequest(_Message):
    """Ask for the installed Datadouble version."""

    command: Literal[Command.VERSION]


type Request = VersionRequest


class VersionResponse(_Message):
    """The installed Datadouble version and the Python running it."""

    command: Literal[Command.VERSION] = Command.VERSION
    outcome: Literal[Outcome.SUCCESS] = Outcome.SUCCESS
    version: str
    python: str


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


type Response = VersionResponse | ErrorResponse


def handle(request: Request) -> Response:
    """Carry out a parsed request."""
    match request:
        case VersionRequest():
            return VersionResponse(version=__version__, python=platform.python_version())
        case _:
            assert_never(request)


def parse_request(raw: str | bytes) -> Request | ErrorResponse:
    """Parse a JSON request, or explain why it is not a valid one."""
    try:
        return VersionRequest.model_validate_json(raw)
    except ValidationError as error:
        details = tuple(
            ErrorDetail(
                location=".".join(str(part) for part in item["loc"]) or "(request)",
                problem=item["msg"],
            )
            for item in error.errors(include_url=False)
        )
        return ErrorResponse(
            error=ErrorCode.INVALID_REQUEST,
            message="The request is not a valid Datadouble request.",
            details=details,
        )


def handle_json(raw: str | bytes) -> Response:
    """Parse and carry out a JSON request."""
    parsed = parse_request(raw)
    if isinstance(parsed, ErrorResponse):
        return parsed
    return handle(parsed)


def exit_code(response: Response) -> ExitCode:
    """The process exit code for a response."""
    match response.outcome:
        case Outcome.SUCCESS:
            return ExitCode.SUCCESS
        case Outcome.FAILURE:
            return ExitCode.FAILURE
        case _:
            assert_never(response.outcome)
