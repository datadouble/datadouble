# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Error messages that are safe to show and log.

PostgreSQL messages can quote the data that caused them (a duplicate key, a
value that does not parse). Messages leaving this adapter keep the SQLSTATE,
the primary message with quoted strings redacted, and the names of the
objects involved, which are identifiers rather than data.
"""

import re

import psycopg

from datadouble.engine import EngineError

_QUOTED = re.compile(r'"[^"]*"|\'[^\']*\'')
_NOISE = ("DETAIL:", "CONTEXT:", "HINT:", "LINE ", "QUERY:", "STATEMENT:")


def redact(text: str) -> str:
    """Replace quoted strings, which may hold data values, with ``"…"``."""
    return _QUOTED.sub('"…"', text)


def database_error(error: psycopg.Error, action: str) -> EngineError:
    """An :class:`EngineError` for a failed database operation."""
    diag = error.diag
    primary = redact(diag.message_primary or type(error).__name__)
    names = [
        f"{label} {value}"
        for label, value in (
            ("schema", diag.schema_name),
            ("table", diag.table_name),
            ("column", diag.column_name),
            ("constraint", diag.constraint_name),
        )
        if value
    ]
    where = f" ({', '.join(names)})" if names else ""
    code = f"[{diag.sqlstate}] " if diag.sqlstate else ""
    return EngineError(f"{action} failed: {code}{primary}{where}")


def tool_error(tool: str, status: int, stderr: str) -> EngineError:
    """An :class:`EngineError` for a failed PostgreSQL command-line tool."""
    lines = [
        redact(line.strip())
        for line in stderr.splitlines()
        if line.strip() and not line.strip().startswith(_NOISE)
    ]
    summary = "; ".join(lines[-3:]) or "no output"
    return EngineError(f"{tool} exited with status {status}: {summary}")
