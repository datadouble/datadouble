# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""A file tree as tables: the catalog, and the files a condition chooses.

Each top-level directory is a table in the ``files`` namespace. Its rows are
the regular files under it, keyed by ``path``, the file's path from the
tree's root (``documents/42/lease.pdf``). Seeds and assertions are ``find``
tests, run with GNU find on the directory: ``-mtime -30 -size -10M``. Tests
that act (``-exec``, ``-delete``, the printing ones) or follow symbolic links
are refused, and only regular files are ever chosen, so nothing outside the
tree is read.
"""

import datetime as dt
import os
import shlex
from collections.abc import Iterable, Iterator
from pathlib import Path, PurePosixPath
from typing import assert_never

from datadouble.catalog import Catalog, Column, ColumnKind, Table
from datadouble.engine import EngineError
from datadouble.names import ColumnName, EngineName, TableName
from datadouble.programs import locate, run_program
from datadouble.subset import (
    AllOf,
    AllRows,
    AnyNull,
    AnyOf,
    Condition,
    NoRows,
    Predicate,
    SelectedBy,
    ValuesIn,
)

ENGINE = EngineName("files")
AS_OF = ":as_of"
NAMESPACE = "files"
PATH = ColumnName("path")
CONTENT = ColumnName("content")
_REFUSED = frozenset(
    {
        "-exec",
        "-execdir",
        "-ok",
        "-okdir",
        "-delete",
        "-fls",
        "-fprint",
        "-fprint0",
        "-fprintf",
        "-ls",
        "-print",
        "-print0",
        "-printf",
        "-quit",
        "-follow",
        "-files0-from",
    }
)
_COLUMNS = (
    Column(name=PATH, type="path", kind=ColumnKind.TEXT, nullable=False),
    Column(name=ColumnName("size"), type="bytes", kind=ColumnKind.INTEGER, nullable=False),
    Column(name=CONTENT, type="bytes", kind=ColumnKind.TEXT, nullable=False),
)
"""Every table's columns: ``content`` is what the PII scan reads, the text in each file."""


def table_name(directory: str) -> TableName:
    """The table of a top-level directory."""
    return TableName.of(NAMESPACE, directory)


def catalog(root: Path) -> Catalog:
    """One table per top-level directory; fails if the root holds files of its own."""
    if not root.is_dir():
        message = f"the files root {root} is not a directory"
        raise EngineError(message)
    entries = sorted(os.scandir(root), key=lambda entry: entry.name)
    loose = [entry.name for entry in entries if not entry.is_dir(follow_symlinks=False)]
    if loose:
        message = (
            f"the files root holds {len(loose)} entries that are not directories; every file"
            " must be in a top-level directory, which is its table"
        )
        raise EngineError(message)
    tables = tuple(
        Table(name=table_name(entry.name), columns=_COLUMNS, primary_key=(PATH,))
        for entry in entries
    )
    return Catalog(engine=ENGINE, tables=tables)


def find_tests(text: str, as_of: dt.datetime) -> list[str]:
    """``find`` tests from a seed or assertion; refuses actions and symbolic-link following.

    The word ``:as_of`` stands for the plan's anchor time, as in ``-newermt :as_of``.
    """
    try:
        words = [as_of.isoformat() if word == AS_OF else word for word in shlex.split(text)]
    except ValueError as error:
        message = f"cannot read the find tests {text!r}: {error}"
        raise EngineError(message) from None
    refused = sorted({word for word in words if word in _REFUSED})
    if refused:
        message = f"find tests may only test files; {', '.join(refused)} is not allowed"
        raise EngineError(message)
    return words


def find(directory: Path, words: list[str]) -> set[str]:
    """The regular files under ``directory`` the tests choose, relative to it."""
    program = locate("find", "DATADOUBLE_FIND")
    if program is None:
        message = "GNU find was not found"
        raise EngineError(message)
    chosen = ["(", *words, ")"] if words else []
    done = run_program(
        program, [str(directory), "-mindepth", "1", *chosen, "-type", "f", "-printf", "%P\\0"]
    )
    if done.status != 0:
        detail = done.stderr.strip().splitlines()[-1:] or [f"status {done.status}"]
        message = f"find failed: {detail[0]}"
        raise EngineError(message)
    return {path for path in done.stdout.split("\0") if path}


def relative(path: str) -> PurePosixPath | None:
    """A path from ``paths_from`` as a path in the tree, or None if it leaves the tree."""
    parts = PurePosixPath(path.lstrip("/")).parts
    if not parts or any(part in {"..", ""} for part in parts):
        return None
    return PurePosixPath(*(part for part in parts if part != "."))


def chosen(root: Path, table: TableName, condition: Condition, as_of: dt.datetime) -> set[str]:
    """The paths, from the root, of the files in ``table`` the condition chooses."""
    directory = root / table.name
    match condition:
        case AllRows():
            return {f"{table.name}/{path}" for path in find(directory, [])}
        case NoRows() | AnyNull():
            # Every file has a path: none is null.
            return set()
        case Predicate(text):
            return {f"{table.name}/{path}" for path in find(directory, find_tests(text, as_of))}
        case ValuesIn(columns, values):
            if columns != (PATH,):
                message = f"files are chosen by path, not {', '.join(columns)}"
                raise EngineError(message)
            return _listed(root, table, (str(row[0]) for row in values))
        case AnyOf(parts) | AllOf(parts):
            sets = [chosen(root, table, part, as_of) for part in parts]
            if not sets:
                return set()
            first, *rest = sets
            return first.union(*rest) if isinstance(condition, AnyOf) else first.intersection(*rest)
        case SelectedBy():
            message = "file trees have no references; use the keys strategy"
            raise EngineError(message)
        case _:
            assert_never(condition)


def _listed(root: Path, table: TableName, paths: Iterable[str]) -> set[str]:
    """The listed paths that are regular files in ``table``; others are skipped."""
    found: set[str] = set()
    for path in paths:
        inside = relative(path)
        if inside is None or inside.parts[0] != table.name:
            continue
        file = root / inside
        if file.is_file() and not file.is_symlink():
            found.add(str(inside))
    return found


def files_in(root: Path) -> Iterator[tuple[str, Path]]:
    """Every regular file under ``root``, with its path from the root."""
    for directory, _, names in os.walk(root):
        for name in sorted(names):
            file = Path(directory) / name
            if file.is_file() and not file.is_symlink():
                yield str(file.relative_to(root)), file
