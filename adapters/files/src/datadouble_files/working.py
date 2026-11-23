# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The scratch side: working copies, cleanup, images and restores.

A working copy is the group's directories from the extract. ``transform``
copies it and runs the cleanup commands (``after_transform`` hooks) in the
copy's root, in a container from the files' image, so an interrupted
transform leaves the loaded copy untouched; column rules do not apply to
files. The image is the tree itself (``physical``).

Verification reads the copy as text: ``content`` yields every run of
printable characters in each file, which finds personal data in text files
and in the metadata binary files carry, such as a photo's EXIF fields.
"""

import datetime as dt
import re
import shlex
from collections import Counter
from collections.abc import Generator, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from datadouble.catalog import Requirement
from datadouble.engine import EngineError, RestoredImage, TableCount, Transformation, one_extract
from datadouble.files import copy_tree
from datadouble.names import ColumnRef, TableName
from datadouble.planner import Plan, PlannedGroup
from datadouble.runtime import Container, ContainerRuntime
from datadouble.spec import Hook, ImageFormat, TransformHook
from datadouble.subset import Edge
from datadouble_files.source import TREE, VERSION, Extracted
from datadouble_files.tree import CONTENT, PATH, files_in, find, find_tests

DEFAULT_IMAGE = (
    "debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2"
)
"""The image cleanup commands run in when the spec names none."""
_PRINTABLE = re.compile(rb"[\x20-\x7e]{6,}")
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)


def _copy(source: Path, destination: Path) -> None:
    problem = copy_tree(source, destination)
    if problem:
        message = f"cannot copy {source.name}: {problem}"
        raise EngineError(message)


def _counts(tree: Path, tables: Iterable[TableName]) -> tuple[TableCount, ...]:
    found = Counter(path.split("/", 1)[0] for path, _ in files_in(tree))
    return tuple(TableCount(table=table, rows=found[table.name]) for table in tables)


def text_runs(file: Path) -> Iterator[str]:
    """The runs of six or more printable ASCII characters in a file."""
    for run in _PRINTABLE.findall(file.read_bytes()):
        yield run.decode("ascii")


class FilesRestored:
    """A tree open for checks: a restored image, or a working copy."""

    def __init__(self, tree: Path) -> None:
        """Check the tree at ``tree``."""
        self.tree = tree

    def counts(self, tables: Iterable[TableName]) -> dict[TableName, int]:
        """Files per top-level directory."""
        return {count.table: count.rows for count in _counts(self.tree, tables)}

    def orphans(self, edge: Edge) -> int:
        """File trees have no references to check."""
        message = f"{edge.name}: file trees have no references"
        raise EngineError(message)

    def text_values(self, column: ColumnRef) -> Iterator[str]:
        """The paths, or the text in the files, of one top-level directory."""
        directory = self.tree / column.table.name
        for path, file in files_in(directory):
            if column.column == PATH:
                yield f"{column.table.name}/{path}"
            elif column.column == CONTENT:
                yield from text_runs(file)

    def failing_rows(self, query: str) -> int:
        """Files the find tests in ``query`` choose anywhere in the tree."""
        return len(find(self.tree, find_tests(query, _EPOCH)))


class FilesWorking:
    """Working copies and images under one scratch directory."""

    def __init__(self, runtime: ContainerRuntime, image: str, scratch: Path) -> None:
        """Run cleanup commands with ``runtime`` in ``image``."""
        self.runtime = runtime
        self.image = image
        self.scratch = scratch

    def server_version(self) -> str:
        """The tree layout's version; there is no server."""
        return VERSION

    def close(self) -> None:
        """Nothing to stop: it runs no servers."""

    def available(self) -> tuple[Requirement, ...]:
        """Nothing yet: this adapter reports no requirements of its sources."""
        return ()

    def _commands(self, commands: Iterable[str], tree: Path, owner: Path) -> None:
        for number, command in enumerate(commands, 1):
            script = f"cd {shlex.quote(str(tree))} && {command}"
            container = Container(
                image=self.image, command=("sh", "-c", script), owner=owner, mounts=(tree,)
            )
            done = self.runtime.run(container)
            if done.status != 0:
                # The output can name files, which can hold personal data.
                message = f"cleanup command {number} failed with status {done.status}"
                raise EngineError(message)

    def load(
        self,
        plan: Plan,  # noqa: ARG002 - the protocol passes the plan; copying needs only the group
        group: PlannedGroup,
        extracts: tuple[Path, ...],
        directory: Path,
        hooks: tuple[Hook, ...],
    ) -> tuple[TableCount, ...]:
        """Copy the group's directories from the extract; run ``before_load`` commands."""
        extract = one_extract(extracts)
        Extracted.model_validate_json((extract / "index.json").read_bytes())
        tree = directory / TREE
        tree.mkdir(parents=True)
        for table in group.tables:
            if (extract / TREE / table.name).is_dir():
                _copy(extract / TREE / table.name, tree / table.name)
        self._commands((hook.sql for hook in hooks), tree, directory)
        return _counts(tree, group.tables)

    def transform(
        self,
        group: PlannedGroup,
        source: Path,
        directory: Path,
        transformation: Transformation,
    ) -> tuple[TableCount, ...]:
        """Copy the working copy and run the cleanup commands in the copy."""
        if transformation.rules:
            message = "files take no column rules; clean them with `cleanup` commands"
            raise EngineError(message)
        directory.parent.mkdir(parents=True, exist_ok=True)
        _copy(source, directory)
        tree = directory / TREE
        hooks: tuple[TransformHook, ...] = transformation.hooks
        self._commands((hook.sql for hook in hooks), tree, directory)
        return _counts(tree, group.tables)

    def package(self, source: Path, image_format: ImageFormat, directory: Path) -> None:
        """The image is the tree."""
        if image_format is not ImageFormat.PHYSICAL:
            message = "file trees have only physical images"
            raise EngineError(message)
        _copy(source / TREE, directory / TREE)

    @contextmanager
    def restore(
        self, image: Path, image_format: ImageFormat, directory: Path
    ) -> Generator[RestoredImage]:
        """Copy an image's tree into scratch space for checks."""
        if image_format is not ImageFormat.PHYSICAL:
            message = "file trees have only physical images"
            raise EngineError(message)
        directory.mkdir(parents=True, exist_ok=True)
        _copy(image / TREE, directory / TREE)
        yield FilesRestored(directory / TREE)
