# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Copying directory trees, sharing blocks where the filesystem allows."""

import subprocess
from enum import StrEnum
from pathlib import Path


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
    result = subprocess.run(  # noqa: S603 - coreutils cp, with paths from Datadouble
        ["cp", "-a", f"--reflink={reflink}", origin, str(destination)],  # noqa: S607 - cp from PATH
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stderr.strip() or f"status {result.returncode}" if result.returncode else None
