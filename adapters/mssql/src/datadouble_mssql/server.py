# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Throwaway SQL Server instances in Microsoft's SQL Server image.

Each start is a new instance: its system databases (``master``, ``model``,
``msdb``, ``tempdb``) are created in a private directory beside the working
database and removed when it stops. The working database's own data and log
files live in ``data/``; they are attached at start and detached before the
instance stops, so an image is just those files and carries no logins, server
settings or system databases. ``sa`` gets a password generated for that start,
and Datadouble connects with it over the network.

The image runs SQL Server as its ``mssql`` user, uid and gid 10001, and the
server refuses to start as any other user, so Datadouble must run as
10001:10001 too (its own image does), for the files each side writes to be
usable by the other.
"""

import secrets
import shutil
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pymssql
from pymssql import Connection

from datadouble.engine import EngineError
from datadouble.runtime import Container, ContainerRuntime, Server, identity
from datadouble_mssql.errors import database_error
from datadouble_mssql.render import compose, quote

IDENTITY = "10001:10001"
"""The user and group SQL Server runs as in Microsoft's image."""
DATA = "data"
"""The directory holding a working database's files."""
_DATA_FILE = "database.mdf"
_LOG_FILE = "database_log.ldf"
_PORT = 1433
_SA_VARIABLE = "MSSQL_SA_PASSWORD"
_YEARS = {"15": "2019", "16": "2022", "17": "2025"}
# sqlcmd moved to mssql-tools18, which needs -C to trust the instance's own
# certificate; older images have mssql-tools.
_SQLCMD = (
    'if [ -x /opt/mssql-tools18/bin/sqlcmd ]; then exec /opt/mssql-tools18/bin/sqlcmd -C "$@";'
    ' else exec /opt/mssql-tools/bin/sqlcmd "$@"; fi'
)
# SQL Server mangles directories in its settings whose path has a component
# starting with a dot (/w/.datadouble/x becomes /w.datadouble/x), which the
# default work directory has, so they point at links in the image's own state
# directory, which only its user can write.
_LINKS = "/var/opt/mssql/datadouble"
_START = (
    f'mkdir -p {_LINKS} && ln -s "$1" {_LINKS}/instance && ln -s "$2" {_LINKS}/data'
    " && exec /opt/mssql/bin/sqlservr"
)
# Ready once startup has finished upgrading the system databases, which a stop
# would interrupt, and the instance accepts sa.
_READY = (
    "sh",
    "-c",
    (
        "grep -q 'Recovery is complete' /var/opt/mssql/log/errorlog || exit 1;"
        f' SQLCMDPASSWORD="${_SA_VARIABLE}"; export SQLCMDPASSWORD;'
        f' set -- -S localhost -U sa -b -Q "SELECT 1"; {_SQLCMD}'
    ),
)


def default_image(server_version: str) -> str:
    """Microsoft's image of a version such as ``16.0.4325.4-CU28``.

    Image tags name the release and cumulative update
    (``2022-CU28-ubuntu-22.04``), not the build; a source on a security
    update (GDR) needs ``source.image``. A major version alone (``16``) gets
    the release's latest image.
    """
    build, _, level = server_version.partition("-")
    year = _YEARS.get(build.split(".")[0])
    if year is None:
        message = (
            f"no SQL Server image is known for version {server_version}; set source.image to"
            " Microsoft's image of that version"
        )
        raise EngineError(message)
    if not level:
        # Only the major version, as image identities record it.
        return f"mcr.microsoft.com/mssql/server:{year}-latest"
    return f"mcr.microsoft.com/mssql/server:{year}-{level}-ubuntu-22.04"


def literal(text: str) -> str:
    """A Unicode string literal, for file paths and names in DDL."""
    return "N'" + text.replace("'", "''") + "'"


SESSION = (
    "SET ANSI_NULLS, ANSI_PADDING, ANSI_WARNINGS, ARITHABORT, CONCAT_NULL_YIELDS_NULL,"
    " QUOTED_IDENTIFIER ON; SET NUMERIC_ROUNDABORT OFF; SET TEXTSIZE 2147483647"
)
"""Settings every connection starts with: those computed columns, filtered
indexes and indexed views need, and no limit on the size of text values, which
the driver would otherwise truncate."""


def configure(connection: Connection) -> Connection:
    """``connection``, with the session settings applied."""
    with connection.cursor() as cursor:
        cursor.execute(SESSION)
    return connection


def product_version(connection: Connection) -> str:
    """The server's build and update level, such as ``16.0.4325.4-CU28`` or ``…-RTM``."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT CAST(SERVERPROPERTY('ProductVersion') AS NVARCHAR(40)),"
            " CAST(COALESCE(SERVERPROPERTY('ProductUpdateLevel'), SERVERPROPERTY('ProductLevel'))"
            " AS NVARCHAR(40))"
        )
        ((build, level),) = cursor.fetchall()
    return f"{build!s}-{level!s}"


def check_identity() -> None:
    """Fail unless Datadouble runs as SQL Server's user and group."""
    if identity() != IDENTITY:
        message = (
            f"SQL Server working databases need Datadouble to run as {IDENTITY}, the user and"
            f" group Microsoft's SQL Server image runs as; it runs as {identity()}. Datadouble's"
            " own image runs as 10001:10001."
        )
        raise EngineError(message)


