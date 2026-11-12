# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Running the external programs Datadouble wraps: ``docker``, ``kubectl``, ``cosign``...

Datadouble wraps mature tools rather than reimplementing them, so it runs them
often. Every run goes through here: a program found by name (or named by an
environment variable), arguments as a list and never through a shell, and
output captured for the caller to check.
"""

import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Completed:
    """A finished program: its status and output."""

    status: int
    stdout: str
    stderr: str

    def last_error(self) -> str:
        """The last line of standard error, or the status if there is none."""
        lines = [line for line in self.stderr.strip().splitlines() if line.strip()]
        return lines[-1] if lines else f"status {self.status}"


def locate(name: str, variable: str | None = None) -> str | None:
    """The path of program ``name``: the environment variable ``variable`` if set, else ``PATH``."""
    configured = os.environ.get(variable) if variable else None
    return configured or shutil.which(name)


def run_program(
    program: str,
    arguments: Sequence[str],
    *,
    stdin: str | bytes | None = None,
    env: Mapping[str, str] | None = None,
) -> Completed:
    """Run ``program`` with ``arguments`` and wait; ``env`` is added to the environment.

    Output is decoded as UTF-8, with anything undecodable replaced.
    """
    result = subprocess.run(  # noqa: S603 - the one place programs run: no shell, arguments as a list
        [program, *arguments],
        input=stdin.encode() if isinstance(stdin, str) else stdin,
        capture_output=True,
        check=False,
        env={**os.environ, **(env or {})},
    )
    return Completed(
        status=result.returncode,
        stdout=result.stdout.decode(errors="replace"),
        stderr=result.stderr.decode(errors="replace"),
    )


def start_program(program: str, arguments: Sequence[str]) -> "subprocess.Popen[str]":
    """Start ``program`` without waiting; its standard output can be read as text."""
    return subprocess.Popen(  # noqa: S603 - as run_program, for programs that keep running
        [program, *arguments], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
    )
