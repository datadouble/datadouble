# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Error messages that are safe to show and log.

SQL Server messages can quote the data that caused them (``The duplicate key
value is (x)``, ``converting the varchar value 'x'``). Messages leaving this
adapter keep the error number and the message with data values hidden;
quoted object names and identifiers in brackets are kept (decisions 54 and
139). The driver's own trailer is dropped.
"""

import pymssql

from datadouble.engine import EngineError
from datadouble.redaction import Redactor, kinds
from datadouble.runtime import runtime_failure

_TRAILER = "DB-Lib error message"

# The shapes in which SQL Server reports data values; the ``value`` group is
# hidden. Message texts are from sys.messages, named by number; a new one is
# one more line. Any other quoted string that no kind word names is hidden
# anyway. Truncation without a value (8152), NULL into a column (515) and
# conflicts with a constraint (547) name only objects.
_VALUES = (
    r"duplicate key value is \((?P<value>.*)\)",  # 2627, 2601
    r"when converting the [\w ]+? value '(?P<value>.*)' to data type",  # 245
    r"conversion of the [\w ]+? value '(?P<value>.*)' overflowed",  # 248
    r"Truncated value: '(?P<value>.*)'",  # 2628
    r"character string '(?P<value>.*)'",  # 105, 102: the statement, literals and all
)
# Words after which a quoted string names an object: ``column 'email'``,
# ``Invalid object name 'dbo.orders'``.
_NAMES = (
    r"\b(?:object|column)\s+name\s+",
    kinds(
        "table column constraint index object database schema login user role"
        " procedure function trigger view type statistics file partition"
    ),
)

# A message a user raised (RAISERROR or THROW, numbers from 50000) can quote
# any value, so every quoted string in it is hidden.
_USER_ERRORS = 50000
_RAISED = (r"\bMsg (?:[5-9]\d{4}|\d{6,})\b",)

redact = Redactor(values=_VALUES, names=_NAMES, marks="'\"", raised=_RAISED)
"""Hide the data values in a SQL Server message, keeping the objects it names."""


def database_error(error: pymssql.Error, action: str) -> EngineError:
    """An :class:`EngineError` for a failed database operation."""
    arguments: tuple[object, ...] = error.args
    if len(arguments) > 1 and isinstance(arguments[0], int):
        raw = arguments[1]
        detail = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        detail = detail.split(_TRAILER, 1)[0].strip()
        raised = arguments[0] >= _USER_ERRORS
        return EngineError(f"{action} failed: [{arguments[0]}] {redact(detail, raised=raised)}")
    return EngineError(f"{action} failed: {type(error).__name__}")


def tool_error(tool: str, status: int, output: str) -> EngineError:
    """An :class:`EngineError` for a failed SQL Server program in a container.

    Its output is redacted, unless the status says the container runtime
    failed before the program ran (:func:`~datadouble.runtime.runtime_failure`).
    """
    keep = runtime_failure(status)
    raised = redact.raises(output)
    lines = [
        line.strip() if keep else redact(line.strip(), raised=raised)
        for line in output.splitlines()
        if line.strip()
    ]
    summary = "; ".join(lines[-3:]) or "no output"
    return EngineError(f"{tool} exited with status {status}: {summary}")
