# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Throwaway Oracle containers holding one pluggable database each.

A working database is a pluggable database (PDB) whose datafiles live in the
stage directory's ``data/``. Each start runs a new container from Oracle's
Database Free image, whose own container database (CDB) lives and dies inside
it, and plugs the PDB in from its manifest (``pdb.xml``); before the
container stops the PDB is unplugged again, leaving consistent datafiles and
a manifest naming where they were. An image is just those files: no CDB, no
other users' passwords. ``SYS`` gets a password generated for that start, and
Datadouble connects with it over the network.

The image runs Oracle as its ``oracle`` user, uid and gid 54321, which is the
user Datadouble runs as for Oracle (decision 86).
"""

import secrets
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from datadouble.engine import EngineError
from datadouble.runtime import Container, ContainerRuntime, Server
from datadouble_oracle.driver import Connection, Error, connect
from datadouble_oracle.errors import database_error
from datadouble_oracle.render import compose, quote

PDB = "DATADOUBLE"
"""The pluggable database every working database and image is."""
DATA = "data"
"""The directory of a working database's datafiles and manifest."""
MANIFEST = "pdb.xml"
_PORT = 1521
_TAG_PARTS = 4
"""Image tags name a version's first four parts."""
_SEED = "/opt/oracle/oradata/FREE/pdbseed/"
_SYS_VARIABLE = "ORACLE_PWD"
# The image's own start; on SIGTERM its script shuts the database down but goes
# on waiting for its log, so this one shuts down and exits itself. Working
# databases are unplugged before the container stops.
_START = """
"$ORACLE_BASE/$RUN_FILE" &
trap 'printf "SHUTDOWN IMMEDIATE\\nEXIT\\n" | sqlplus -s / as sysdba > /dev/null; exit 0' TERM
wait
"""
# Ready once SYS logs in with this start's password, which the image sets only
# after the database opens; silent SQL*Plus reports a login by the query's
# answer alone. The password reaches SQL*Plus through a builtin.
_READY = (
    "bash",
    "-c",
    (
        'printf \'connect sys/"%s"@localhost:1521/FREE as sysdba\\n'
        'SELECT 4242 + 1 FROM dual;\\nexit\\n\' "$ORACLE_PWD"'
        " | sqlplus -s -L /nolog | grep -q 4243"
    ),
)


def default_image(server_version: str) -> str:
    """Oracle's Database Free image of a version such as ``23.26.3.0.0``.

    Tags name the first four parts (``23.26.3.0``). The full image, not
    ``-lite``: lite compresses its pre-built database and expands it on every
    start, which doubles a start's time (78 s against 38 s), and it leaves out
    components a source may use.
    """
    parts = server_version.split(".")
    if len(parts) < _TAG_PARTS or not all(part.isdigit() for part in parts):
        message = (
            f"no Oracle image is known for version {server_version}; set source.image to"
            " an Oracle Database Free image of that version"
        )
        raise EngineError(message)
    return f"container-registry.oracle.com/database/free:{'.'.join(parts[:_TAG_PARTS])}"


def literal(text: str) -> str:
    """A string literal, for file paths in DDL."""
    return "'" + text.replace("'", "''") + "'"


@dataclass(frozen=True, slots=True)
class Tools:
    """Oracle containers from one image."""

    runtime: ContainerRuntime
    image: str


