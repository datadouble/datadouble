# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The engine adapter interface.

An engine adapter does everything that depends on the engine: introspecting
the catalog, reading one consistent snapshot, building working databases in
scratch space, applying anonymization in native updates, packaging golden
images and restoring them for verification. The core drives these through the
protocols below and never contains engine-specific logic.

Adapters register under the ``datadouble.engines`` entry point group; the
entry point names a zero-argument callable that returns an
:class:`EngineAdapter`.
"""

import datetime as dt
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, JsonValue

from datadouble.catalog import Catalog
from datadouble.names import ColumnRef, EngineName, TableName
from datadouble.planner import Capabilities, Plan, PlannedGroup, PlannedRule
from datadouble.spec import Hook, ImageFormat, TransformHook
from datadouble.subset import Condition, Edge, KeyReader


class EngineError(Exception):
    """An engine operation that failed; the message is safe to show and log."""


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class TableCount(_Model):
    """Rows in one table."""

    table: TableName
    rows: int


class ExtractResult(_Model):
    """What ``extract`` read."""

    server_version: str
    snapshot: str
    """The engine's identifier for the snapshot, for the manifest."""
    snapshot_seconds: float
    """How long the snapshot was held open."""
    counts: tuple[TableCount, ...]


type Mapper = Callable[[PlannedRule, Iterable[str]], Mapping[str, str]]
"""Turns a rule and the distinct values of its column into replacements."""


@dataclass(frozen=True, slots=True)
class Transformation:
    """The anonymization ``transform`` applies to one working database."""

    rules: tuple[PlannedRule, ...]
    mapper: Mapper
    """Replacements for value rules (faker, hash, format-preserving)."""
    hooks: tuple[TransformHook, ...]


class Snapshot(KeyReader, Protocol):
    """One consistent point-in-time view of the source."""

    def extract(
        self, plan: Plan, selections: Mapping[TableName, Condition], directory: Path
    ) -> ExtractResult:
        """Write every extracted table's definition and chosen rows under ``directory``."""
        ...


class SourceSession(Protocol):
    """An open connection to the source."""

    def catalog(self) -> Catalog:
        """Introspect the tables, columns and keys."""
        ...

    def snapshot(self, as_of: dt.datetime) -> AbstractContextManager[Snapshot]:
        """Open a consistent snapshot; every read inside it sees the same data.

        ``as_of`` is the plan's anchor time, bound wherever spec SQL says ``:as_of``.
        """
        ...


class RestoredImage(Protocol):
    """A golden image restored into scratch space for verification."""

    def counts(self, tables: Iterable[TableName]) -> dict[TableName, int]:
        """Rows in each table."""
        ...

    def orphans(self, edge: Edge) -> int:
        """Child rows whose non-null reference has no parent row."""
        ...

    def text_values(self, column: ColumnRef) -> Iterable[str]:
        """The distinct non-null values of a column, as text."""
        ...

    def failing_rows(self, query: str) -> int:
        """Rows a native assertion query returns; zero means it passed."""
        ...


class WorkingDatabases(Protocol):
    """Working databases and images in scratch space."""

    def server_version(self) -> str:
        """The version of the server that builds and restores images."""
        ...

    def release(self, directory: Path) -> None:
        """Stop anything still running under ``directory`` from an interrupted attempt.

        Called before a stage's directory is cleared to run the stage again.
        """
        ...

    def load(
        self,
        plan: Plan,
        group: PlannedGroup,
        extract: Path,
        directory: Path,
        hooks: tuple[Hook, ...],
    ) -> tuple[TableCount, ...]:
        """Build the group's working database in ``directory`` from the extract.

        Runs ``before_load`` hooks first, applies the group's fixups, and leaves
        the database stopped.
        """
        ...

    def transform(
        self,
        group: PlannedGroup,
        source: Path,
        directory: Path,
        transformation: Transformation,
    ) -> tuple[TableCount, ...]:
        """Copy the loaded database to ``directory`` and anonymize it there.

        Every rule for a table is applied in one native update; the column rules
        and then the ``after_transform`` hooks run in one transaction, so an
        interrupted transform leaves nothing half done. Returns the row counts
        afterwards, since hooks may add or remove rows.
        """
        ...

    def package(self, source: Path, image_format: ImageFormat, directory: Path) -> None:
        """Write the working database in ``source`` as an image in ``directory``."""
        ...

    def restore(
        self, image: Path, image_format: ImageFormat, directory: Path
    ) -> AbstractContextManager[RestoredImage]:
        """Restore an image into ``directory`` and open it for checks."""
        ...


class ServerPlan(_Model):
    """How to run a server on a copy of a physical image, in a container.

    The copy is mounted at ``mount``, owned by ``owner``; ``command`` runs as
    that owner, with the copy's password in the environment variable
    ``DATADOUBLE_PASSWORD``, and must leave a server listening on ``port``
    that accepts ``user`` with that password over the network. ``ready``
    exits successfully once it does.
    """

    image: str
    mount: str
    owner: str
    command: tuple[str, ...]
    ready: tuple[str, ...]
    port: int
    database: str
    user: str


class EngineAdapter(Protocol):
    """An engine: its capabilities, the source side and the scratch side."""

    @property
    def name(self) -> EngineName:
        """The name specs use for this engine."""
        ...

    def capabilities(self) -> Capabilities:
        """What the engine supports, for planning."""
        ...

    def connect(self, connection: Mapping[str, JsonValue]) -> AbstractContextManager[SourceSession]:
        """Open the source described by the spec's ``source.connection``."""
        ...

    def working(self, scratch: Path) -> WorkingDatabases:
        """Working databases under ``scratch``, which only holds files this run owns."""
        ...

    def server(self, image: Path, server_image: str | None) -> ServerPlan:
        """How to serve a copy of the unpacked physical image ``image``.

        ``server_image`` overrides the container image the engine would choose.
        """
        ...
