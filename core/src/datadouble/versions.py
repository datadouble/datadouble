# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Engine server versions: the newest of several.

Sources on one compatibility line (``images.version_line``, as long as the
engine's ``Capabilities.version_line`` says) read each other's dumps, so one
working server of the newest version on the line can hold every source on it.
"""

import re

_NUMBER = re.compile(r"\d+")


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(number) for number in _NUMBER.findall(version))


def newest(versions: tuple[str, ...]) -> str:
    """The newest of ``versions``, comparing their numbers in turn; the first of equals."""
    if not versions:
        message = "no versions to choose from"
        raise ValueError(message)
    return max(versions, key=_key)
