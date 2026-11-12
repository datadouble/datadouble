# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Throwaway PostgreSQL clusters and tools, in the official PostgreSQL image.

Every program runs in a container from the image, as Datadouble's own user,
with the paths it needs mounted at the same absolute path. The image's
``nss_wrapper`` gives that user a name, which ``initdb`` and the server
insist on. Each working database is its own cluster in scratch space; while
it runs, it accepts the superuser over the network with a password generated
for that start, and over the loopback address without one, which only
programs inside its own container (``pg_restore``, ``pg_dump``) can use. It
has no Unix socket. Server logs and the
authentication file stay outside the data directory, so a physical image does
not carry them.
"""

import os
import re
import secrets
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from datadouble.engine import EngineError
from datadouble.runtime import Completed, Container, ContainerRuntime, Server
from datadouble_postgres.errors import tool_error

SUPERUSER = "postgres"
LOOPBACK = "127.0.0.1"
"""How programs inside a server's container reach it: trusted, since only they can."""
_VERSION = re.compile(r"\(PostgreSQL\) (\d+(?:\.\d+)?)")
_LOGIN_ENV = "DATADOUBLE_PASSWORD"


def default_image(server_version: str) -> str:
    """The official image of exactly ``server_version``, such as ``postgres:18.4-trixie``.

    The working server matches the source's minor version: a dump from a newer
    minor release can hold what an older server rejects.
    """
    return f"postgres:{server_version}-trixie"


@dataclass(frozen=True, slots=True)
class Tools:
    """PostgreSQL programs in containers from one image.

    ``home`` holds the files every container needs: the user database for
    ``nss_wrapper`` and the servers' authentication file.
    """

    runtime: ContainerRuntime
    image: str
    home: Path

    def _environment(self) -> dict[str, str]:
        passwd = self.home / "passwd"
        if not passwd.is_file():
            self.home.mkdir(parents=True, exist_ok=True)
            uid, gid = os.getuid(), os.getgid()
            passwd.write_text(f"datadouble:x:{uid}:{gid}::/tmp:/bin/sh\n", "utf-8")
            (self.home / "group").write_text(f"datadouble:x:{gid}:\n", "utf-8")
            (self.home / "pg_hba.conf").write_text(
                f"host all all {LOOPBACK}/32 trust\nhost all all all scram-sha-256\n", "utf-8"
            )
        return {
            "LD_PRELOAD": "libnss_wrapper.so",
            "NSS_WRAPPER_PASSWD": str(passwd),
            "NSS_WRAPPER_GROUP": str(self.home / "group"),
        }

    def container(
        self,
        command: Sequence[str],
        owner: Path,
        mounts: Sequence[Path] = (),
        secrets_: Mapping[str, str] | None = None,
    ) -> Container:
        """A container running ``command`` with ``mounts`` and the tools' home."""
        return Container(
            image=self.image,
            command=tuple(command),
            owner=owner,
            mounts=(self.home, *mounts),
            env=self._environment(),
            secrets=dict(secrets_ or {}),
        )

    def run(
        self,
        name: str,
        arguments: Sequence[str],
        owner: Path,
        mounts: Sequence[Path] = (),
        secrets_: Mapping[str, str] | None = None,
    ) -> str:
        """Run one program to completion; raise a redacted :class:`EngineError` if it fails."""
        done = self.runtime.run(self.container([name, *arguments], owner, mounts, secrets_))
        return _checked(name, done)

    def version(self) -> str:
        """The full server version in the image, such as ``18.1``."""
        output = self.run("postgres", ["--version"], self.home)
        match = _VERSION.search(output)
        if match is None:
            message = f"cannot read the PostgreSQL version from {output.strip()!r}"
            raise EngineError(message)
        return match.group(1)


def _checked(name: str, done: Completed) -> str:
    if done.status != 0:
        raise tool_error(name, done.status, done.stderr)
    return done.stdout


class Cluster:
    """One cluster, identified by its data directory."""

    def __init__(
        self, tools: Tools, datadir: Path, owner: Path, mounts: Sequence[Path] = ()
    ) -> None:
        """Manage the cluster in ``datadir`` for the stage directory ``owner``.

        ``mounts`` are further paths the server's container sees, for the
        programs run inside it.
        """
        self.tools = tools
        self.datadir = datadir
        self.owner = owner
        self.mounts = (datadir.parent, *mounts)
        self._server: Server | None = None
        self._password = ""

    def initialize(self) -> None:
        """Create the cluster: UTF-8, C locale."""
        self.datadir.parent.mkdir(parents=True, exist_ok=True)
        self.tools.run(
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
            self.owner,
            [self.datadir.parent],
        )

    @property
    def server(self) -> Server:
        """The running server's container."""
        if self._server is None:
            message = "the cluster is not running"
            raise EngineError(message)
        return self._server

    @contextmanager
    def running(self) -> Generator["Cluster"]:
        """Start the cluster for the duration of the block, then stop it cleanly."""
        data = str(self.datadir)
        password = secrets.token_hex(16)
        # The password is hex, so quoting it is safe; it reaches the container
        # through the runtime's secret channel, never a command line.
        start = (
            f'printf "ALTER ROLE {SUPERUSER} PASSWORD \'%s\'\\n" "${_LOGIN_ENV}"'
            f" | postgres --single -D {data} postgres > /dev/null"
            f" && exec postgres -D {data} -c listen_addresses='*'"
            f" -c unix_socket_directories='' -c hba_file={self.tools.home}/pg_hba.conf"
            " -c fsync=off -c max_connections=20 -c logging_collector=off"
        )
        container = replace(
            self.tools.container(
                ["sh", "-c", start], self.owner, self.mounts, {_LOGIN_ENV: password}
            ),
            port=5432,
            ready=("pg_isready", f"--host={LOOPBACK}", f"--username={SUPERUSER}"),
        )
        with self.tools.runtime.serve(container) as server:
            self._server, self._password = server, password
            try:
                yield self
            finally:
                self._server, self._password = None, ""

    def execute(self, name: str, arguments: Sequence[str]) -> str:
        """Run a client program inside the server's container, over the loopback address."""
        return _checked(name, self.server.execute([name, *arguments]))

    def local_conninfo(self, dbname: str) -> str:
        """A connection string for programs inside the server's container."""
        return make_conninfo(host=LOOPBACK, dbname=dbname, user=SUPERUSER)

    def connect(self, dbname: str) -> psycopg.Connection[tuple[object, ...]]:
        """Connect to one database of the running cluster, over the network."""
        endpoint = self.server.endpoint
        conninfo = make_conninfo(
            host=endpoint.host, port=str(endpoint.port), dbname=dbname, user=SUPERUSER
        )
        try:
            return psycopg.connect(conninfo, password=self._password, autocommit=True)
        except psycopg.Error as error:
            message = f"connecting to the working database failed: {type(error).__name__}"
            raise EngineError(message) from None

    def create_database(self, dbname: str) -> None:
        """Create a database unless it is the default ``postgres`` one."""
        if dbname == SUPERUSER:
            return
        with self.connect(SUPERUSER) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
