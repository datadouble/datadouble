# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The part of python-oracledb this adapter uses, precisely typed.

The driver's own annotations leave parameter and row types unknown, which
strict checking reports at every call; the adapter goes through these
protocols instead, and only this module touches the driver's connections.
"""

import datetime as dt
from collections.abc import Iterator
from decimal import Decimal
from types import TracebackType
from typing import Protocol, Self

import oracledb

type Value = str | int | float | Decimal | bytes | dt.datetime | dt.date | dt.timedelta | None
"""A value the driver binds or returns."""
type Row = tuple[object, ...]
type Binds = dict[str, Value] | list[Value]

Error = oracledb.Error
SYSDBA = oracledb.AUTH_MODE_SYSDBA


class Cursor(Protocol):
    """A cursor."""

    def execute(self, statement: str, parameters: Binds | None = None, /) -> object:
        """Run a statement."""
        ...

    def executemany(self, statement: str, parameters: list[list[Value]], /) -> object:
        """Run a statement for each row of bind values."""
        ...

    def fetchall(self) -> list[Row]:
        """The remaining rows."""
        ...

    def __iter__(self) -> Iterator[Row]:
        """Rows as they stream."""
        ...

    def __enter__(self) -> Self:
        """The cursor, closed on leaving the block."""
        ...

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
        /,
    ) -> object:
        """Close the cursor."""
        ...


class Connection(Protocol):
    """A connection."""

    def cursor(self) -> Cursor:
        """A new cursor."""
        ...

    def commit(self) -> None:
        """Commit the transaction."""
        ...

    def rollback(self) -> None:
        """Roll the transaction back."""
        ...

    def close(self) -> None:
        """Close the connection."""
        ...

    def __enter__(self) -> Self:
        """The connection, closed on leaving the block."""
        ...

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
        /,
    ) -> object:
        """Close the connection."""
        ...


def connect(user: str, password: str, dsn: str, *, sysdba: bool = False) -> Connection:
    """A connection that returns LOBs as strings and bytes."""
    opened: Connection = oracledb.connect(
        user=user,
        password=password,
        dsn=dsn,
        mode=SYSDBA if sysdba else oracledb.AUTH_MODE_DEFAULT,
    )
    return opened


def read(value: object) -> object:
    """``value``, or a LOB's whole contents."""
    if isinstance(value, oracledb.LOB):
        contents: object = value.read()
        return contents
    return value
