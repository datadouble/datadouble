# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The MySQL engine adapter for Datadouble.

Spec settings::

    sources:
      app:
        engine: mysql
        connection:
          url: {env: SHOP_MYSQL_URL}   # mysql://user:password@host:3306/database, by reference
          databases: [shop, "billing_*"]  # names or globs; the URL's database by default

The catalog spans the databases ``databases`` lists (``["*"]`` for every one
but MySQL's own), or the URL's database; each table is named
``database.table``. Working databases, restores and ``mysqldump`` run in the
official MySQL image of exactly the source's version (``mysql:<version>``), or
in ``sources.<name>.image``, through the spec's container runtime. ``mysqldump``
connects to the source from a container, so the source must be reachable from
there at the URL's address.
"""

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import unquote, urlsplit

import pymysql
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from pymysql.connections import Connection

from datadouble.engine import (
    EngineError,
    ServerPlan,
    SourceSession,
    Tooling,
    WorkingDatabases,
    choose_databases,
)
from datadouble.names import EngineName
from datadouble.planner import AccountNames, Capabilities
from datadouble.secrets import resolve_secret
from datadouble.settings import NoSettings, parse_settings
from datadouble.spec import ImageFormat, SecretRef
from datadouble_mysql.errors import database_error
from datadouble_mysql.introspect import ENGINE, SYSTEM_DATABASES
from datadouble_mysql.server import USER, Tools, create_user, default_image
from datadouble_mysql.source import MysqlSession
from datadouble_mysql.working import DatabaseInfo, MysqlWorking

MOUNT = "/var/lib/datadouble"
"""Where container servers mount a copy."""
COPY_USER = "datadouble"
"""The user an instance copy's server gets, with the copy's password."""


class MysqlConnection(BaseModel):
    """The ``sources.<name>.connection`` settings."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    url: SecretRef
    databases: tuple[Annotated[str, Field(min_length=1)], ...] = ()
    """The databases to read, as names or globs (``["*"]`` for all but MySQL's
    own); the URL's database when empty."""


def parse_url(url: str) -> tuple[dict[str, str], str | None]:
    """Connection settings (host, port, user, password) and the database, if any, from a URL."""
    parts = urlsplit(url)
    database = unquote(parts.path.lstrip("/")) or None
    if parts.scheme != "mysql" or not parts.hostname:
        message = "the mysql url must look like mysql://user:password@host:3306/database"
        raise EngineError(message)
    return (
        {
            "host": parts.hostname,
            "port": str(parts.port or 3306),
            "user": unquote(parts.username or ""),
            "password": unquote(parts.password or ""),
        },
        database,
    )


def list_databases(connection: Connection) -> tuple[str, ...]:
    """Every database the user can see."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA")
            return tuple(str(row[0]) for row in cursor.fetchall())
    except pymysql.MySQLError as error:
        raise database_error(error, "listing the databases") from None


class MysqlEngine:
    """Implements :class:`datadouble.engine.EngineAdapter` for MySQL."""

    @property
    def name(self) -> EngineName:
        """``mysql``."""
        return ENGINE

    @property
    def owner(self) -> str:
        """``999:999``, the user the official MySQL image serves as."""
        return "999:999"

    def capabilities(self) -> Capabilities:
        """Both image formats, SQL expressions, the nested strategy, hooks and accounts."""
        # Minor releases (8.0, 8.4) change the data directory; 8.0 cannot open 8.4's.
        return Capabilities(
            formats=(ImageFormat.PHYSICAL, ImageFormat.LOGICAL),
            expressions=True,
            nested=True,
            version_line=2,
            accounts=AccountNames(max_characters=32, reserved=(USER, COPY_USER)),
            standalone=True,
            several_sources=True,
        )

    @contextmanager
    def connect(self, connection: Mapping[str, JsonValue]) -> Generator[SourceSession]:
        """Connect to the source and find the databases to read."""
        settings = parse_settings(MysqlConnection, connection, "mysql connection")
        options, database = parse_url(resolve_secret(settings.url))
        if not settings.databases and database is None:
            message = "name a database in the mysql url, or list connection.databases"
            raise EngineError(message)
        try:
            opened = pymysql.connect(
                host=options["host"],
                port=int(options["port"]),
                user=options["user"],
                password=options["password"],
                database=database,
                charset="utf8mb4",
                autocommit=True,
            )
        except pymysql.MySQLError as error:
            raise database_error(error, "connecting to the source") from None
        try:
            wanted = settings.databases or (str(database),)
            databases = choose_databases(list_databases(opened), wanted, SYSTEM_DATABASES)
            default = database if database in databases else databases[0]
            yield MysqlSession(opened, options, databases, default)
        finally:
            if opened.open:
                opened.close()

    def working(self, scratch: Path, tooling: Tooling, source_version: str) -> WorkingDatabases:
        """Servers under ``scratch``, in the image for the source's version."""
        parse_settings(NoSettings, tooling.server, "mysql server")
        image = tooling.image or default_image(source_version)
        return MysqlWorking(Tools(tooling.runtime, image), scratch)

    def server(self, image: Path, server_image: str | None) -> ServerPlan:
        """The official MySQL image of the image's version, on the copy.

        At start the user ``datadouble`` is created on the copy, or its
        password reset, to the copy's password, and the server then listens on
        the network.
        """
        info = DatabaseInfo.model_validate_json((image / "database.json").read_bytes())
        data = f"{MOUNT}/mysql"
        ready = f"{MOUNT}/.datadouble-ready"
        options = f"--datadir={data} --mysqlx=OFF"
        start = (
            f"rm -f {ready}"
            f" && mysqld {options} --bind-address=127.0.0.1 --daemonize > /dev/null"
            f" && {create_user(COPY_USER)}"
            " && mysqladmin -uroot shutdown"
            f" && touch {ready}"
            f" && exec mysqld {options} --bind-address=*"
        )
        return ServerPlan(
            image=server_image or default_image(info.server_version),
            mount=MOUNT,
            owner=self.owner,
            command=("sh", "-c", start),
            ready=("sh", "-c", f"test -f {ready} && mysqladmin -uroot ping"),
            port=3306,
            database=info.default,
            user=COPY_USER,
        )


def engine() -> MysqlEngine:
    """The entry point named in ``datadouble.engines``."""
    return MysqlEngine()
