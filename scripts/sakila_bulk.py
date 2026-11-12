# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

r"""Convert jOOQ's Sakila insert script into one bulk-load file per table.

Run once, when the fixture changes; the output is committed, so test data
stays constant::

    uv run scripts/sakila_bulk.py mysql-sakila-insert-data.sql dev/sakila/data

Each ``<table>.tsv`` starts with a header line of column names, then one row
per line: fields separated by tabs, ``\\N`` for NULL, and backslash escapes
for backslash, tab, newline and carriage return. That is the text format of
MySQL's ``LOAD DATA`` and PostgreSQL's ``COPY``; the other engines' loaders
are pointed at it. ``tables.txt`` lists the tables in the order the script
inserts them, with their row counts.
"""

import re
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

_STATEMENT = re.compile(
    r"Insert into (\w+)\s*\(([^)]*)\)\s*Values\s*\((.*?)\)\s*;\s*$",
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)
_ESCAPES = {"\\": "\\\\", "\t": "\\t", "\n": "\\n", "\r": "\\r"}


def values(text: str) -> Iterator[str | None]:
    """The values of one ``VALUES (...)`` list: quoted strings, or unquoted NULL."""
    position = 0
    while position < len(text):
        while position < len(text) and text[position] in " ,\n\r\t":
            position += 1
        if position >= len(text):
            return
        if text.startswith("NULL", position):
            yield None
            position += 4
            continue
        if text[position] != "'":
            message = f"unexpected value at {text[position : position + 20]!r}"
            raise ValueError(message)
        position += 1
        chunk: list[str] = []
        while True:
            end = text.index("'", position)
            chunk.append(text[position:end])
            if text.startswith("''", end):
                chunk.append("'")
                position = end + 2
            else:
                position = end + 1
                break
        yield "".join(chunk)


def field(value: str | None) -> str:
    """One field in the bulk format."""
    if value is None:
        return "\\N"
    return "".join(_ESCAPES.get(character, character) for character in value)


def convert(script: str, output: Path) -> dict[str, int]:
    """Write one file per table; return the row count of each, in insert order."""
    output.mkdir(parents=True, exist_ok=True)
    rows: dict[str, list[str]] = {}
    for match in _STATEMENT.finditer(script):
        table, columns, row = match.groups()
        names = [name.strip().strip("`") for name in columns.split(",")]
        lines = rows.setdefault(table, ["\t".join(names)])
        if lines[0] != "\t".join(names):
            message = f"{table}: the column list changes between statements"
            raise ValueError(message)
        found = [field(value) for value in values(row)]
        if len(found) != len(names):
            message = f"{table}: {len(found)} values for {len(names)} columns"
            raise ValueError(message)
        lines.append("\t".join(found))
    for table, lines in rows.items():
        (output / f"{table}.tsv").write_text("\n".join(lines) + "\n", "utf-8")
    counts = {table: len(lines) - 1 for table, lines in rows.items()}
    listing = "".join(f"{table}\t{count}\n" for table, count in counts.items())
    (output / "tables.txt").write_text(listing, "utf-8")
    return counts


def main(argv: Sequence[str]) -> int:
    """Convert ``argv[0]`` (the insert script) into ``argv[1]``."""
    script, output = argv
    for table, count in convert(Path(script).read_text("utf-8"), Path(output)).items():
        sys.stdout.write(f"{table}: {count}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
