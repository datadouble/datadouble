# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The part of redis-py this adapter uses, with replies made concrete.

redis-py's methods return either a reply or an awaitable, which strict
checking cannot narrow, so the adapter sends raw commands through
:class:`Client`, with redis-py's reply parsing turned off, and reads the raw
replies with the helpers here. Keys and
values stay bytes; text is decoded with ``surrogateescape``, so bytes that
are not UTF-8 survive a round trip unchanged.
"""

import re
from collections.abc import Generator, MutableMapping, Sequence
from contextlib import contextmanager
from typing import Protocol, cast
from urllib.parse import unquote, urlsplit

import redis
from pydantic import TypeAdapter
from redis.exceptions import RedisError

from datadouble.engine import EngineError

type Argument = str | bytes | int | float
type Call = Sequence[Argument]

Error = RedisError
_CODE = re.compile(r"[A-Z][A-Z_]+")
_SCHEMES = {"redis": False, "rediss": True}
_ITEMS: TypeAdapter[list[object]] = TypeAdapter(list[object])


class _Pipeline(Protocol):
    def execute_command(self, *arguments: Argument) -> object: ...

    def execute(self, *, raise_on_error: bool = True) -> list[object]: ...


class _Connection(Protocol):
    response_callbacks: MutableMapping[str, object]

    def execute_command(self, *arguments: Argument) -> object: ...

    def pipeline(self, *, transaction: bool = True) -> _Pipeline: ...

    def close(self) -> None: ...


class Client:
    """A connection to one database of a server."""

    def __init__(self, connection: _Connection) -> None:
        """Wrap a connection."""
        self._redis = connection

    @classmethod
    def from_url(cls, url: str, database: int) -> "Client":
        """A client for ``database`` of the server at a ``redis://`` or ``rediss://`` URL."""
        parts = urlsplit(url)
        if parts.scheme not in _SCHEMES or not parts.hostname:
            message = "a Redis URL is redis://[user:password@]host[:port] or rediss://..."
            raise EngineError(message)
        return cls._made(
            redis.Redis(
                host=parts.hostname,
                port=parts.port or 6379,
                username=unquote(parts.username) if parts.username else None,
                password=unquote(parts.password) if parts.password else None,
                ssl=_SCHEMES[parts.scheme],
                db=database,
                socket_timeout=60,
                protocol=2,
            )
        )

    @classmethod
    def at(cls, host: str, port: int, password: str, database: int) -> "Client":
        """A client for ``database`` of a working server."""
        return cls._made(
            redis.Redis(host=host, port=port, password=password, db=database, protocol=2)
        )

    @classmethod
    def _made(cls, connection: redis.Redis) -> "Client":
        # redis-py annotates replies as awaitables and leaves execute_command untyped.
        narrowed = cast("_Connection", connection)  # why: see the comment above
        # Raw RESP2 replies: redis-py otherwise turns some into dicts, sets and floats.
        narrowed.response_callbacks.clear()
        return cls(narrowed)

    def command(self, *arguments: Argument) -> object:
        """Send one command and return its reply."""
        return self._redis.execute_command(*arguments)

    def commands(self, calls: Sequence[Call]) -> list[object]:
        """Send several commands in one round trip, without a transaction."""
        pipeline = self._redis.pipeline(transaction=False)
        for call in calls:
            pipeline.execute_command(*call)
        return pipeline.execute(raise_on_error=True)

    def close(self) -> None:
        """Close the connection."""
        self._redis.close()


def failure(error: RedisError, action: str) -> EngineError:
    """An :class:`EngineError` naming the error's code, never its text.

    A Lua script's error, or a command's, can quote the data it was given.
    """
    first = str(error).split(" ", 1)[0]
    code = f" ({first})" if _CODE.fullmatch(first) else ""
    return EngineError(f"{action} failed: {type(error).__name__}{code}")


@contextmanager
def redacted(action: str) -> Generator[None]:
    """Turn a Redis error in the block into an :func:`failure` for ``action``."""
    try:
        yield
    except RedisError as error:
        raise failure(error, action) from None


def binary(reply: object) -> bytes:
    """A bulk string reply as bytes."""
    return reply if isinstance(reply, bytes) else str(reply).encode()


def text(reply: object) -> str:
    """A bulk string reply as text."""
    if isinstance(reply, bytes):
        return reply.decode("utf-8", "surrogateescape")
    return str(reply)


def raw(value: str) -> bytes:
    """Text back to the bytes it was decoded from."""
    return value.encode("utf-8", "surrogateescape")


def number(reply: object) -> int:
    """An integer reply."""
    if isinstance(reply, int):
        return reply
    return int(text(reply))


def items(reply: object) -> list[object]:
    """An array reply."""
    if isinstance(reply, list | tuple):
        return _ITEMS.validate_python(reply)
    message = f"expected an array reply, not {type(reply).__name__}"
    raise EngineError(message)


def texts(reply: object) -> list[str]:
    """An array of bulk strings as text."""
    return [text(item) for item in items(reply)]
