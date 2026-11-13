# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Throwaway ``redis-server`` processes, in the official Redis image.

Each working database is a server on a directory in scratch space, whose
data is its ``dump.rdb``. A server is started with a password generated for
that start, read from standard input with the rest of its configuration so
it never appears on a command line, and saves only when told to: Datadouble
runs ``SAVE`` once the data is ready, before the server stops.
"""

import re
import secrets
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from datadouble.engine import EngineError
from datadouble.runtime import Container, ContainerRuntime, Server
from datadouble_redis.driver import Client, Error, failure

RDB = "dump.rdb"
PORT = 6379
LOGIN_ENV = "DATADOUBLE_PASSWORD"
_VERSION = re.compile(r"v=(\d+\.\d+\.\d+)")


def default_image(server_version: str) -> str:
    """The official image of exactly ``server_version``, such as ``redis:8.10.3``."""
    return f"redis:{server_version}"


def start_command(directory: str, databases: int, *, persist: bool) -> str:
    """A shell command starting a server on ``directory`` with the password from the environment.

    The password goes to the server as a configuration line on standard input.
    Without ``persist`` the server never saves on its own.
    """
    saving = "" if persist else ' --save ""'
    return (
        f"printf 'requirepass %s\\n' \"${LOGIN_ENV}\""
        f" | exec redis-server - --dir {directory} --dbfilename {RDB} --databases {databases}"
        f" --appendonly no --protected-mode no --port {PORT}{saving}"
    )


def ready_command() -> tuple[str, ...]:
    """A command that succeeds once the server answers, authenticating from the environment."""
    return ("sh", "-c", f'REDISCLI_AUTH="${LOGIN_ENV}" redis-cli ping | grep -q PONG')


@dataclass(frozen=True, slots=True)
class Tools:
    """Redis programs in containers from one image."""

    runtime: ContainerRuntime
    image: str

    def container(self, command: Sequence[str], owner: Path, mounts: Sequence[Path]) -> Container:
        """A container running ``command`` with ``mounts``."""
        return Container(
            image=self.image, command=tuple(command), owner=owner, mounts=tuple(mounts)
        )

    def version(self, owner: Path) -> str:
        """The server version in the image, such as ``8.10.3``."""
        done = self.runtime.run(self.container(["redis-server", "--version"], owner, ()))
        match = _VERSION.search(done.stdout)
        if done.status != 0 or match is None:
            message = (
                f"cannot read the Redis version: {(done.stderr or done.stdout).strip()[:80]!r}"
            )
            raise EngineError(message)
        return match.group(1)


class RedisServer:
    """One ``redis-server`` on a directory holding its ``dump.rdb``."""

    def __init__(self, tools: Tools, directory: Path, owner: Path, databases: int) -> None:
        """Manage the server on ``directory`` for the stage directory ``owner``."""
        self.tools = tools
        self.directory = directory
        self.owner = owner
        self.databases = databases
        self._server: Server | None = None
        self._password = ""

    @property
    def server(self) -> Server:
        """The running server's container."""
        if self._server is None:
            message = "the server is not running"
            raise EngineError(message)
        return self._server

    @contextmanager
    def running(self) -> Generator["RedisServer"]:
        """Start the server for the block, then stop it."""
        self.directory.mkdir(parents=True, exist_ok=True)
        password = secrets.token_hex(16)
        start = start_command(str(self.directory), self.databases, persist=False)
        container = replace(
            self.tools.container(["sh", "-c", start], self.owner, [self.directory]),
            secrets={LOGIN_ENV: password},
            port=PORT,
            ready=ready_command(),
        )
        with self.tools.runtime.serve(container) as server:
            self._server, self._password = server, password
            try:
                yield self
            finally:
                self._server, self._password = None, ""

    def client(self, database: int) -> Client:
        """A client for one database of the running server."""
        endpoint = self.server.endpoint
        return Client.at(endpoint.host, endpoint.port, self._password, database)

    def save(self) -> None:
        """Write the data to ``dump.rdb``."""
        client = self.client(0)
        try:
            client.command("SAVE")
        except Error as error:
            raise failure(error, "saving") from None
        finally:
            client.close()
