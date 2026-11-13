# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The SQL Server engine adapter for Datadouble.

Spec settings::

    source:
      engine: mssql
      connection:
        url: {env: SHOP_MSSQL_URL}   # mssql://user:password@host:1433/database, by reference
      server:
        accept_eula: true            # required: you accept the SQL Server licence
        edition: Developer           # the default; any value MSSQL_PID takes

The catalog is the URL's database, which must allow snapshot isolation.
Extract runs no programs in containers. Working databases and restores run in
Microsoft's SQL Server image of the source's release and cumulative update
(``mcr.microsoft.com/mssql/server:2022-CU28-ubuntu-22.04``), or in
``source.image``, through the spec's container runtime, as uid and gid 10001,
which Datadouble must run as too. They run SQL Server under its licence terms,
which the operator accepts with ``source.server.accept_eula``; Developer
edition is licensed for development and testing, not production.
"""

import shlex
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

import pymssql
from pydantic import BaseModel, ConfigDict, JsonValue

from datadouble.engine import EngineError, ServerPlan, SourceSession, Tooling, WorkingDatabases
from datadouble.names import EngineName
from datadouble.planner import Capabilities
from datadouble.secrets import resolve_secret
from datadouble.settings import parse_settings
from datadouble.spec import ImageFormat, SecretRef
from datadouble_mssql.errors import database_error
from datadouble_mssql.introspect import ENGINE
from datadouble_mssql.render import compose, quote
from datadouble_mssql.server import (
    DATA,
    IDENTITY,
    Tools,
    check_identity,
    configure,
    default_image,
    literal,
)
from datadouble_mssql.source import MssqlSession
from datadouble_mssql.working import DatabaseInfo, MssqlWorking

MOUNT = "/var/lib/datadouble"
"""Where container servers mount a copy."""

# A new instance beside the copy's files, with a random sa password; the
# database is attached and the login datadouble made with the copy's password,
# which reaches the server through a builtin and a pipe, never a command line.
_SERVE = """
set -eu
instance=@INSTANCE@
rm -rf "$instance" && mkdir -p "$instance"
export ACCEPT_EULA=Y MSSQL_PID=@EDITION@ MSSQL_DATA_DIR=@DATA@ MSSQL_LOG_DIR=@DATA@
export MSSQL_MASTER_DATA_FILE="$instance/master.mdf" MSSQL_MASTER_LOG_FILE="$instance/mastlog.ldf"
export MSSQL_DUMP_DIR="$instance"
MSSQL_SA_PASSWORD="Dd-$(od -An -N16 -tx1 /dev/urandom | tr -d ' \\n')"
export MSSQL_SA_PASSWORD SQLCMDPASSWORD="$MSSQL_SA_PASSWORD"
sqlcmd() {
    if [ -x /opt/mssql-tools18/bin/sqlcmd ]; then /opt/mssql-tools18/bin/sqlcmd -C "$@"
    else /opt/mssql-tools/bin/sqlcmd "$@"; fi
}
/opt/mssql/bin/sqlservr & server=$!
trap 'kill -TERM "$server"; wait "$server"' TERM INT
until grep -q 'Recovery is complete' /var/opt/mssql/log/errorlog 2> /dev/null \\
    && sqlcmd -S localhost -U sa -b -Q "SELECT 1" > /dev/null 2>&1; do sleep 1; done
password=$(printf %s "$DATADOUBLE_PASSWORD" | sed "s/'/''/g")
printf '%s\\n' "@ATTACH@" | sqlcmd -S localhost -U sa -b -I > /dev/null
touch "$instance/ready"
wait "$server"
"""


class MssqlConnection(BaseModel):
    """The ``source.connection`` settings."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    url: SecretRef


