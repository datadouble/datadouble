# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Error messages that are safe to show and log.

Oracle messages can quote the data that caused them: a value too large for a
column, and since release 23 a second line naming the row's key values
(``row with column values (ID:1) already exists``). Messages leaving this
adapter keep the first line, with quoted strings and column values redacted,
and only the error codes of the lines after it; identifiers in double quotes
name objects rather than data and are kept.
"""

import re

from datadouble.engine import EngineError
from datadouble_oracle.driver import Error

_QUOTED = re.compile(r"'[^']*'")
_VALUES = re.compile(r"(column values|values) \(.*?\)", re.IGNORECASE)
_CODE = re.compile(r"\b(?:ORA|DPY|PLS|TNS)-\d{4,5}\b")


def redact(text: str) -> str:
    """The first line of ``text``, redacted, and the error codes of the lines after it.

    Quoted strings and column values become ``…``; the later lines are reduced
    to their codes (such as ``ORA-27040``), which say what failed without data.
    """
    lines = text.strip().splitlines()
    if not lines:
        return ""
    first = _VALUES.sub(r"\1 (…)", _QUOTED.sub("'…'", lines[0]))
    codes = [code for line in lines[1:] for code in _CODE.findall(line)]
    later = [code for code in dict.fromkeys(codes) if code not in first]
    return f"{first} (also {', '.join(later)})" if later else first


def database_error(error: Error, action: str) -> EngineError:
    """An :class:`EngineError` for a failed database operation."""
    arguments: tuple[object, ...] = error.args
    detail = arguments[0] if arguments else None
    message = getattr(detail, "message", None)
    if isinstance(message, str) and message:
        return EngineError(f"{action} failed: {redact(message)}")
    return EngineError(f"{action} failed: {type(error).__name__}")
