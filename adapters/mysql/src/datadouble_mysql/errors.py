# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Error messages that are safe to show and log.

MySQL messages can quote the data that caused them (``Duplicate entry 'x'
for key``, a value that does not fit). Messages leaving this adapter keep the
error number and the message with data values hidden; quoted object names and
identifiers in backticks are kept (decisions 54 and 139).
"""

import re

import pymysql

from datadouble.engine import EngineError
from datadouble.redaction import Redactor, kinds
from datadouble.runtime import runtime_failure

_NOISE = ("mysql: [Warning]", "mysqldump: [Warning]", "Warning:")

# The shapes in which MySQL reports data values; the ``value`` group is
# hidden. Message texts are the server's (share/messages_to_clients.txt),
# named by error; a new one is one more line. Any other quoted string that no
# kind word names is hidden anyway. Data too long (1406), data truncated
# (1265) and out of range (1264) name only the column.
_VALUES = (
    r"Duplicate entry '(?P<value>.*)' for key",  # ER_DUP_ENTRY 1062
    # ER_TRUNCATED_WRONG_VALUE_FOR_FIELD 1366, ER_WRONG_VALUE 1525
    r"Incorrect [\w ]+? value: '(?P<value>.*)'(?= for column|$)",
    r"Truncated incorrect [\w ]+? value: '(?P<value>.*)'$",  # ER_TRUNCATED_WRONG_VALUE 1292
    r"can't be set to the value of '(?P<value>.*)'$",  # ER_WRONG_VALUE_FOR_VAR 1231
    r"Invalid \w+ character string: '(?P<value>.*)'$",  # ER_INVALID_CHARACTER_STRING 1300
    r"Cannot convert string '(?P<value>.*)' from",  # ER_CANNOT_CONVERT_STRING 3854
    r"near '(?P<value>.*)' at line \d+",  # ER_PARSE_ERROR 1064: the statement, literals and all
)
# What says a quoted string names an object: an account ('app'@'%'), a kind
# word (``column 'email'``), or a clause (``in 'field list'``).
_KINDS = (
    "table column key index constraint database schema user role procedure"
    " function trigger view event file directory tablespace partition"
    " plugin component engine server"
)
_NAMES = (
    rf"{kinds('user role')}'[^']*'@",
    kinds(_KINDS),
    r"\bin\s+(?='[\w ]+ (?:list|clause|statement)')",
)

# A message a user raised (SIGNAL or RESIGNAL) can quote any value after any
# word, so every quoted string in it is hidden. A trigger can SIGNAL with any
# error number, a real one such as 1062 included, and pymysql drops the
# SQLSTATE, so names are kept only in a message that matches, whole, the
# server's own text for its number; any other has every quoted string
# hidden. The texts are from share/messages_to_clients.txt; a quoted name is
# '[^']*', a quoted value '.*'.
_NAME = "'[^']*'"
_ACCOUNT = f"{_NAME}@{_NAME}"
_TEMPLATES = {
    1044: f"Access denied for user {_ACCOUNT} to database {_NAME}",
    1045: rf"Access denied for user {_ACCOUNT} \(using password: (?:YES|NO)\)",
    1048: f"Column {_NAME} cannot be null",
    1049: f"Unknown database {_NAME}",
    1050: f"Table {_NAME} already exists",
    1051: f"Unknown table {_NAME}",
    1054: f"Unknown column {_NAME} in {_NAME}",
    1062: f"Duplicate entry '.*' for key {_NAME}",
    1064: (
        r"You have an error in your SQL syntax; check the manual that corresponds to your"
        r" (?:MySQL|MariaDB) server version for the right syntax to use near '.*' at line \d+"
    ),
    1142: rf"[\w ]+ command denied to user {_ACCOUNT} for table {_NAME}",
    1143: rf"[\w ]+ command denied to user {_ACCOUNT} for column {_NAME} in table {_NAME}",
    1146: f"Table {_NAME} doesn't exist",
    1231: f"Variable {_NAME} can't be set to the value of '.*'",
    1264: rf"Out of range value for column {_NAME} at row \d+",
    1265: rf"Data truncated for column {_NAME} at row \d+",
    1292: r"Truncated incorrect [\w ]+ value: '.*'",
    1300: r"Invalid \w+ character string: '.*'",
    1366: rf"Incorrect [\w ]+ value: '.*' for column {_NAME} at row \d+",
    1406: rf"Data too long for column {_NAME} at row \d+",
    1451: r"Cannot delete or update a parent row: a foreign key constraint fails \([^']*\)",
    1452: r"Cannot add or update a child row: a foreign key constraint fails \([^']*\)",
    1525: r"Incorrect [\w ]+ value: '.*'",
    3819: f"Check constraint {_NAME} is violated\\.",
    3854: r"Cannot convert string '.*' from \w+ to \w+",
}
_KNOWN = {code: re.compile(text, re.DOTALL) for code, text in _TEMPLATES.items()}
# The client's line for a server error: ERROR 1062 (23000) at line 1: ...
_CLIENT = re.compile(r"ERROR (?P<code>\d+) \(\w+\)(?: at line \d+)?[^:]*: (?P<message>.*)")
# mysqldump's: Got error: 1146: Table 'shop.t' doesn't exist when using LOCK TABLES
_DUMP = re.compile(r"Got error: (?P<code>\d+): (?P<message>.*?)(?: when [\w ]+)?$")


def _error_line(line: str) -> re.Match[str] | None:
    """The code and message of a client's or mysqldump's line for a server error."""
    return _CLIENT.search(line) or _DUMP.search(line)


# SIGNAL's SQLSTATE class 45 and error 1644, as the client prints them.
_RAISED = (r"\b1644\b", r"\(45\d{3}\)")

redact = Redactor(values=_VALUES, names=_NAMES, marks="'\"", raised=_RAISED)
"""Hide the data values in a MySQL message, keeping the objects it names."""


def genuine(code: object, message: str) -> bool:
    """Whether ``message`` is the server's own text for error ``code``, not a user's."""
    known = _KNOWN.get(code) if isinstance(code, int) else None
    return known is not None and known.fullmatch(message.strip()) is not None


def _raised_in_client(output: str) -> bool:
    """Whether the client's output holds an error a user may have raised.

    Each ``ERROR n (state)`` line, and mysqldump's ``Got error: n:`` line
    (with its trailing ``when ...``), must be the server's own text for ``n``,
    and the whole of its error: a further line before the next one could be
    a user's text continuing it.
    """
    lines = [
        line.strip()
        for line in output.splitlines()
        if line.strip() and not line.strip().startswith(_NOISE)
    ]
    for index, line in enumerate(lines):
        match = _error_line(line)
        if match is None:
            continue
        rest = lines[index + 1 :]
        follows = bool(rest) and _error_line(rest[0]) is None
        if follows or not genuine(int(match["code"]), match["message"]):
            return True
    return False


def database_error(error: pymysql.MySQLError, action: str) -> EngineError:
    """An :class:`EngineError` for a failed database operation."""
    arguments: tuple[object, ...] = error.args
    code = f"[{arguments[0]}] " if len(arguments) > 1 else ""
    detail = str(arguments[1]) if len(arguments) > 1 else type(error).__name__
    raised = len(arguments) > 1 and not genuine(arguments[0], detail)
    return EngineError(f"{action} failed: {code}{redact(detail, raised=raised)}")


def tool_error(tool: str, status: int, stderr: str) -> EngineError:
    """An :class:`EngineError` for a failed MySQL command-line tool.

    Its output is redacted, unless the status says the container runtime
    failed before the program ran (:func:`~datadouble.runtime.runtime_failure`).
    """
    keep = runtime_failure(status)
    raised = redact.raises(stderr) or _raised_in_client(stderr)
    lines = [
        line.strip() if keep else redact(line.strip(), raised=raised)
        for line in stderr.splitlines()
        if line.strip() and not line.strip().startswith(_NOISE)
    ]
    summary = "; ".join(lines[-3:]) or "no output"
    return EngineError(f"{tool} exited with status {status}: {summary}")