class MssqlServerSettings(BaseModel):
    """The ``source.server`` settings."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    accept_eula: Literal[True] | None = None
    """Set by the operator to accept the SQL Server licence; required."""
    edition: str = "Developer"


def double_quoted(text: str) -> str:
    """``text`` escaped for a double-quoted shell string, so nothing in it expands."""
    for special in ("\\", '"', "$", "`"):
        text = text.replace(special, "\\" + special)
    return text


def parse_url(url: str) -> tuple[dict[str, str], str]:
    """Connection settings (server, port, user, password) and the database, from a URL."""
    parts = urlsplit(url)
    database = unquote(parts.path.lstrip("/"))
    if parts.scheme != "mssql" or not parts.hostname or not database:
        message = "the mssql url must look like mssql://user:password@host:1433/database"
        raise EngineError(message)
    return (
        {
            "server": parts.hostname,
            "port": str(parts.port or 1433),
            "user": unquote(parts.username or ""),
            "password": unquote(parts.password or ""),
        },
        database,
    )


def server_settings(server: Mapping[str, JsonValue]) -> MssqlServerSettings:
    """The ``source.server`` settings, which must accept the licence."""
    settings = parse_settings(MssqlServerSettings, server, "mssql server")
    if settings.accept_eula is None:
        message = (
            "SQL Server working databases run Microsoft's SQL Server image, whose licence the"
            " operator must accept: set source.server.accept_eula to true (Developer edition,"
            " the default, is licensed for development and testing)"
        )
        raise EngineError(message)
    return settings


class MssqlEngine:
    """Implements :class:`datadouble.engine.EngineAdapter` for SQL Server."""

    @property
    def name(self) -> EngineName:
        """``mssql``."""
        return ENGINE

    def capabilities(self) -> Capabilities:
        """Both image formats, SQL expressions, the nested strategy and hooks."""
        return Capabilities(
            formats=(ImageFormat.PHYSICAL, ImageFormat.LOGICAL), expressions=True, nested=True
        )

    @contextmanager
    def connect(self, connection: Mapping[str, JsonValue]) -> Generator[SourceSession]:
        """Connect to the source."""
        settings = parse_settings(MssqlConnection, connection, "mssql connection")
        options, database = parse_url(resolve_secret(settings.url))
        try:
            opened = configure(
                pymssql.connect(
                    server=options["server"],
                    port=options["port"],
                    user=options["user"],
                    password=options["password"],
                    database=database,
                    autocommit=True,
                )
            )
        except pymssql.Error as error:
            raise database_error(error, "connecting to the source") from None
        try:
            yield MssqlSession(opened, options, database)
        finally:
            opened.close()

    def working(self, scratch: Path, tooling: Tooling, source_version: str) -> WorkingDatabases:
        """Instances under ``scratch``, in the image for the source's version."""
        settings = server_settings(tooling.server)
        check_identity()
        image = tooling.image or default_image(source_version)
        return MssqlWorking(Tools(tooling.runtime, image, settings.edition), scratch)

    def server(self, image: Path, server_image: str | None) -> ServerPlan:
        """Microsoft's image of the image's version, attaching the copy's files.

        At start a new instance is made in a directory beside the files, with a
        random ``sa`` password; the database is attached, and the login
        ``datadouble`` is created with the copy's password.
        """
        info = DatabaseInfo.model_validate_json((image / "database.json").read_bytes())
        data = f"{MOUNT}/{DATA}"
        attach = compose(
            "CREATE DATABASE {} ON (FILENAME = {}), (FILENAME = {}) FOR ATTACH;"
            " CREATE LOGIN [datadouble] WITH PASSWORD = N'@PASSWORD@', CHECK_POLICY = OFF;"
            " ALTER SERVER ROLE sysadmin ADD MEMBER [datadouble];",
            quote(info.database),
            literal(f"{data}/database.mdf"),
            literal(f"{data}/database_log.ldf"),
        )
        start = (
            _SERVE.replace("@INSTANCE@", f"{MOUNT}/instance")
            .replace("@DATA@", data)
            .replace("@EDITION@", shlex.quote(info.edition))
            .replace("@ATTACH@", double_quoted(attach).replace("@PASSWORD@", "$password"))
        )
        return ServerPlan(
            image=server_image or default_image(info.server_version),
            mount=MOUNT,
            owner=IDENTITY,
            command=("bash", "-c", start),
            ready=("test", "-f", f"{MOUNT}/instance/ready"),
            port=1433,
            database=info.database,
            user="datadouble",
        )


def engine() -> MssqlEngine:
    """The entry point named in ``datadouble.engines``."""
    return MssqlEngine()
