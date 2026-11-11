# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Channels: named, movable pointers to an image's generations.

A channel lives in a store next to the images it points at, as
``<spec>/<group>/<engine>-<version>/channels/<name>.json``. ``promote``
advances one in the export store, ``fetch`` one in staging, and ``activate``
moves one back. Each channel keeps the generations it pointed at before, newest
first, so a rollback knows where it came from.
"""

import datetime as dt
from collections.abc import Callable

from pydantic import AwareDatetime, BaseModel, ConfigDict

from datadouble.names import Generation, check_name
from datadouble.storage import Store

_HISTORY = 20


class Channel(BaseModel):
    """Where a channel points, and where it pointed before."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    name: str
    generation: Generation
    updated_at: AwareDatetime
    previous: tuple[Generation, ...] = ()


def channel_key(prefix: str, name: str) -> str:
    """The store key of a channel under an image line's prefix."""
    return f"{prefix}/channels/{check_name('channel', name)}.json"


def read_channel(store: Store, prefix: str, name: str) -> Channel | None:
    """A channel, or None if it has never been set."""
    data = store.get_bytes(channel_key(prefix, name))
    return None if data is None else Channel.model_validate_json(data)


def point_channel(
    store: Store,
    prefix: str,
    name: str,
    generation: Generation,
    clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(tz=dt.UTC),
) -> Channel:
    """Point a channel at ``generation``, remembering where it pointed before."""
    current = read_channel(store, prefix, name)
    previous: tuple[Generation, ...] = ()
    if current is not None and current.generation != generation:
        previous = (current.generation, *current.previous)[:_HISTORY]
    elif current is not None:
        previous = current.previous
    channel = Channel(name=name, generation=generation, updated_at=clock(), previous=previous)
    store.put_bytes(channel_key(prefix, name), channel.model_dump_json(indent=2).encode())
    return channel
