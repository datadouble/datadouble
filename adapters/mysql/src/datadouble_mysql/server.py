# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Throwaway MySQL servers and MySQL's tools, in the official MySQL image.

Every program runs in a container from the image, as Datadouble's own user,
with the paths it needs mounted at the same absolute path. Each working
database is its own server on a data directory in scratch space, without a
binary log. While it runs, a build user accepts a password generated for that
start over the network, and ``root`` connects without one over the server's
socket, which only programs inside its own container can reach; tools such as
``mysqldump`` run there. The build user is dropped before the server stops, so
images do not carry it. Files a start needs (its readiness marker and setup
log) live in a private directory beside the data directory, never in it.
"""

import re
import secrets
import shutil
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import pymysql
from pymysql.connections import Connection

from datadouble.engine import EngineError
from datadouble.runtime import Completed, Container, ContainerRuntime, Server
from datadouble_mysql.errors import database_error, tool_error

USER = "datadouble-build"
_VERSION = re.compile(r"Ver (\d+\.\d+\.\d+)")
_PORT = 3306
_LOGIN_ENV = "DATADOUBLE_PASSWORD"


def default_image(server_version: str) -> str:
    """The official image of exactly ``server_version``, such as ``mysql:8.4.13``."""
    return f"mysql:{server_version}"


def create_user(user: str) -> str:
    """Shell for creating ``user`` with every privilege, or resetting its password.

    The password comes from ``DATADOUBLE_PASSWORD`` and reaches the server on
    standard input, never a command line. Run by ``root`` over the socket.
    """
    # printf's format: the host wildcard is %%, the passwords are %s.
    statement = (
        f"CREATE USER IF NOT EXISTS '{user}'@'%%' IDENTIFIED BY '%s';"
        f" ALTER USER '{user}'@'%%' IDENTIFIED BY '%s';"
        f" GRANT ALL ON *.* TO '{user}'@'%%' WITH GRANT OPTION;"
    )
    return f'printf "{statement}\\n" "${_LOGIN_ENV}" "${_LOGIN_ENV}" | mysql -uroot'


def checked(name: str, done: Completed) -> str:
    """``done``'s output, or a redacted :class:`EngineError` if it failed."""
    if done.status != 0:
        raise tool_error(name, done.status, done.stderr)
    return done.stdout


@dataclass(frozen=True, slots=True)
class Tools:
    """MySQL programs in containers from one image."""

    runtime: ContainerRuntime
    image: str

    def container(self, command: Sequence[str], owner: Path, mounts: Sequence[Path]) -> Container:
        """A container running ``command`` with ``mounts``."""
        return Container(
            image=self.image, command=tuple(command), owner=owner, mounts=tuple(mounts)
        )

    def version(self, owner: Path) -> str:
        """The server version in the image, such as ``8.4.13``."""
        done = self.runtime.run(self.container(["mysqld", "--version"], owner, ()))
        output = checked("mysqld", done)
        match = _VERSION.search(output)
        if match is None:
            message = f"cannot read the MySQL version from {output.strip()[:80]!r}"
            raise EngineError(message)
        return match.group(1)


class MysqlServer:
    """One server, identified by its data directory."""

    def __init__(
        self, tools: Tools, datadir: Path, owner: Path, mounts: Sequence[Path] = ()
    ) -> None:
        """Manage the server on ``datadir`` for the stage directory ``owner``.

        ``mounts`` are further paths the server's container sees, for loading
        files and for the tools run inside it.
        """
        self.tools = tools
        self.datadir = datadir
        self.owner = owner
        self.mounts = (datadir.parent, *mounts)
        self._server: Server | None = None
        self._password = ""

    def initialize(self) -> None:
        """Create an empty data directory with a password-less ``root``@``localhost``."""
        self.datadir.parent.mkdir(parents=True, exist_ok=True)
        command = ["mysqld", "--initialize-insecure", f"--datadir={self.datadir}"]
        container = self.tools.container(command, self.owner, [self.datadir.parent])
        checked("mysqld --initialize", self.tools.runtime.run(container))

    @property
    def server(self) -> Server:
        """The running server's container."""
        if self._server is None:
            message = "the server is not running"
            raise EngineError(message)
        return self._server

    @contextmanager
    def running(self) -> Generator["MysqlServer"]:
        """Start the server for the block, then shut it down cleanly."""
        password = secrets.token_hex(16)
        private = self.datadir.parent / f".mysqld-{secrets.token_hex(4)}"
        private.mkdir(mode=0o700)
        setup_log, ready = private / "setup.log", private / "ready"
        options = (
            f"--datadir={self.datadir} --skip-log-bin --mysqlx=OFF --secure-file-priv="
            " --local-infile=OFF"
        )
        start = (
            f"mysqld {options} --bind-address=127.0.0.1 --daemonize --log-error={setup_log}"
            f" > /dev/null || {{ tail -n 3 {setup_log}; exit 1; }}"
            f"; {create_user(USER)}"
            " && mysqladmin -uroot shutdown"
            f" && touch {ready}"
            f" && exec mysqld {options} --bind-address=*"
        )
        container = replace(
            self.tools.container(["sh", "-c", start], self.owner, self.mounts),
            secrets={_LOGIN_ENV: password},
            port=_PORT,
            ready=("sh", "-c", f"test -f {ready} && mysqladmin -uroot ping"),
        )
        try:
            with self.tools.runtime.serve(container) as server:
                self._server, self._password = server, password
                try:
                    yield self
                    self._drop_build_user()
                finally:
                    self._server, self._password = None, ""
        finally:
            shutil.rmtree(private, ignore_errors=True)

    def _drop_build_user(self) -> None:
        with self.connect() as connection:
            try:
                connection.cursor().execute(f"DROP USER '{USER}'@'%'")
            except pymysql.MySQLError as error:
                raise database_error(error, "removing the build user") from None

    def connect(self, database: str | None = None) -> Connection:
        """Connect to the running server as the build user, over the network."""
        endpoint = self.server.endpoint
        try:
            return pymysql.connect(
                host=endpoint.host,
                port=endpoint.port,
                user=USER,
                password=self._password,
                database=database,
                charset="utf8mb4",
                autocommit=True,
            )
        except pymysql.MySQLError as error:
            raise database_error(error, "connecting to the working database") from None

    def tool(self, name: str, arguments: Sequence[str], stdin_file: Path | None = None) -> str:
        """Run a client program inside the server's container as ``root``.

        With ``stdin_file``, the program reads that file on standard input.
        """
        command = [name, "-uroot", *arguments]
        if stdin_file is not None:
            command = ["sh", "-c", f'exec "$@" < {stdin_file}', "sh", *command]
        return checked(name, self.server.execute(command))
