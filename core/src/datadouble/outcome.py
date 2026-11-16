# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""How an operation ended, and the exit codes that say so."""

from enum import IntEnum, StrEnum


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
