# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Error messages that are safe to show and log.

Oracle messages can quote the data that caused them: a value too large for a
column, and since release 23 a second line naming the row's key values
(``row with column values (ID:1) already exists``). Messages leaving this
adapter keep the error code and the first line only, with quoted strings and
column values redacted; identifiers in double quotes name objects rather than
data and are kept.
"""

import re

from datadouble.engine import EngineError
from datadouble_oracle.driver import Error

_QUOTED = re.compile(r"'[^']*'")
_VALUES = re.compile(r"(column values|values) \(.*?\)", re.IGNORECASE)


def redact(text: str) -> str:
    """The first line of ``text``, with quoted strings and column values replaced by ``…``."""
    first = text.strip().splitlines()[0] if text.strip() else ""
    return _VALUES.sub(r"\1 (…)", _QUOTED.sub("'…'", first))


def database_error(error: Error, action: str) -> EngineError:
    """An :class:`EngineError` for a failed database operation."""
    arguments: tuple[object, ...] = error.args
    detail = arguments[0] if arguments else None
    message = getattr(detail, "message", None)
    if isinstance(message, str) and message:
        return EngineError(f"{action} failed: {redact(message)}")
    return EngineError(f"{action} failed: {type(error).__name__}")
