# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Copying directory trees, sharing blocks where the filesystem allows."""

from enum import StrEnum
from pathlib import Path

from datadouble.programs import locate, run_program


class Reflink(StrEnum):
    """Whether a copy shares data blocks with its source (``cp --reflink``)."""

    AUTO = "auto"
    """Share blocks if the filesystem can, copy them otherwise."""
    ALWAYS = "always"
    """Share blocks, or fail."""


def copy_tree(
    source: Path,
    destination: Path,
    reflink: Reflink = Reflink.AUTO,
    *,
    contents: bool = False,
) -> str | None:
    """Copy ``source`` to ``destination`` with ``cp -a``; return cp's error, or None.

    With ``contents``, ``source``'s contents go into the existing directory
    ``destination`` rather than ``source`` itself becoming it.
    """
    origin = f"{source}/." if contents else str(source)
    done = run_program(
        locate("cp") or "cp", ["-a", f"--reflink={reflink}", origin, str(destination)]
    )
    return done.stderr.strip() or f"status {done.status}" if done.status else None
