# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Throwaway ``mongod`` servers and MongoDB's tools, in the official MongoDB image.

Every program runs in a container from the image, as Datadouble's own user,
with the paths it needs mounted at the same absolute path. Each working
database is its own ``mongod`` on a data directory in scratch space, started
with authentication: a build user gets a password generated for that start,
and is dropped again before the server stops, so images do not carry it.
``mongorestore`` and ``mongodump`` run inside the server's container and read
the password from a private configuration file, never a command line.
"""

import re
import secrets
import shutil
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from pymongo import MongoClient
from pymongo.errors import PyMongoError

from datadouble.engine import EngineError
from datadouble.runtime import Completed, Container, ContainerRuntime, Server

type Client = MongoClient[dict[str, object]]

USER = "datadouble-build"
_VERSION = re.compile(r"db version v(\d+\.\d+(?:\.\d+)?)")
_PORT = 27017
_LOGIN_ENV = "DATADOUBLE_PASSWORD"


def default_image(server_version: str) -> str:
    """The official image of exactly ``server_version``, such as ``mongo:9.0.8``."""
    return f"mongo:{server_version}"


def upsert_user(user: str) -> str:
    """Code for mongosh that creates ``user`` as root, or resets its password.

    The password comes from the ``DATADOUBLE_PASSWORD`` environment variable.
    """
    password = f"process.env.{_LOGIN_ENV}"
    return (
        "const admin = db.getSiblingDB('admin');"
        f" if (admin.getUser('{user}')) admin.updateUser('{user}', {{pwd: {password}}});"
        f" else admin.createUser({{user: '{user}', pwd: {password}, roles: ['root']}});"
    )


def _last_line(text: str, fallback: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else fallback


def _checked(name: str, done: Completed) -> str:
    """MongoDB's tools report documents only by namespace, so their output is safe to quote."""
    if done.status != 0:
        detail = _last_line(done.stderr or done.stdout, f"status {done.status}")
        message = f"{name} failed: {detail}"
        raise EngineError(message)
    return done.stdout


@dataclass(frozen=True, slots=True)
class Tools:
    """MongoDB programs in containers from one image."""

    runtime: ContainerRuntime
    image: str

    def container(
        self, command: Sequence[str], owner: Path, mounts: Sequence[Path], home: Path | None = None
    ) -> Container:
        """A container running ``command`` with ``mounts``; mongosh needs a writable ``home``."""
        return Container(
            image=self.image,
            command=tuple(command),
            owner=owner,
            mounts=tuple(mounts),
            env={"HOME": str(home)} if home else {},
        )

    def version(self, owner: Path) -> str:
        """The server version in the image, such as ``9.0.8``."""
        output = _checked(
            "mongod", self.runtime.run(self.container(["mongod", "--version"], owner, ()))
        )
        match = _VERSION.search(output)
        if match is None:
            message = f"cannot read the MongoDB version from {output.strip()[:80]!r}"
            raise EngineError(message)
        return match.group(1)


class MongoServer:
    """One ``mongod``, identified by its data directory."""

    def __init__(
        self, tools: Tools, dbpath: Path, owner: Path, mounts: Sequence[Path] = ()
    ) -> None:
        """Manage the server on ``dbpath`` for the stage directory ``owner``.

        ``mounts`` are further paths the server's container sees, for the
        tools run inside it.
        """
        self.tools = tools
        self.dbpath = dbpath
        self.owner = owner
        self.mounts = (dbpath.parent, *mounts)
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
    def running(self) -> Generator["MongoServer"]:
        """Start the server for the block, then shut it down cleanly."""
        self.dbpath.mkdir(parents=True, exist_ok=True)
        data = str(self.dbpath)
        password = secrets.token_hex(16)
        # Files for this start only, beside the data directory and never in it.
        private = self.dbpath.parent / f".mongod-{secrets.token_hex(4)}"
        private.mkdir(mode=0o700)
        setup_log, ready = private / "setup.log", private / "ready"
        start = (
            f"mongod --dbpath {data} --bind_ip 127.0.0.1 --port {_PORT} --fork"
            f" --logpath {setup_log} > /dev/null || {{ tail -n 3 {setup_log}; exit 1; }}"
            f'; mongosh --quiet --eval "{upsert_user(USER)}" > /dev/null'
            f" && mongod --dbpath {data} --shutdown > /dev/null"
            f" && touch {ready}"
            f" && exec mongod --dbpath {data} --auth --bind_ip_all --port {_PORT}"
        )
        container = replace(
            self.tools.container(["sh", "-c", start], self.owner, self.mounts, private),
            secrets={_LOGIN_ENV: password},
            port=_PORT,
            ready=(
                "sh",
                "-c",
                f"test -f {ready} && mongosh --quiet --eval 'db.adminCommand(\"ping\")'",
            ),
        )
        with self.tools.runtime.serve(container) as server:
            self._server, self._password = server, password
            try:
                yield self
                self._drop_build_user()
            finally:
                self._server, self._password = None, ""
                shutil.rmtree(private, ignore_errors=True)

    def _drop_build_user(self) -> None:
        with self.client() as client:
            try:
                client.admin.command("dropUser", USER)
            except PyMongoError as error:
                message = f"removing the build user failed: {type(error).__name__}"
                raise EngineError(message) from None

    def client(self) -> Client:
        """A client for the running server, as the build user."""
        endpoint = self.server.endpoint
        client: Client = MongoClient(
            host=endpoint.host,
            port=endpoint.port,
            username=USER,
            password=self._password,
            authSource="admin",
            directConnection=True,
            serverSelectionTimeoutMS=10000,
        )
        return client

    def tool(self, name: str, arguments: Sequence[str]) -> str:
        """Run ``mongodump`` or ``mongorestore`` inside the server's container.

        The container already holds the build user's password in its
        environment; the tool reads its connection and password as a
        configuration file on standard input, so the password is never written
        to a file or a command line.
        """
        config = (
            f"printf 'uri: mongodb://{USER}@127.0.0.1:{_PORT}/?authSource=admin\\n"
            f'password: %s\\n\' "${_LOGIN_ENV}"'
            ' | exec "$0" --config=/dev/stdin "$@"'
        )
        return _checked(
            name, self.server.execute(["sh", "-c", config, name, "--quiet", *arguments])
        )
