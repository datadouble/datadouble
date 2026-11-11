# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Throwaway local PostgreSQL clusters for working databases and restores.

Each working database is its own cluster in scratch space, listening only on a
Unix socket in a private directory, with trust authentication for the local
superuser. Server logs go next to the socket, never into the data directory,
so a physical image does not carry them.
"""

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from datadouble.engine import EngineError
from datadouble_postgres.errors import tool_error

SUPERUSER = "postgres"
_VERSION = re.compile(r"\(PostgreSQL\) (\d+(?:\.\d+)?)")


@dataclass(frozen=True, slots=True)
class Binaries:
    """A directory holding one PostgreSQL version's server and client programs."""

    bindir: Path

    def tool(self, name: str) -> str:
        """The path of one program."""
        return str(self.bindir / name)

    def run(self, name: str, arguments: Sequence[str], env: Mapping[str, str] | None = None) -> str:
        """Run a program; raise a redacted :class:`EngineError` if it fails."""
        result = subprocess.run(  # noqa: S603 - the program comes from bindir, arguments are built here
            [self.tool(name), *arguments],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, **(env or {})},
        )
        if result.returncode != 0:
            raise tool_error(name, result.returncode, result.stderr)
        return result.stdout

    def version(self) -> str:
        """The full server version, such as ``18.1``."""
        output = self.run("postgres", ["--version"])
        match = _VERSION.search(output)
        if match is None:
            message = f"cannot read the PostgreSQL version from {output.strip()!r}"
            raise EngineError(message)
        return match.group(1)


def _major(bindir: Path) -> int:
    """The major version in ``.../postgresql/<major>/bin``, or 0."""
    name = bindir.parent.name
    return int(name) if name.isdigit() else 0


def find_binaries(installed: Path = Path("/usr/lib/postgresql")) -> Binaries:
    """``DATADOUBLE_POSTGRES_BINDIR``, else the newest under ``installed``, else ``PATH``."""
    candidates: list[Path] = []
    if configured := os.environ.get("DATADOUBLE_POSTGRES_BINDIR"):
        candidates.append(Path(configured))
    else:
        candidates.extend(sorted(installed.glob("*/bin"), key=_major, reverse=True))
        if on_path := shutil.which("initdb"):
            candidates.append(Path(on_path).parent)
    for candidate in candidates:
        if (candidate / "initdb").is_file() and (candidate / "pg_dump").is_file():
            return Binaries(candidate)
    message = (
        "PostgreSQL server binaries were not found; install them or set DATADOUBLE_POSTGRES_BINDIR"
    )
    raise EngineError(message)


class Cluster:
    """One local cluster, identified by its data directory."""

    def __init__(self, binaries: Binaries, datadir: Path) -> None:
        """Manage the cluster in ``datadir``."""
        self.binaries = binaries
        self.datadir = datadir
        self._runtime: Path | None = None

    def initialize(self) -> None:
        """Create the cluster: UTF-8, C locale, trust for the local superuser."""
        self.datadir.parent.mkdir(parents=True, exist_ok=True)
        self.binaries.run(
            "initdb",
            [
                "--pgdata",
                str(self.datadir),
                "--username",
                SUPERUSER,
                "--auth",
                "trust",
                "--encoding",
                "UTF8",
                "--locale",
                "C",
                "--no-instructions",
            ],
        )

    @property
    def socket_dir(self) -> Path:
        """The directory holding the socket while the cluster runs."""
        if self._runtime is None:
            message = "the cluster is not running"
            raise EngineError(message)
        return self._runtime

    @contextmanager
    def running(self) -> Generator["Cluster"]:
        """Start the cluster for the duration of the block, then stop it cleanly."""
        runtime = Path(tempfile.mkdtemp(prefix="dd-pg-"))
        options = (
            f"-c listen_addresses='' -c unix_socket_directories='{runtime}' "
            "-c unix_socket_permissions=0700 -c fsync=off -c max_connections=20"
        )
        try:
            self.binaries.run(
                "pg_ctl",
                [
                    "start",
                    "--pgdata",
                    str(self.datadir),
                    "--wait",
                    "--log",
                    str(runtime / "server.log"),
                    "-o",
                    options,
                ],
            )
            self._runtime = runtime
            try:
                yield self
            finally:
                self._runtime = None
                self.binaries.run(
                    "pg_ctl", ["stop", "--pgdata", str(self.datadir), "--mode", "fast", "--wait"]
                )
        finally:
            shutil.rmtree(runtime, ignore_errors=True)

    def stop_abandoned(self) -> None:
        """Stop a server left running by a process that was killed; ignore failures."""
        subprocess.run(  # noqa: S603 - the program comes from bindir, arguments are built here
            [
                self.binaries.tool("pg_ctl"),
                "stop",
                "--pgdata",
                str(self.datadir),
                "--mode",
                "immediate",
                "--wait",
            ],
            capture_output=True,
            check=False,
        )

    def conninfo(self, dbname: str) -> str:
        """A connection string for one database of the running cluster."""
        return make_conninfo(host=str(self.socket_dir), dbname=dbname, user=SUPERUSER)

    def connect(self, dbname: str) -> psycopg.Connection[tuple[object, ...]]:
        """Connect to one database of the running cluster."""
        return psycopg.connect(self.conninfo(dbname), autocommit=True)

    def create_database(self, dbname: str) -> None:
        """Create a database unless it is the default ``postgres`` one."""
        if dbname == SUPERUSER:
            return
        with self.connect(SUPERUSER) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
