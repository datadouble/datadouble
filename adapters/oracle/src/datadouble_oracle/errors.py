# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Error messages that are safe to show and log.

Oracle messages can quote the data that caused them: a value that does not
convert or fit, and, where ``ERROR_MESSAGE_DETAILS`` is on, a second line
naming the row's key values (``row with column values (ID:1) already
exists``). Messages leaving this adapter keep the first line, with data
values hidden, and only the error codes of the lines after it; identifiers
in double quotes name objects rather than data and are kept (decisions 54
and 139).
"""

import re

from datadouble.engine import EngineError
from datadouble.redaction import Redactor, kinds
from datadouble_oracle.driver import Error

_CODE = re.compile(r"\b(?:ORA|DPY|PLS|TNS)-\d{4,5}\b")

# The shapes in which Oracle reports data values; the ``value`` group is
# hidden. Message texts are from Oracle's error reference
# (docs.oracle.com/en/error-help/db/ora-NNNNN), named by code; a new one is
# one more line. Any single-quoted string, a literal, is hidden anyway unless
# a kind word names it. ORA-00001 and ORA-12899 name only objects and lengths.
_VALUES = (
    r"string value containing (?P<value>.*?) to a number",  # ORA-01722
    r"invalid string value: (?P<value>.*)",  # ORA-03302, ORA-01722's detail
    r"value (?P<value>\S+) greater than specified precision",  # ORA-01438
    r"(?:column )?values \((?P<value>.*)\)",  # ORA-03301, ORA-00001's detail
)
# Words after which a single-quoted string names an object: ``file '/u01/x.dbf'``.
_NAMES = (kinds("file directory user role tablespace"),)

# A message a user raised (RAISE_APPLICATION_ERROR, ORA-20000 to ORA-20999)
# can quote any value, so every quoted string in it is hidden.
_RAISED = (r"\bORA-20\d{3}\b",)

_redact = Redactor(values=_VALUES, names=_NAMES, marks="'", raised=_RAISED)


def redact(text: str) -> str:
    """The first line of ``text``, redacted, and the error codes of the lines after it.

    Data values and literals become ``…``; the later lines are reduced
    to their codes (such as ``ORA-27040``), which say what failed without data.
    """
    lines = text.strip().splitlines()
    if not lines:
        return ""
    first = _redact(lines[0], raised=_redact.raises(text))
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
