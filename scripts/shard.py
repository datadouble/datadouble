# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Run one shard of the collected tests: ``pytest --shard 2/4``.

The integration suite's slower groups run as several CI jobs at once
(tests/integration-groups.txt). Shard K of N takes every Nth test in
collection order, starting with the Kth, so the slow tests of one file spread
across the shards; together the shards run every test exactly once. The root
``conftest.py`` loads these hooks into every run.
"""

import re
from dataclasses import dataclass

import pytest

_SHARD = re.compile(r"([1-9]\d*)/([1-9]\d*)")


class ShardError(ValueError):
    """A ``--shard`` that is not K/N with 1 <= K <= N."""

    def __init__(self, text: str) -> None:
        """Name what was given."""
        super().__init__(f"--shard takes K/N with 1 <= K <= N, not {text!r}")


@dataclass(frozen=True)
class Shard:
    """The Kth (from 1) of N shards."""

    index: int
    count: int

    @classmethod
    def parse(cls, text: str) -> "Shard":
        """``K/N``, with 1 <= K <= N."""
        match = _SHARD.fullmatch(text)
        if match is None or int(match[1]) > int(match[2]):
            raise ShardError(text)
        return cls(int(match[1]), int(match[2]))

    def takes(self, position: int) -> bool:
        """Whether the test at ``position`` (from 0) in collection order is this shard's."""
        return position % self.count == self.index - 1


_KEY = pytest.StashKey[Shard | None]()


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add ``--shard``."""
    parser.addoption(
        "--shard",
        metavar="K/N",
        help="run only the Kth of N shards of the collected tests",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Check ``--shard`` before collecting anything."""
    value: object = config.getoption("shard")
    try:
        config.stash[_KEY] = Shard.parse(value) if isinstance(value, str) else None
    except ShardError as error:
        raise pytest.UsageError(str(error)) from error


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Keep this shard's tests and report the others as deselected."""
    shard = config.stash[_KEY]
    if shard is None:
        return
    kept = [item for position, item in enumerate(items) if shard.takes(position)]
    dropped = [item for position, item in enumerate(items) if not shard.takes(position)]
    items[:] = kept
    config.hook.pytest_deselected(items=dropped)
