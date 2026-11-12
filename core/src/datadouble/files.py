# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Copying directory trees, sharing blocks where the filesystem allows.

Copies run in this process rather than through ``cp``, so a copy never
outlives Datadouble: a run that is killed and resumed cannot have an earlier
attempt's copy still writing into the directory the new attempt uses. Blocks
are shared with the ``FICLONE`` ioctl (reflinks, on btrfs, XFS and others);
otherwise the kernel's copy is used.
"""

import fcntl
import shutil
from enum import StrEnum
from pathlib import Path

_FICLONE = 0x40049409
"""The Linux ioctl that makes a file share its source's blocks."""


class Reflink(StrEnum):
    """Whether a copy shares data blocks with its source."""

    AUTO = "auto"
    """Share blocks if the filesystem can, copy them otherwise."""
    ALWAYS = "always"
    """Share blocks, or fail."""


def _file(source: Path, destination: Path, reflink: Reflink) -> None:
    with source.open("rb") as reading, destination.open("xb") as writing:
        try:
            fcntl.ioctl(writing.fileno(), _FICLONE, reading.fileno())
            cloned = True
        except OSError:
            if reflink is Reflink.ALWAYS:
                raise
            cloned = False
    if not cloned:
        shutil.copyfile(source, destination)
    shutil.copystat(source, destination)


def _tree(source: Path, destination: Path, reflink: Reflink) -> None:
    if source.is_symlink():
        destination.symlink_to(source.readlink())
    elif source.is_dir():
        destination.mkdir()
        for child in sorted(source.iterdir()):
            _tree(child, destination / child.name, reflink)
        shutil.copystat(source, destination)
    else:
        _file(source, destination, reflink)


def copy_tree(
    source: Path,
    destination: Path,
    reflink: Reflink = Reflink.AUTO,
    *,
    contents: bool = False,
) -> str | None:
    """Copy ``source`` to the new path ``destination``; return the error, or None.

    Modes, timestamps and symbolic links are kept. With ``contents``,
    ``source``'s contents go into the existing directory ``destination``
    rather than ``source`` itself becoming it.
    """
    try:
        if contents:
            for child in sorted(source.iterdir()):
                _tree(child, destination / child.name, reflink)
        else:
            _tree(source, destination, reflink)
    except OSError as error:
        return f"{error.strerror or type(error).__name__}: {error.filename or source}"
    return None
