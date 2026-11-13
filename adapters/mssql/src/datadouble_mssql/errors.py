# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Error messages that are safe to show and log.

SQL Server messages can quote the data that caused them (``The duplicate key
value is (x)``, ``converting the varchar value 'x'``). Messages leaving this
adapter keep the error number and the message with quoted strings and
parenthesized key values redacted; identifiers in brackets are kept, since
they name objects rather than data. The driver's own trailer is dropped.
"""

import re

import pymssql

from datadouble.engine import EngineError

_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_KEY_VALUE = re.compile(r"(key value is )\(.*?\)(?=\.|$)", re.DOTALL)
_TRAILER = "DB-Lib error message"


def redact(text: str) -> str:
    """Replace quoted strings and duplicate-key values, which may hold data, with ``…``."""
    return _QUOTED.sub("'…'", _KEY_VALUE.sub(r"\1(…)", text))


def database_error(error: pymssql.Error, action: str) -> EngineError:
    """An :class:`EngineError` for a failed database operation."""
    arguments: tuple[object, ...] = error.args
    if len(arguments) > 1 and isinstance(arguments[0], int):
        raw = arguments[1]
        detail = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        detail = detail.split(_TRAILER, 1)[0].strip()
        return EngineError(f"{action} failed: [{arguments[0]}] {redact(detail)}")
    return EngineError(f"{action} failed: {type(error).__name__}")


def tool_error(tool: str, status: int, output: str) -> EngineError:
    """An :class:`EngineError` for a failed SQL Server program in a container."""
    lines = [redact(line.strip()) for line in output.splitlines() if line.strip()]
    summary = "; ".join(lines[-3:]) or "no output"
    return EngineError(f"{tool} exited with status {status}: {summary}")
