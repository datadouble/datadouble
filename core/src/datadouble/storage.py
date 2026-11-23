# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Storage for finished, immutable artifacts: golden images and their summaries.

Stores are addressed by keys like ``spec/group/postgres-18/<generation>.summary.json``.
Storage adapters register under the ``datadouble.stores`` entry point group;
each entry point names a callable that takes the store's settings from the
spec and returns a :class:`Store`. The filesystem store lives here.

The filesystem store gives every file it writes its ``mode`` setting (``0640`` by
default) and every directory it creates that mode plus the search bit wherever
the mode has the read bit, whatever the umask, so another program, such as a
web server in front of an export store, can read what ``promote`` wrote.

A store that cannot list its keys, such as a web server, raises
:class:`ListingUnsupportedError` from ``list``; readers then use the index
``promote`` writes under each ``<spec>/<group>/`` (see :mod:`datadouble.images`).
"""

import os
import re
import shutil
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, JsonValue, field_validator

from datadouble.settings import parse_settings


class StoreError(Exception):
    """A store operation or configuration that failed."""


class ListingUnsupportedError(StoreError):
    """A store that cannot list its keys; readers use the index ``promote`` writes."""


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
        """Keys starting with ``prefix``, sorted.

        Raises :class:`ListingUnsupportedError` if the store cannot list keys.
        """
        ...

    def delete(self, key: str) -> None:
        """Remove ``key``; removing a key that does not exist is not an error."""
        ...


DEFAULT_MODE = 0o640
"""The mode of the files the filesystem store writes unless its ``mode`` says otherwise."""

_MODE = re.compile(r"0?[0-7]{3}")
_OWNER_READ_WRITE = stat.S_IRUSR | stat.S_IWUSR
_OTHERS_WRITE = stat.S_IWGRP | stat.S_IWOTH


def directory_mode(mode: int) -> int:
    """``mode`` plus the search bit wherever it has the read bit (``0640`` gives ``0750``)."""
    return mode | (mode & 0o444) >> 2


class FilesystemSettings(BaseModel):
    """Settings for the filesystem store."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    path: Path
    mode: str = f"{DEFAULT_MODE:04o}"
    """The mode of every file the store writes, as an octal string such as ``"0640"``."""

    @field_validator("mode")
    @classmethod
    def _check_mode(cls, mode: str) -> str:
        if not _MODE.fullmatch(mode):
            message = "must be three octal digits with an optional leading 0, such as 0640"
            raise ValueError(message)
        bits = int(mode, 8)
        if bits & _OWNER_READ_WRITE != _OWNER_READ_WRITE or bits & _OTHERS_WRITE:
            message = "must let the owner read and write and nobody else write, such as 0640"
            raise ValueError(message)
        return mode

    @property
    def permissions(self) -> int:
        """``mode`` as a number."""
        return int(self.mode, 8)


class FilesystemStore:
    """A directory, local or a network share. Writes go to a temporary file first."""

    def __init__(self, root: Path, mode: int = DEFAULT_MODE) -> None:
        """Store under ``root``, which is created if needed, writing files with ``mode``."""
        self.root = root
        self.mode = mode

    def _path(self, key: str) -> Path:
        return self.root / check_key(key)

    def describe(self) -> str:
        """The directory."""
        return f"filesystem:{self.root}"

    def _directories(self, directory: Path) -> None:
        """Create ``directory`` and missing parents, each with the directory mode."""
        missing: list[Path] = []
        while not directory.is_dir():
            missing.append(directory)
            directory = directory.parent
        for path in reversed(missing):
            try:
                path.mkdir(mode=directory_mode(self.mode))
            except FileExistsError:
                continue
            path.chmod(directory_mode(self.mode))

    def _write(self, key: str, write: "_Writer") -> None:
        target = self._path(key)
        try:
            self._directories(target.parent)
            handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".partial-")
        except OSError as error:
            message = f"cannot write {key} to {self.describe()}: {error.strerror}"
            raise StoreError(message) from None
        os.close(handle)
        try:
            write(Path(temporary))
            try:
                Path(temporary).chmod(self.mode)
            except OSError as error:
                message = f"cannot set the mode of {key} in {self.describe()}: {error.strerror}"
                raise StoreError(message) from None
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
    """Build a filesystem store from its spec settings (``path``, ``mode``)."""
    parsed = parse_settings(FilesystemSettings, settings, "filesystem store")
    return FilesystemStore(parsed.path, parsed.permissions)
