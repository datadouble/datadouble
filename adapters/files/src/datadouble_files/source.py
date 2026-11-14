# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The source side: the tree's catalog, and the copy of the chosen files.

A file tree has no transactions. A consistent copy would need a filesystem
snapshot (btrfs, ZFS or LVM), which this version does not take, so the
extract runs only when the spec accepts an inconsistent copy
(``accept_inconsistent: true``), and the manifest says so: each file is
copied as it is when read.
"""

import datetime as dt
import shutil
import time
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from datadouble.catalog import Catalog
from datadouble.engine import EngineError, ExtractResult, Snapshot, TableCount, Tooling
from datadouble.names import ColumnName, TableName
from datadouble.planner import Plan
from datadouble.subset import Condition, KeyTuple
from datadouble_files.tree import PATH, catalog, chosen

TREE = "tree"
"""The directory of an extract, working copy or image that holds the files."""
VERSION = "1"
"""The version of the tree's layout, which images record as their server version."""
INCONSISTENT = "none: each file as it was when copied"


class Extracted(BaseModel):
    """What an extract holds, for ``load``."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    tables: tuple[TableName, ...]


class FilesSnapshot:
    """Reads of the live tree; not a snapshot, as the spec accepted."""

    def __init__(self, root: Path, as_of: dt.datetime) -> None:
        """Read ``root``; ``as_of`` is bound where find tests say ``:as_of``."""
        self.root = root
        self.as_of = as_of
        self.opened = time.monotonic()

    def project(
        self, table: TableName, condition: Condition, columns: tuple[ColumnName, ...]
    ) -> Iterable[KeyTuple]:
        """The chosen files' paths; files have no other key."""
        if columns != (PATH,):
            message = f"files are chosen by path, not {', '.join(columns)}"
            raise EngineError(message)
        return {(path,) for path in chosen(self.root, table, condition, self.as_of)}

    def extract(
        self,
        plan: Plan,
        selections: Mapping[TableName, Condition],
        directory: Path,
        tooling: Tooling,  # noqa: ARG002 - the copy needs no containers
    ) -> ExtractResult:
        """Copy the chosen files, keeping their paths and times."""
        current = catalog(self.root)
        if current.fingerprint() != plan.catalog_fingerprint:
            message = "the tree's top-level directories changed since planning; plan again"
            raise EngineError(message)
        counts: list[TableCount] = []
        for table in plan.extracted():
            paths = sorted(chosen(self.root, table, selections[table], self.as_of))
            for path in paths:
                target = directory / TREE / path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.root / path, target)
            (directory / TREE / table.name).mkdir(parents=True, exist_ok=True)
            counts.append(TableCount(table=table, rows=len(paths)))
        index = Extracted(tables=plan.extracted())
        (directory / "index.json").write_text(index.model_dump_json(indent=2), "utf-8")
        return ExtractResult(
            server_version=VERSION,
            snapshot=INCONSISTENT,
            snapshot_seconds=round(time.monotonic() - self.opened, 3),
            counts=tuple(counts),
        )


class FilesSource:
    """A tree on a path Datadouble can read."""

    def __init__(self, root: Path, *, accept_inconsistent: bool) -> None:
        """Read the tree at ``root``."""
        self.root = root
        self.accept_inconsistent = accept_inconsistent

    def catalog(self) -> Catalog:
        """The top-level directories."""
        return catalog(self.root)

    @contextmanager
    def snapshot(self, as_of: dt.datetime) -> Generator[Snapshot]:
        """Reads of the live tree, if the spec accepts an inconsistent copy."""
        if not self.accept_inconsistent:
            message = (
                "a consistent copy of a file tree needs a filesystem snapshot, which this"
                " version does not take; set `accept_inconsistent: true` in the files"
                " connection to copy each file as it is when read"
            )
            raise EngineError(message)
        yield FilesSnapshot(self.root, as_of)
