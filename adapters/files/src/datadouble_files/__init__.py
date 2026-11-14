# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The files engine adapter for Datadouble.

It copies a spec's ``files`` (decision 97)::

    files:
      connection:
        root: /srv/uploads             # the tree, as Datadouble sees it
        accept_inconsistent: true      # no filesystem snapshot: copy each file as it is
      select:                          # find tests for each top-level directory
        avatars: -mtime -30
        documents: -size -10M
      exclude: [tmp, cache]
      paths_from:                      # and the files the chosen rows name
        table: customer_documents
        path: documents/{customer_id}/{file_name}
      cleanup:                         # shell, run in the copy's root
        - find . -name '*.log' -delete
      image: vendor/exiftool:13        # the image cleanup runs in

Each top-level directory is a table whose rows are its files, keyed by their
path from the root. Every top-level directory must be selected or excluded.
"""

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict, JsonValue

from datadouble.engine import EngineError, ServerPlan, SourceSession, Tooling, WorkingDatabases
from datadouble.names import EngineName
from datadouble.planner import Capabilities
from datadouble.runtime import identity
from datadouble.settings import NoSettings, parse_settings
from datadouble.spec import ImageFormat
from datadouble_files.source import FilesSource
from datadouble_files.tree import ENGINE
from datadouble_files.working import DEFAULT_IMAGE, FilesWorking


class FilesConnection(BaseModel):
    """The ``files.connection`` settings."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    root: str
    accept_inconsistent: bool = False


class FilesEngine:
    """Implements :class:`datadouble.engine.EngineAdapter` for file trees."""

    @property
    def name(self) -> EngineName:
        """``files``."""
        return ENGINE

    @property
    def owner(self) -> str:
        """Whoever runs Datadouble: copies are plain files, served by no server."""
        return identity()

    def capabilities(self) -> Capabilities:
        """Physical images and hooks (the cleanup commands); every directory is accounted for."""
        return Capabilities(
            formats=(ImageFormat.PHYSICAL,),
            expressions=False,
            nested=False,
            hooks=True,
            complete=True,
        )

    @contextmanager
    def connect(self, connection: Mapping[str, JsonValue]) -> Generator[SourceSession]:
        """Open the tree."""
        settings = parse_settings(FilesConnection, connection, "files connection")
        yield FilesSource(Path(settings.root), accept_inconsistent=settings.accept_inconsistent)

    def working(self, scratch: Path, tooling: Tooling, source_version: str) -> WorkingDatabases:  # noqa: ARG002 - one layout version
        """Copies under ``scratch``; cleanup runs in ``tooling.image`` or a Debian image."""
        parse_settings(NoSettings, tooling.server, "files server")
        return FilesWorking(tooling.runtime, tooling.image or DEFAULT_IMAGE, scratch)

    def server(self, image: Path, server_image: str | None) -> ServerPlan:
        """A file tree has no server; replicate it as a volume."""
        message = (
            f"{image.name}: a file tree has no server to start, in {server_image or 'any'} image;"
            " its copies are its files"
        )
        raise EngineError(message)


def engine() -> FilesEngine:
    """The entry point named in ``datadouble.engines``."""
    return FilesEngine()
