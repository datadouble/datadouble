# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Storage for finished, immutable artifacts: golden images and their summaries.

Stores are addressed by keys like ``spec/group/postgres-18/<generation>.summary.json``.
Storage adapters register under the ``datadouble.stores`` entry point group;
each entry point names a callable that takes the store's settings from the
spec and returns a :class:`Store`. The filesystem store lives here.
"""

import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, JsonValue

from datadouble.settings import parse_settings


class StoreError(Exception):
    """A store operation or configuration that failed."""


_KEY = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")


def check_key(key: str) -> str:
    """Return ``key`` if it is a relative path of safe components."""
    if not _KEY.match(key) or any(part in {".", ".."} for part in key.split("/")):
        message = f"{key!r} is not a valid store key"
        raise StoreError(message)
    return key


class Store(Protocol):
    """A place images are written to and read from."""

    def describe(self) -> str:
        """Where the store is, for manifests and messages; never includes credentials."""
        ...

    def put_file(self, key: str, source: Path) -> None:
        """Write a file under ``key``, replacing nothing half-written on failure."""
        ...

    def put_bytes(self, key: str, data: bytes) -> None:
        """Write bytes under ``key``."""
        ...

    def get_bytes(self, key: str) -> bytes | None:
        """The bytes under ``key``, or None if there are none."""
        ...

    def get_file(self, key: str, destination: Path) -> bool:
        """Copy ``key`` to ``destination``; False if there is no such key."""
        ...

    def list(self, prefix: str) -> list[str]:
        """Keys starting with ``prefix``, sorted."""
        ...

    def delete(self, key: str) -> None:
        """Remove ``key``; removing a key that does not exist is not an error."""
        ...


class FilesystemSettings(BaseModel):
    """Settings for the filesystem store."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    path: Path


class FilesystemStore:
    """A directory, local or a network share. Writes go to a temporary file first."""

    def __init__(self, root: Path) -> None:
        """Store under ``root``, which is created if needed."""
        self.root = root

    def _path(self, key: str) -> Path:
        return self.root / check_key(key)

    def describe(self) -> str:
        """The directory."""
        return f"filesystem:{self.root}"

    def _write(self, key: str, write: "_Writer") -> None:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".partial-")
        os.close(handle)
        try:
            write(Path(temporary))
            Path(temporary).replace(target)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def put_file(self, key: str, source: Path) -> None:
        """Copy ``source`` into the store."""
        self._write(key, lambda temporary: shutil.copyfile(source, temporary))

    def put_bytes(self, key: str, data: bytes) -> None:
        """Write ``data`` into the store."""
        self._write(key, lambda temporary: temporary.write_bytes(data))

    def get_bytes(self, key: str) -> bytes | None:
        """Read a key's bytes."""
        path = self._path(key)
        return path.read_bytes() if path.is_file() else None

    def get_file(self, key: str, destination: Path) -> bool:
        """Copy a key out of the store."""
        path = self._path(key)
        if not path.is_file():
            return False
        shutil.copyfile(path, destination)
        return True

    def delete(self, key: str) -> None:
        """Remove a key."""
        self._path(key).unlink(missing_ok=True)

    def list(self, prefix: str) -> list[str]:
        """Keys under the store root starting with ``prefix``."""
        if not self.root.is_dir():
            return []
        keys = (
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file() and not path.name.startswith(".partial-")
        )
        return sorted(key for key in keys if key.startswith(prefix))


class _Writer(Protocol):
    def __call__(self, temporary: Path, /) -> object: ...


def filesystem_store(settings: Mapping[str, JsonValue]) -> Store:
    """Build a filesystem store from its spec settings (``path``)."""
    return FilesystemStore(parse_settings(FilesystemSettings, settings, "filesystem store").path)
