# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The PostgreSQL engine adapter for Datadouble.

Spec settings::

    source:
      engine: postgres
      connection:
        dsn: {env: SHOP_DSN}      # a libpq connection string or URI, by reference

The server binaries that build and restore images (``initdb``, ``pg_ctl``,
``pg_dump``, ``pg_restore``) are found under ``/usr/lib/postgresql/<version>/bin``
(newest first), on ``PATH``, or in ``DATADOUBLE_POSTGRES_BINDIR``.
"""

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import BaseModel, ConfigDict, JsonValue

from datadouble.engine import SourceSession, WorkingDatabases
from datadouble.names import EngineName
from datadouble.planner import Capabilities
from datadouble.secrets import resolve_secret
from datadouble.settings import parse_settings
from datadouble.spec import ImageFormat, SecretRef
from datadouble_postgres.cluster import find_binaries
from datadouble_postgres.errors import database_error
from datadouble_postgres.introspect import ENGINE
from datadouble_postgres.source import PostgresSession
from datadouble_postgres.working import PostgresWorking


class PostgresConnection(BaseModel):
    """The ``source.connection`` settings."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    dsn: SecretRef


class PostgresEngine:
    """Implements :class:`datadouble.engine.EngineAdapter` for PostgreSQL."""

    @property
    def name(self) -> EngineName:
        """``postgres``."""
        return ENGINE

    def capabilities(self) -> Capabilities:
        """Both image formats, SQL expressions and the nested strategy."""
        return Capabilities(
            formats=(ImageFormat.PHYSICAL, ImageFormat.LOGICAL), expressions=True, nested=True
        )

    @contextmanager
    def connect(self, connection: Mapping[str, JsonValue]) -> Generator[SourceSession]:
        """Connect to the source; the password never reaches a command line."""
        settings = parse_settings(PostgresConnection, connection, "postgres connection")
        params = conninfo_to_dict(resolve_secret(settings.dsn))
        password = params.pop("password", None)
        conninfo = make_conninfo(**{k: str(v) for k, v in params.items()})
        try:
            opened = psycopg.connect(conninfo, password=password, autocommit=True)
        except psycopg.Error as error:
            raise database_error(error, "connecting to the source") from None
        with opened:
            yield PostgresSession(
                opened, conninfo, None if password is None else str(password), find_binaries()
            )

    def working(self, scratch: Path) -> WorkingDatabases:
        """Local clusters under ``scratch``."""
        return PostgresWorking(find_binaries(), scratch)


def engine() -> PostgresEngine:
    """The entry point named in ``datadouble.engines``."""
    return PostgresEngine()
