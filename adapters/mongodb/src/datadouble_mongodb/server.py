# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Throwaway local ``mongod`` servers, and MongoDB's command-line tools.

Each working database is its own ``mongod`` on a data directory in scratch
space, listening only on a Unix socket in a private directory. Its log goes
next to the socket, never into the data directory.
"""

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from pymongo import MongoClient

from datadouble.engine import EngineError

type Client = MongoClient[dict[str, object]]

_VERSION = re.compile(r"db version v(\d+\.\d+(?:\.\d+)?)")
_PORT = "27017"


def _last_line(text: str, fallback: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else fallback


@dataclass(frozen=True, slots=True)
class Tools:
    """Where ``mongod``, ``mongodump`` and ``mongorestore`` are."""

    bindir: Path | None
    """A directory holding all three, or None to find them on ``PATH``."""

    def tool(self, name: str) -> str:
        """The path of one program."""
        found = str(self.bindir / name) if self.bindir else shutil.which(name)
        if not found or not Path(found).is_file():
            message = (
                f"{name} was not found; install the MongoDB server and database tools, "
                "or set DATADOUBLE_MONGODB_BINDIR"
            )
            raise EngineError(message)
        return found

    def run(self, name: str, arguments: Sequence[str]) -> str:
        """Run a program; raise :class:`EngineError` if it fails.

        MongoDB's tools report documents only by namespace, so their output is
        safe to quote.
        """
        result = subprocess.run(  # noqa: S603 - a MongoDB program, with arguments built here
            [self.tool(name), *arguments], capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            detail = _last_line(result.stderr or result.stdout, f"status {result.returncode}")
            message = f"{name} failed: {detail}"
            raise EngineError(message)
        return result.stdout

    def version(self) -> str:
        """The server version, such as ``9.0.8``."""
        output = self.run("mongod", ["--version"])
        match = _VERSION.search(output)
        if match is None:
            message = f"cannot read the MongoDB version from {output.strip()[:80]!r}"
            raise EngineError(message)
        return match.group(1)


def find_tools() -> Tools:
    """``DATADOUBLE_MONGODB_BINDIR``, or ``PATH``."""
    configured = os.environ.get("DATADOUBLE_MONGODB_BINDIR")
    return Tools(Path(configured) if configured else None)


class Server:
    """One local ``mongod``, identified by its data directory."""

    def __init__(self, tools: Tools, dbpath: Path) -> None:
        """Manage the server on ``dbpath``."""
        self.tools = tools
        self.dbpath = dbpath
        self._socket: Path | None = None

    @property
    def socket(self) -> Path:
        """The socket while the server runs."""
        if self._socket is None:
            message = "the server is not running"
            raise EngineError(message)
        return self._socket

    def uri(self) -> str:
        """A connection URI for the running server."""
        return f"mongodb://{quote(str(self.socket), safe='')}/?directConnection=true"

    @contextmanager
    def running(self) -> Generator["Server"]:
        """Start the server for the block, then shut it down cleanly."""
        self.dbpath.mkdir(parents=True, exist_ok=True)
        runtime = Path(tempfile.mkdtemp(prefix="dd-mongo-"))
        socket = runtime / "mongod.sock"
        try:
            self.tools.run(
                "mongod",
                [
                    f"--dbpath={self.dbpath}",
                    f"--bind_ip={socket}",
                    f"--port={_PORT}",
                    "--nounixsocket",
                    "--fork",
                    f"--logpath={runtime / 'mongod.log'}",
                ],
            )
            self._socket = socket
            try:
                yield self
            finally:
                self._socket = None
                self.tools.run("mongod", [f"--dbpath={self.dbpath}", "--shutdown"])
        finally:
            shutil.rmtree(runtime, ignore_errors=True)

    def client(self) -> Client:
        """A client for the running server."""
        client: Client = MongoClient(self.uri(), serverSelectionTimeoutMS=10000)
        return client

    def stop_abandoned(self) -> None:
        """Shut down a server left running by a killed process; ignore failures."""
        subprocess.run(  # noqa: S603 - mongod, with arguments built here
            [self.tools.tool("mongod"), f"--dbpath={self.dbpath}", "--shutdown"],
            capture_output=True,
            check=False,
        )
