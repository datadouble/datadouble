# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Error messages that are safe to show and log.

PostgreSQL messages can quote the data that caused them (a duplicate key, a
value that does not parse). Messages leaving this adapter keep the SQLSTATE,
the primary message with data values hidden, and the names of the objects
involved, which are identifiers rather than data (decisions 54 and 139).
"""

import psycopg

from datadouble.engine import EngineError
from datadouble.redaction import Redactor, kinds
from datadouble.runtime import runtime_failure

_NOISE = ("DETAIL:", "CONTEXT:", "HINT:", "LINE ", "QUERY:", "STATEMENT:")

# The shapes in which PostgreSQL reports data values; the ``value`` group is
# hidden. Message texts are from the server's sources (src/backend), named
# below; a new one is one more line. Values are matched greedily, so one
# holding a quote mark is hidden whole. Any other quoted string that no kind
# word names is hidden anyway.
_VALUES = (
    # utils/adt: numutils.c, int8.c, float.c, uuid.c, bool.c, enum.c, json.c...
    r'invalid input syntax for type [^:]+: "(?P<value>.*)"',
    r'invalid input value for enum [^:]+: "(?P<value>.*)"',
    r'value "(?P<value>.*)" is out of range for type',
    # utils/adt/datetime.c, formatting.c
    r'(?:date/time field value|time zone displacement) out of range: "(?P<value>.*)"',
    r'invalid value "(?P<value>.*)" for',
    # utils/adt/arrayfuncs.c, rowtypes.c, rangetypes.c, multirangetypes.c
    r'malformed (?:array|record|range|multirange) literal: "(?P<value>.*)"',
    # utils/adt/jsonfuncs.c, a DETAIL line
    r'Token "(?P<value>.*)" is invalid',
    # utils/mb/mbutils.c: the bytes are the data's
    r"invalid byte sequence for encoding \"[^\"]*\": (?P<value>.*)",
    r"character with byte sequence (?P<value>.*?) in encoding",
    # access/nbtree/nbtinsert.c, utils/adt/ri_triggers.c, catalog/index.c,
    # executor/execIndexing.c, DETAIL lines naming the key's columns and values
    (
        r"Key \(.*?\)=\((?P<value>.*)\) (?:already exists|is not present in table"
        r"|is still referenced from table|is duplicated|conflicts with existing key)"
    ),
    r"existing key \(.*?\)=\((?P<value>.*)\)",
    # executor/execMain.c, a DETAIL line with the whole row
    r"Failing row contains \((?P<value>.*)\)",
    # commands/copyfrom.c, a CONTEXT line
    r'COPY [^,]+, line \d+, column [^:]+: "(?P<value>.*)"',
    r'COPY [^,]+, line \d+: "(?P<value>.*)"',
)
# Words after which a quoted string names an object: ``relation "orders"``.
_NAMES = (
    kinds(
        "relation table column constraint index sequence view schema database role"
        " user extension type domain function procedure aggregate operator trigger"
        " rule policy collation conversion encoding language tablespace publication"
        " subscription slot server parameter statistics attribute file directory"
    ),
)

# A message a user raised (RAISE in PL/pgSQL, SQLSTATE P0001; plpy.error in
# PL/Python, XX000; die in PL/Perl, 38000; error in PL/Tcl) can quote any
# value, so every quoted string in it is hidden. Its context names the
# language (``PL/Python function "f"``), and psql and pg_restore print it.
_RAISED = (r"\bP0001\b", r"\bPL/\w+")

redact = Redactor(values=_VALUES, names=_NAMES, marks="\"'", raised=_RAISED)
"""Hide the data values in a PostgreSQL message, keeping the objects it names."""


def database_error(error: psycopg.Error, action: str) -> EngineError:
    """An :class:`EngineError` for a failed database operation."""
    diag = error.diag
    raised = diag.sqlstate == "P0001" or redact.raises(diag.context or "")
    primary = redact(diag.message_primary or type(error).__name__, raised=raised)
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
    """An :class:`EngineError` for a failed PostgreSQL command-line tool.

    Its output is redacted, unless the status says the container runtime
    failed before the program ran (:func:`~datadouble.runtime.runtime_failure`).
    """
    keep = runtime_failure(status)
    raised = redact.raises(stderr)
    lines = [
        line.strip() if keep else redact(line.strip(), raised=raised)
        for line in stderr.splitlines()
        if line.strip() and not line.strip().startswith(_NOISE)
    ]
    summary = "; ".join(lines[-3:]) or "no output"
    return EngineError(f"{tool} exited with status {status}: {summary}")
