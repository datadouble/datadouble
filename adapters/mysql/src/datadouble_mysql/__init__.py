# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The MySQL engine adapter for Datadouble.

Spec settings::

    source:
      engine: mysql
      connection:
        url: {env: SHOP_MYSQL_URL}   # mysql://user:password@host:3306/database, by reference

The catalog is the URL's database. Working databases, restores and
``mysqldump`` run in the official MySQL image of exactly the source's version
(``mysql:<version>``), or in ``source.image``, through the spec's container
runtime. ``mysqldump`` connects to the source from a container, so the source
must be reachable from there at the URL's address.
"""

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pymysql
from pydantic import BaseModel, ConfigDict, JsonValue

from datadouble.engine import EngineError, ServerPlan, SourceSession, Tooling, WorkingDatabases
from datadouble.names import EngineName
from datadouble.planner import Capabilities
from datadouble.secrets import resolve_secret
from datadouble.settings import NoSettings, parse_settings
from datadouble.spec import ImageFormat, SecretRef
from datadouble_mysql.errors import database_error
from datadouble_mysql.introspect import ENGINE
from datadouble_mysql.server import Tools, create_user, default_image
from datadouble_mysql.source import MysqlSession
from datadouble_mysql.working import DatabaseInfo, MysqlWorking

MOUNT = "/var/lib/datadouble"
"""Where container servers mount a copy."""


class MysqlConnection(BaseModel):
    """The ``source.connection`` settings."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    url: SecretRef


def parse_url(url: str) -> tuple[dict[str, str], str]:
    """Connection settings (host, port, user, password) and the database, from a URL."""
    parts = urlsplit(url)
    database = unquote(parts.path.lstrip("/"))
    if parts.scheme != "mysql" or not parts.hostname or not database:
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


class MysqlEngine:
    """Implements :class:`datadouble.engine.EngineAdapter` for MySQL."""

    @property
    def name(self) -> EngineName:
        """``mysql``."""
        return ENGINE

    def capabilities(self) -> Capabilities:
        """Both image formats, SQL expressions, the nested strategy and hooks."""
        return Capabilities(
            formats=(ImageFormat.PHYSICAL, ImageFormat.LOGICAL), expressions=True, nested=True
        )

    @contextmanager
    def connect(self, connection: Mapping[str, JsonValue]) -> Generator[SourceSession]:
        """Connect to the source."""
        settings = parse_settings(MysqlConnection, connection, "mysql connection")
        options, database = parse_url(resolve_secret(settings.url))
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
            yield MysqlSession(opened, options, database)
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
            f" && {create_user('datadouble')}"
            " && mysqladmin -uroot shutdown"
            f" && touch {ready}"
            f" && exec mysqld {options} --bind-address=*"
        )
        return ServerPlan(
            image=server_image or default_image(info.server_version),
            mount=MOUNT,
            owner="999:999",
            command=("sh", "-c", start),
            ready=("sh", "-c", f"test -f {ready} && mysqladmin -uroot ping"),
            port=3306,
            database=info.database,
            user="datadouble",
        )


def engine() -> MysqlEngine:
    """The entry point named in ``datadouble.engines``."""
    return MysqlEngine()
