# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The engine adapter interface.

An engine adapter does everything that depends on the engine: introspecting
the catalog, reading one consistent snapshot, building working databases in
scratch space, applying anonymization in native updates, packaging golden
images and restoring them for verification. The core drives these through the
protocols below and never contains engine-specific logic.

Engine software (servers and tools) runs in the vendor's container images
through a :class:`~datadouble.runtime.ContainerRuntime`; see
:mod:`datadouble.runtime`.

Adapters register under the ``datadouble.engines`` entry point group; the
entry point names a zero-argument callable that returns an
:class:`EngineAdapter`.
"""

import datetime as dt
import fnmatch
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from datadouble.catalog import Catalog, Requirement
from datadouble.names import ColumnRef, EngineName, GroupName, TableName
from datadouble.planner import Capabilities, Plan, PlannedGroup, PlannedRule
from datadouble.runtime import ContainerRuntime, identity
from datadouble.spec import AccountPrivileges, Hook, ImageFormat, TransformHook
from datadouble.subset import Condition, Edge, KeyReader


class EngineError(Exception):
    """An engine operation that failed; the message is safe to show and log."""


class RequirementError(Exception):
    """The working image lacks requirements the sources use.

    ``problems`` lists each, with the source that uses it.
    """

    def __init__(self, problems: tuple[tuple[str, str], ...]) -> None:
        """Name every unmet requirement and its source."""
        super().__init__(
            f"the working image lacks {len(problems)} requirement(s) of the sources;"
            " name an image that has them in sources.<name>.image, or leave them out with"
            " sources.<name>.omit"
        )
        self.problems = problems


def unmet(needed: Iterable[Requirement], available: Iterable[Requirement]) -> tuple[str, ...]:
    """What ``needed`` asks for that ``available`` does not provide.

    An available requirement without a version provides every version.
    """
    offered: dict[tuple[str, str], set[str | None]] = {}
    for requirement in available:
        offered.setdefault((requirement.kind, requirement.name), set()).add(requirement.version)
    problems: list[str] = []
    for requirement in needed:
        versions = offered.get((requirement.kind, requirement.name))
        if versions is None:
            problems.append(f"{requirement.kind} {requirement.name} is not in the working image")
        elif None not in versions and requirement.version not in versions:
            listed = ", ".join(sorted(v for v in versions if v is not None))
            problems.append(
                f"{requirement.kind} {requirement.name} {requirement.version} is not in the"
                f" working image, which has {listed}"
            )
    return tuple(problems)


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
    snapshots: dict[str, str] = Field(default_factory=dict[str, str])
    """Each database's snapshot, by database, for engines that read each
    database under its own (``snapshot`` is then empty)."""
    snapshot_seconds: float
    """How long the snapshot was held open."""
    counts: tuple[TableCount, ...]


@dataclass(frozen=True, slots=True)
class Tooling:
    """Where engine software runs: the runtime, the spec's image override and server settings."""

    runtime: ContainerRuntime
    image: str | None = None
    """The sources' ``image``; when None the adapter picks the vendor image itself."""
    server: Mapping[str, JsonValue] = field(default_factory=dict[str, JsonValue])
    """The sources' ``server`` settings, which the adapter parses."""


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
        self,
        plan: Plan,
        selections: Mapping[TableName, Condition],
        directory: Path,
        tooling: Tooling,
    ) -> ExtractResult:
        """Write every extracted table's definition and chosen rows under ``directory``."""
        ...


class SourceSession(Protocol):
    """An open connection to the source."""

    def server_version(self) -> str:
        """The source server's version, which picks the default working image."""
        ...

    def namespaces(self) -> tuple[str, ...]:
        """The outermost namespaces (databases) the source reads, empty ones included.

        Several sources of a spec must not share one, since one working server
        holds them all. Engines whose namespaces exist only through their
        tables answer :func:`outermost` of the catalog.
        """
        ...

    def catalog(self) -> Catalog:
        """Introspect the tables, columns, keys and requirements."""
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

    def available(self) -> tuple[Requirement, ...] | None:
        """The requirements the working image can provide, one entry per version.

        An entry without a version provides every version of it. None means the
        working databases need none of the source's add-ons to hold its data,
        such as Salesforce's local copies of an org's managed packages' objects.
        """
        ...

    def close(self) -> None:
        """Stop whatever the working databases kept running between calls.

        A run calls it once it has finished with them, even after a failure;
        nothing is called afterwards.
        """
        ...

    def load(
        self,
        plan: Plan,
        group: PlannedGroup,
        extracts: tuple[Path, ...],
        directory: Path,
        hooks: tuple[Hook, ...],
    ) -> tuple[TableCount, ...]:
        """Build the group's working database in ``directory`` from the extracts.

        ``extracts`` holds one directory per source, in the spec's order, each
        written by that source's snapshot; every source's databases go into
        the one working database. There is more than one only when the
        engine's capabilities allow several sources. Runs ``before_load``
        hooks first, in the first source's default database, applies the
        group's fixups, and leaves the database stopped. A standalone group's
        database holds exactly its closure: the tables of ``group.packaged()``,
        with the rows of :func:`group_extract` where it has them, and of the
        group's objects, created in dependency order, no others.
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


@dataclass(frozen=True)
class Account:
    """One of ``package.accounts``, with its password resolved; the password never prints."""

    user: str
    password: str = field(repr=False)
    privileges: AccountPrivileges = AccountPrivileges.ALL


@runtime_checkable
class Accounts(Protocol):
    """Working databases that can give a physical image accounts (``Capabilities.accounts``)."""

    def add_accounts(self, image: Path, accounts: Sequence[Account]) -> None:
        """Give the physical image in ``image`` the accounts, each with its privileges.

        An existing user of an account's name gets the account's password and
        privileges. Each account must then log in over the network with its
        password, and only with it, or this raises :class:`EngineError`. The
        password must not appear in messages, or in plain text in any statement
        a server could log.
        """
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
    """The database clients connect to by default; a copy holds every database
    its image does, and this is the source's default one."""
    user: str


