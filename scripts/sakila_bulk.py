# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

r"""Convert jOOQ's Sakila insert script into one bulk-load file per table.

Run once, when the fixture changes; the output is committed, so test data
stays constant::

    uv run scripts/sakila_bulk.py

It downloads the insert script from jOOQ's collection at the pinned commit,
checks its SHA-256, and writes into ``dev/sakila/data``.

Each ``<table>.tsv`` starts with a header line of column names, then one row
per line: fields separated by tabs, ``\\N`` for NULL, and backslash escapes
for backslash, tab, newline and carriage return. That is the text format of
MySQL's ``LOAD DATA`` and PostgreSQL's ``COPY``; the other engines' loaders
are pointed at it. ``tables.txt`` lists the tables in the order the script
inserts them, with their row counts.
"""

import hashlib
import re
import sys
import urllib.request
from collections.abc import Iterator
from pathlib import Path

SOURCE_SHA256 = "353ef858e4d2d1a60549969283434da15b905aef3ca7099d82b649b75f8de99f"
OUTPUT = Path(__file__).resolve().parent.parent / "dev" / "sakila" / "data"

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


def download() -> bytes:
    """The MySQL Sakila insert script from jOOQ's collection, at the pinned commit."""
    with urllib.request.urlopen(
        "https://raw.githubusercontent.com/jOOQ/sakila/e089a5b1ec9af0df7a9c6a5d47d49fa1736a4e84/"
        "mysql-sakila-db/mysql-sakila-insert-data.sql",
        timeout=60,
    ) as response:
        data: bytes = response.read()
    return data


def main() -> int:
    """Download, check and convert the pinned insert script into ``OUTPUT``."""
    data = download()
    digest = hashlib.sha256(data).hexdigest()
    if digest != SOURCE_SHA256:
        sys.stdout.write(f"the insert script's SHA-256 is {digest}, not {SOURCE_SHA256}\n")
        return 1
    for table, count in convert(data.decode("utf-8"), OUTPUT).items():
        sys.stdout.write(f"{table}: {count}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
