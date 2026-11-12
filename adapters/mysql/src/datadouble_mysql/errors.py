# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Error messages that are safe to show and log.

MySQL messages can quote the data that caused them (``Duplicate entry 'x'
for key``, a value that does not fit). Messages leaving this adapter keep the
error number and the message with quoted strings redacted; identifiers in
backticks are kept, since they name objects rather than data.
"""

import re

import pymysql

from datadouble.engine import EngineError

_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_NOISE = ("mysql: [Warning]", "mysqldump: [Warning]", "Warning:")


def redact(text: str) -> str:
    """Replace quoted strings, which may hold data values, with ``'…'``."""
    return _QUOTED.sub("'…'", text)


def database_error(error: pymysql.MySQLError, action: str) -> EngineError:
    """An :class:`EngineError` for a failed database operation."""
    arguments: tuple[object, ...] = error.args
    code = f"[{arguments[0]}] " if len(arguments) > 1 else ""
    detail = str(arguments[1]) if len(arguments) > 1 else type(error).__name__
    return EngineError(f"{action} failed: {code}{redact(detail)}")


def tool_error(tool: str, status: int, stderr: str) -> EngineError:
    """An :class:`EngineError` for a failed MySQL command-line tool."""
    lines = [
        redact(line.strip())
        for line in stderr.splitlines()
        if line.strip() and not line.strip().startswith(_NOISE)
    ]
    summary = "; ".join(lines[-3:]) or "no output"
    return EngineError(f"{tool} exited with status {status}: {summary}")