GROUP_EXTRACTS = "groups"
"""Where, in the extract stage's directory beside ``data``, standalone groups' own rows go."""


def group_extract_path(extract: Path, group: GroupName) -> Path:
    """Where a standalone group's own rows from one source are extracted.

    ``extract`` is that source's shared extract, ``data/<source>``; the
    group's own extract of it is ``groups/<group>/<source>``, beside ``data``,
    so it can never be taken for a source's.
    """
    return extract.parent.parent / GROUP_EXTRACTS / group / extract.name


def group_extract(extract: Path, group: GroupName) -> Path | None:
    """A standalone group's own extract, if it has one.

    It holds, in the shared extract's format, the rows of the tables the
    group's closure pulls in, and of its own tables when they gained rows a
    pulled-in table references. ``load`` takes the tables it lists from
    here, the group's other own tables from the shared extract, and leaves
    pulled-in tables it does not list without rows.
    """
    path = group_extract_path(extract, group)
    return path if path.is_dir() else None


def choose_databases(
    present: Iterable[str], wanted: Iterable[str], reserved: frozenset[str] = frozenset()
) -> tuple[str, ...]:
    """The databases a source's ``connection.databases`` names or matches, sorted.

    Each entry is a name or a shell-style glob. A name must be ``present`` and a
    glob must match at least one present database, so a typing mistake fails
    rather than quietly reading less. ``reserved`` databases (the engine's own)
    are never matched, and naming one fails.
    """
    available = frozenset(present)
    chosen: set[str] = set()
    for entry in wanted:
        if not any(character in entry for character in "*?["):
            if entry in reserved:
                message = f"database {entry} belongs to the server itself and cannot be read"
                raise EngineError(message)
            if entry not in available:
                message = f"database {entry} is not on the source, or the user cannot see it"
                raise EngineError(message)
            chosen.add(entry)
            continue
        matched = {name for name in available - reserved if fnmatch.fnmatchcase(name, entry)}
        if not matched:
            message = f"the database pattern {entry} matches no database on the source"
            raise EngineError(message)
        chosen |= matched
    return tuple(sorted(chosen))


def outermost(catalog: Catalog) -> tuple[str, ...]:
    """The outermost namespaces of a catalog's tables, in catalog order."""
    return tuple(dict.fromkeys(table.name.outermost for table in catalog.tables))


def one_extract(extracts: tuple[Path, ...]) -> Path:
    """The one extract of an engine that reads one source per spec."""
    if len(extracts) != 1:
        message = f"this engine loads one source's extract, not {len(extracts)}"
        raise EngineError(message)
    return extracts[0]


def check_owner(engine: "EngineAdapter") -> None:
    """Fail unless Datadouble runs as the user the engine's copies are served as."""
    if identity() != engine.owner:
        message = (
            f"{engine.name} images are served as {engine.owner}, so their files must belong to"
            f" that user and group; Datadouble runs as {identity()}. Run it as {engine.owner}"
            f" (for example `docker run --user {engine.owner}` or a pod's runAsUser and"
            " runAsGroup)"
        )
        raise EngineError(message)


class EngineAdapter(Protocol):
    """An engine: its capabilities, the source side and the scratch side."""

    @property
    def name(self) -> EngineName:
        """The name specs use for this engine."""
        ...

    @property
    def owner(self) -> str:
        """The ``uid:gid`` a copy of its images is served as, in its vendor's image.

        Datadouble runs as this user to build, fetch and replicate the engine's
        images, so their files already belong to the server that will open
        them (decision 86).
        """
        ...

    def capabilities(self) -> Capabilities:
        """What the engine supports, for planning."""
        ...

    def connect(self, connection: Mapping[str, JsonValue]) -> AbstractContextManager[SourceSession]:
        """Open one source, described by its ``connection`` in the spec's ``sources``."""
        ...

    def working(self, scratch: Path, tooling: Tooling, source_version: str) -> WorkingDatabases:
        """Working databases under ``scratch``, which only holds files this run owns.

        They run in ``tooling.image``, or by default the vendor's image for
        ``source_version``: the newest version of the sources the extracts
        were read from, which all share one version line.
        """
        ...

    def server(self, image: Path, server_image: str | None) -> ServerPlan:
        """How to serve a copy of the unpacked physical image ``image``.

        ``server_image`` overrides the container image the engine would choose.
        """
        ...
