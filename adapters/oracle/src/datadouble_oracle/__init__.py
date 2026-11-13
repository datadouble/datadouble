# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The Oracle engine adapter for Datadouble.

Spec settings::

    source:
      engine: oracle
      connection:
        url: {env: SHOP_ORACLE_URL}   # oracle://user:password@host:1521/service, by reference
        schemas: [SHOP]               # the connecting user's own schema by default

The catalog is the named schemas, read through the service (a pluggable
database's service on a multitenant server). The connecting user needs
``SELECT_CATALOG_ROLE`` (for ``DBMS_METADATA``) and ``SELECT`` on the
tables. Extract runs no programs in containers. Working databases and
restores are pluggable databases in Oracle Database Free containers of
exactly the source's version
(``container-registry.oracle.com/database/free:23.26.3.0``), or in
``source.image``, through the spec's container runtime, as uid and gid 54321.
"""

import shlex
from collections.abc import Generator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, JsonValue

from datadouble.engine import EngineError, ServerPlan, SourceSession, Tooling, WorkingDatabases
from datadouble.names import EngineName
from datadouble.planner import Capabilities
from datadouble.secrets import resolve_secret
from datadouble.settings import NoSettings, parse_settings
from datadouble.spec import ImageFormat, SecretRef
from datadouble_oracle.driver import Error, connect
from datadouble_oracle.errors import database_error
from datadouble_oracle.introspect import ENGINE
from datadouble_oracle.render import compose, quote
from datadouble_oracle.server import DATA, MANIFEST, PDB, Tools, default_image, literal
from datadouble_oracle.source import OracleSession, configure
from datadouble_oracle.working import DatabaseInfo, OracleWorking

MOUNT = "/var/lib/datadouble"
"""Where container servers mount a copy."""

# Oracle's own start, then the copy plugged in and the login datadouble made
# with the copy's password, which reaches SQL*Plus on standard input.
_SERVE = """
set -eu
"$ORACLE_BASE/$RUN_FILE" &
trap 'printf "SHUTDOWN IMMEDIATE\\nEXIT\\n" | sqlplus -s / as sysdba > /dev/null; exit 0' TERM INT
until "$ORACLE_BASE/$CHECK_DB_FILE" > /dev/null 2>&1; do sleep 2; done
password=$(printf %s "$DATADOUBLE_PASSWORD" | tr -d '"')
sqlplus -s / as sysdba > /dev/null <<EOF
WHENEVER SQLERROR EXIT FAILURE
@PLUG@
ALTER SESSION SET CONTAINER = @PDB@;
CREATE USER datadouble IDENTIFIED BY "$password";
GRANT DBA TO datadouble;
EXIT
EOF
touch @READY@
wait
"""


class OracleConnection(BaseModel):
    """The ``source.connection`` settings."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    url: SecretRef
    schemas: tuple[str, ...] | None = None


def parse_url(url: str) -> dict[str, str]:
    """Connection settings (user, password, dsn) from a URL."""
    parts = urlsplit(url)
    service = unquote(parts.path.lstrip("/"))
    if parts.scheme != "oracle" or not parts.hostname or not service:
        message = "the oracle url must look like oracle://user:password@host:1521/service"
        raise EngineError(message)
    return {
        "user": unquote(parts.username or ""),
        "password": unquote(parts.password or ""),
        "dsn": f"{parts.hostname}:{parts.port or 1521}/{service}",
    }


class OracleEngine:
    """Implements :class:`datadouble.engine.EngineAdapter` for Oracle."""

    @property
    def name(self) -> EngineName:
        """``oracle``."""
        return ENGINE

    @property
    def owner(self) -> str:
        """``54321:54321``, the ``oracle`` user Oracle's image runs as."""
        return "54321:54321"

    def capabilities(self) -> Capabilities:
        """Both image formats, SQL expressions, the nested strategy and hooks."""
        return Capabilities(
            formats=(ImageFormat.PHYSICAL, ImageFormat.LOGICAL), expressions=True, nested=True
        )

    @contextmanager
    def connect(self, connection: Mapping[str, JsonValue]) -> Generator[SourceSession]:
        """Connect to the source."""
        settings = parse_settings(OracleConnection, connection, "oracle connection")
        options = parse_url(resolve_secret(settings.url))
        try:
            opened = configure(connect(options["user"], options["password"], options["dsn"]))
            with opened.cursor() as cursor:
                cursor.execute("SELECT USER FROM dual")
                ((user,),) = cursor.fetchall()
        except Error as error:
            raise database_error(error, "connecting to the source") from None
        try:
            yield OracleSession(opened, options, settings.schemas or (str(user),))
        finally:
            # Already closed, or the connection was lost: there is nothing
            # left to close, and the error that got here is the one to report.
            with suppress(Error):
                opened.close()

    def working(self, scratch: Path, tooling: Tooling, source_version: str) -> WorkingDatabases:
        """Pluggable databases under ``scratch``, in the image for the source's version."""
        parse_settings(NoSettings, tooling.server, "oracle server")
        image = tooling.image or default_image(source_version)
        return OracleWorking(Tools(tooling.runtime, image), scratch)

    def server(self, image: Path, server_image: str | None) -> ServerPlan:
        """Oracle's image of the image's version, with the copy plugged in.

        The copy's pluggable database is opened in the container's own
        container database, and the login ``datadouble`` made in it with the
        copy's password; clients connect to its service, ``DATADOUBLE``.
        """
        info = DatabaseInfo.model_validate_json((image / "database.json").read_bytes())
        data = f"{MOUNT}/{DATA}"
        plug = compose(
            "CREATE PLUGGABLE DATABASE {} USING {} SOURCE_FILE_NAME_CONVERT = ({}, {})"
            " NOCOPY TEMPFILE REUSE;\nALTER PLUGGABLE DATABASE {} OPEN;",
            quote(PDB),
            literal(f"{data}/{MANIFEST}"),
            literal(f"{info.unplugged_from}/"),
            literal(f"{data}/"),
            quote(PDB),
        )
        ready = f"{MOUNT}/ready"
        start = (
            _SERVE.replace("@PLUG@", plug.replace("$", "\\$"))
            .replace("@PDB@", quote(PDB))
            .replace("@READY@", shlex.quote(ready))
        )
        return ServerPlan(
            image=server_image or default_image(info.server_version),
            mount=MOUNT,
            owner=self.owner,
            command=("bash", "-c", start),
            ready=("test", "-f", ready),
            port=1521,
            database=PDB,
            user="datadouble",
        )


def engine() -> OracleEngine:
    """The entry point named in ``datadouble.engines``."""
    return OracleEngine()