class OracleServer:
    """One container serving the pluggable database in ``directory``."""

    def __init__(
        self, tools: Tools, directory: Path, owner: Path, mounts: tuple[Path, ...] = ()
    ) -> None:
        """Serve ``directory`` for the stage directory ``owner``."""
        self.tools = tools
        self.directory = directory
        self.owner = owner
        self.mounts = (directory, *mounts)
        self._server: Server | None = None
        self._password = ""

    @property
    def data(self) -> Path:
        """The directory of the database's datafiles and manifest."""
        return self.directory / DATA

    @property
    def server(self) -> Server:
        """The running container."""
        if self._server is None:
            message = "the server is not running"
            raise EngineError(message)
        return self._server

    @contextmanager
    def running(self) -> Generator["OracleServer"]:
        """Start a new container for the block, then stop it."""
        self.data.mkdir(parents=True, exist_ok=True)
        # Oracle's rules: letters, digits and a symbol, starting with a letter.
        password = f"Dd{secrets.token_hex(12)}_1"
        container = Container(
            image=self.tools.image,
            command=("/bin/bash", "-c", _START),
            owner=self.owner,
            mounts=self.mounts,
            secrets={_SYS_VARIABLE: password},
            port=_PORT,
            ready=_READY,
        )
        with self.tools.runtime.serve(container) as server:
            self._server, self._password = server, password
            try:
                yield self
            except Error as error:
                # Whatever the block did, the message leaves redacted.
                raise database_error(error, "using the working database") from None
            finally:
                self._server, self._password = None, ""

    def connect(self, container: str = PDB) -> Connection:
        """Connect as ``SYS`` to the CDB's root, then switch to ``container``."""
        endpoint = self.server.endpoint
        try:
            connection = connect(
                "sys", self._password, f"{endpoint.host}:{endpoint.port}/FREE", sysdba=True
            )
            with connection.cursor() as cursor:
                cursor.execute(compose("ALTER SESSION SET CONTAINER = {}", quote(container)))
                cursor.execute(
                    "ALTER SESSION SET TIME_ZONE = '+00:00' NLS_NUMERIC_CHARACTERS = '.,'"
                )
        except Error as error:
            raise database_error(error, "connecting to the working database") from None
        return connection

    def version(self) -> str:
        """The server's full version, such as ``23.26.3.0.0``."""
        with self.connect("CDB$ROOT") as connection, connection.cursor() as cursor:
            cursor.execute("SELECT version_full FROM product_component_version WHERE ROWNUM = 1")
            ((version,),) = cursor.fetchall()
        return str(version)

    def _root(self, *statements: str) -> None:
        with self.connect("CDB$ROOT") as connection, connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)

    def create(self) -> None:
        """Create an empty pluggable database in ``data/``, with a tablespace for the schemas."""
        self._root(
            compose(
                "CREATE PLUGGABLE DATABASE {} ADMIN USER {} IDENTIFIED BY {}"
                " FILE_NAME_CONVERT = ({}, {})",
                quote(PDB),
                quote("DD_ADMIN"),
                quote(self._password),
                literal(_SEED),
                literal(f"{self.data}/"),
            ),
            compose("ALTER PLUGGABLE DATABASE {} OPEN", quote(PDB)),
        )
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                compose(
                    "CREATE TABLESPACE {} DATAFILE {} SIZE 32M AUTOEXTEND ON",
                    quote("DATADOUBLE_DATA"),
                    literal(str(self.data / "datadouble_data01.dbf")),
                )
            )
            cursor.execute(
                compose("ALTER DATABASE DEFAULT TABLESPACE {}", quote("DATADOUBLE_DATA"))
            )

    def plug(self, unplugged_from: str) -> None:
        """Plug in the database whose manifest is in ``data/``.

        ``unplugged_from`` is where its files were, as the manifest names them.
        """
        self._root(
            compose(
                "CREATE PLUGGABLE DATABASE {} USING {} SOURCE_FILE_NAME_CONVERT = ({}, {})"
                " NOCOPY TEMPFILE REUSE",
                quote(PDB),
                literal(str(self.data / MANIFEST)),
                literal(f"{unplugged_from}/"),
                literal(f"{self.data}/"),
            ),
            compose("ALTER PLUGGABLE DATABASE {} OPEN", quote(PDB)),
        )

    def unplug(self) -> str:
        """Unplug the database into ``data/``, leaving consistent files; return where it was."""
        manifest = self.data / MANIFEST
        manifest.unlink(missing_ok=True)
        self._root(
            compose("ALTER PLUGGABLE DATABASE {} CLOSE IMMEDIATE", quote(PDB)),
            compose(
                "ALTER PLUGGABLE DATABASE {} UNPLUG INTO {}", quote(PDB), literal(str(manifest))
            ),
            compose("DROP PLUGGABLE DATABASE {} KEEP DATAFILES", quote(PDB)),
        )
        return str(self.data)