@dataclass(frozen=True, slots=True)
class Tools:
    """SQL Server instances from one image."""

    runtime: ContainerRuntime
    image: str
    edition: str = "Developer"
    env: dict[str, str] = field(default_factory=dict[str, str])


class MssqlServer:
    """One instance serving the working database in ``directory``."""

    def __init__(
        self, tools: Tools, directory: Path, owner: Path, mounts: tuple[Path, ...] = ()
    ) -> None:
        """Serve ``directory`` for the stage directory ``owner``.

        ``mounts`` are further paths the instance reads, such as data files to load.
        """
        self.tools = tools
        self.directory = directory
        self.owner = owner
        self.mounts = (directory, *mounts)
        self._server: Server | None = None
        self._password = ""

    @property
    def data(self) -> Path:
        """The directory of the database's data and log files."""
        return self.directory / DATA

    @property
    def server(self) -> Server:
        """The running instance's container."""
        if self._server is None:
            message = "the server is not running"
            raise EngineError(message)
        return self._server

    @contextmanager
    def running(self) -> Generator["MssqlServer"]:
        """Start a new instance for the block, then shut it down cleanly."""
        self.data.mkdir(parents=True, exist_ok=True)
        private = self.directory / f".mssql-{secrets.token_hex(4)}"
        private.mkdir(mode=0o700)
        # Upper and lower case, digits and a symbol, as SQL Server's policy wants.
        password = f"Dd-{secrets.token_hex(16)}"
        container = Container(
            image=self.tools.image,
            command=("sh", "-c", _START, "sh", str(private), str(self.data)),
            owner=self.owner,
            mounts=self.mounts,
            env={
                "ACCEPT_EULA": "Y",
                "MSSQL_PID": self.tools.edition,
                "MSSQL_MASTER_DATA_FILE": f"{_LINKS}/instance/master.mdf",
                "MSSQL_MASTER_LOG_FILE": f"{_LINKS}/instance/mastlog.ldf",
                "MSSQL_DATA_DIR": f"{_LINKS}/data",
                "MSSQL_LOG_DIR": f"{_LINKS}/data",
                "MSSQL_DUMP_DIR": f"{_LINKS}/instance",
                "MSSQL_BACKUP_DIR": f"{_LINKS}/instance",
                **self.tools.env,
            },
            secrets={_SA_VARIABLE: password},
            port=_PORT,
            ready=_READY,
        )
        try:
            with self.tools.runtime.serve(container) as server:
                self._server, self._password = server, password
                try:
                    yield self
                finally:
                    self._server, self._password = None, ""
        finally:
            shutil.rmtree(private, ignore_errors=True)

    def connect(self, database: str = "master") -> Connection:
        """Connect to the running instance as ``sa``."""
        endpoint = self.server.endpoint
        try:
            return configure(
                pymssql.connect(
                    server=endpoint.host,
                    port=str(endpoint.port),
                    user="sa",
                    password=self._password,
                    database=database,
                    autocommit=True,
                )
            )
        except pymssql.Error as error:
            raise database_error(error, "connecting to the working database") from None

    def version(self) -> str:
        """The instance's version, such as ``16.0.4325.4-CU28``."""
        with self.connect() as connection:
            return product_version(connection)

    def _execute(self, connection: Connection, action: str, statement: str) -> None:
        try:
            with connection.cursor() as cursor:
                cursor.execute(statement)
        except pymssql.Error as error:
            raise database_error(error, action) from None

    def create(self, database: str, collation: str) -> None:
        """Create an empty database, with simple recovery, in ``data/``."""
        with self.connect() as connection:
            self._execute(
                connection,
                f"creating {database}",
                compose(
                    "CREATE DATABASE {} ON PRIMARY (NAME = {}, FILENAME = {})"
                    " LOG ON (NAME = {}, FILENAME = {}) COLLATE {}",
                    quote(database),
                    quote("data"),
                    literal(str(self.data / _DATA_FILE)),
                    quote("log"),
                    literal(str(self.data / _LOG_FILE)),
                    collation,
                ),
            )
            self._execute(
                connection,
                f"configuring {database}",
                compose("ALTER DATABASE {} SET RECOVERY SIMPLE", quote(database)),
            )

    def attach(self, database: str) -> None:
        """Attach the database whose files are in ``data/``."""
        with self.connect() as connection:
            self._execute(
                connection,
                f"attaching {database}",
                compose(
                    "CREATE DATABASE {} ON (FILENAME = {}), (FILENAME = {}) FOR ATTACH",
                    quote(database),
                    literal(str(self.data / _DATA_FILE)),
                    literal(str(self.data / _LOG_FILE)),
                ),
            )

    def detach(self, database: str) -> None:
        """Detach the database, leaving consistent files in ``data/``."""
        with self.connect() as connection:
            self._execute(
                connection,
                f"closing {database}",
                compose(
                    "ALTER DATABASE {} SET SINGLE_USER WITH ROLLBACK IMMEDIATE", quote(database)
                ),
            )
            self._execute(
                connection,
                f"detaching {database}",
                compose("EXEC sp_detach_db @dbname = {}, @skipchecks = 'true'", literal(database)),
            )
