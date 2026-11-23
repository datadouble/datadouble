# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The PostgreSQL engine adapter for Datadouble.

Spec settings::

    source:
      engine: postgres
      connection:
        dsn: {env: SHOP_DSN}      # a libpq connection string or URI, by reference
        databases: [shop, "billing_*"]  # names or globs; by default the DSN's

Tables are named ``database.schema.table``; ``databases`` lists the databases
read (``["*"]`` for every one the user can connect to), each under a snapshot
of its own, opened together. Working databases, restores and ``pg_dump`` run
in the official PostgreSQL image of the source's exact version
(``postgres:<version>-trixie``), or in ``source.image``, through the spec's
container runtime. ``pg_dump`` connects to
the source from a container, so the source must be reachable from there at the
address in ``dsn``.
"""

from collections.abc import Generator, Mapping
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Annotated

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from datadouble.engine import (
    ServerPlan,
    SourceSession,
    Tooling,
    WorkingDatabases,
    choose_databases,
)
from datadouble.names import EngineName
from datadouble.planner import AccountNames, Capabilities
from datadouble.secrets import resolve_secret
from datadouble.settings import parse_settings
from datadouble.spec import ImageFormat, SecretRef
from datadouble_postgres.cluster import SUPERUSER, Tools, default_image
from datadouble_postgres.errors import database_error
from datadouble_postgres.introspect import ENGINE
from datadouble_postgres.source import Connection, PostgresSession
from datadouble_postgres.working import DatabaseInfo, PostgresWorking

MOUNT = "/var/lib/datadouble"
"""Where container servers mount a copy."""


class PostgresConnection(BaseModel):
    """The ``source.connection`` settings."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    dsn: SecretRef
    databases: tuple[Annotated[str, Field(min_length=1)], ...] = ()
    """The databases to read, as names or globs (``["*"]`` for every one the
    user can connect to); the connection string's database when empty."""


_DATABASES = """
SELECT datname FROM pg_catalog.pg_database
WHERE NOT datistemplate AND datallowconn
  AND pg_catalog.has_database_privilege(datname, 'CONNECT')
"""


def _open(conninfo: str, password: str | None) -> Connection:
    try:
        return psycopg.connect(conninfo, password=password, autocommit=True)
    except psycopg.Error as error:
        raise database_error(error, "connecting to the source") from None


def list_databases(connection: Connection) -> tuple[str, ...]:
    """Every database the user can connect to, templates aside."""
    try:
        return tuple(str(row[0]) for row in connection.execute(_DATABASES).fetchall())
    except psycopg.Error as error:
        raise database_error(error, "listing the databases") from None


class PostgresServerSettings(BaseModel):
    """The ``source.server`` settings."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    shared_preload_libraries: tuple[Annotated[str, Field(pattern=r"^[A-Za-z0-9_$.-]+$")], ...] = ()
    """Libraries the server loads at start, for extensions that need them
    (``timescaledb``, ``pg_cron``). A physical image keeps the setting, so its
    copies load them too."""


class PostgresEngine:
    """Implements :class:`datadouble.engine.EngineAdapter` for PostgreSQL."""

    @property
    def name(self) -> EngineName:
        """``postgres``."""
        return ENGINE

    @property
    def owner(self) -> str:
        """``999:999``, the user the official PostgreSQL image serves as."""
        return "999:999"

    def capabilities(self) -> Capabilities:
        """Both formats, SQL expressions, accounts and the nested strategy within one database."""
        return Capabilities(
            formats=(ImageFormat.PHYSICAL, ImageFormat.LOGICAL),
            expressions=True,
            nested=True,
            accounts=AccountNames(
                max_bytes=63,
                reserved=("public", "none", "current_user", "session_user", "current_role"),
                reserved_prefixes=("pg_",),
                keep_all=(SUPERUSER,),
            ),
            joins_outermost=False,
        )

    @contextmanager
    def connect(self, connection: Mapping[str, JsonValue]) -> Generator[SourceSession]:
        """Connect to the source; the password never reaches a command line."""
        settings = parse_settings(PostgresConnection, connection, "postgres connection")
        params = conninfo_to_dict(resolve_secret(settings.dsn))
        secret = params.pop("password", None)
        password = None if secret is None else str(secret)
        conninfo = make_conninfo(**{k: str(v) for k, v in params.items()})
        with ExitStack() as stack:
            first = stack.enter_context(_open(conninfo, password))
            home = str(first.info.dbname)
            wanted = settings.databases or (home,)
            databases = choose_databases(list_databases(first), wanted)
            connections = {
                database: first
                if database == home
                else stack.enter_context(_open(make_conninfo(conninfo, dbname=database), password))
                for database in databases
            }
            default = home if home in connections else databases[0]
            yield PostgresSession(connections, conninfo, password, default)

    def working(self, scratch: Path, tooling: Tooling, source_version: str) -> WorkingDatabases:
        """Clusters under ``scratch``, in the image for the source's version."""
        settings = parse_settings(PostgresServerSettings, tooling.server, "postgres server")
        image = tooling.image or default_image(source_version)
        tools = Tools(tooling.runtime, image, scratch, settings.shared_preload_libraries)
        return PostgresWorking(tools, scratch)

    def server(self, image: Path, server_image: str | None) -> ServerPlan:
        """The official PostgreSQL image of the image's version, on the copy.

        At start the data directory gets the permissions PostgreSQL insists on,
        a pg_hba file that allows password logins from anywhere and local trust,
        and the superuser gets the copy's password.
        """
        info = DatabaseInfo.model_validate_json((image / "database.json").read_bytes())
        data = f"{MOUNT}/pgdata"
        hba = f"{data}/pg_hba.datadouble.conf"
        # The password is generated by the replicate adapter (hex), so quoting it is safe.
        start = (
            f"chmod 0700 {data}"
            f" && printf 'local all all trust\\nhost all all all scram-sha-256\\n' > {hba}"
            f' && printf "ALTER ROLE {SUPERUSER} PASSWORD \'%s\'\\n" "$DATADOUBLE_PASSWORD"'
            f" | postgres --single -D {data} postgres > /dev/null"
            f" && exec postgres -D {data} -c listen_addresses='*'"
            f" -c hba_file={hba}"
        )
        return ServerPlan(
            image=server_image or default_image(info.server_version),
            mount=MOUNT,
            owner=self.owner,
            command=("sh", "-c", start),
            ready=("pg_isready", "--host=127.0.0.1", f"--dbname={info.default}"),
            port=5432,
            database=info.default,
            user=SUPERUSER,
        )


def engine() -> PostgresEngine:
    """The entry point named in ``datadouble.engines``."""
    return PostgresEngine()
