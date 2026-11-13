# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Run one shard of the collected tests, and record how long tests take.

The integration suite's slower groups run as several CI jobs at once
(tests/integration-groups.txt); ``pytest --shard 2/4`` runs the second of
four shards, and together the shards run every test exactly once.

With ``--shard-durations FILE`` (a JSON object of test IDs to seconds) the
shards are balanced by time: the longest test first, each to the shard with
the least time so far, and a test the file does not list counts as the
average. Without it, shard K takes every Nth test in collection order,
starting with the Kth. Every shard computes the same split from the same
collection, so stale durations only make the shards less even.

``--record-durations FILE`` writes each test's seconds (setup, call and
teardown) as such a file, and ``python scripts/shard.py merge OUT IN...``
merges several. The root ``conftest.py`` loads these hooks into every run.
"""

import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

_SHARD = re.compile(r"([1-9]\d*)/([1-9]\d*)")
_DURATIONS = TypeAdapter(dict[str, float])


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

    def positions(self, ids: Sequence[str], durations: Mapping[str, float]) -> set[int]:
        """The positions (from 0) in collection order of this shard's tests."""
        if not durations:
            return {p for p in range(len(ids)) if p % self.count == self.index - 1}
        average = sum(durations.values()) / len(durations)
        loads = [0.0] * self.count
        mine: set[int] = set()
        for position in sorted(range(len(ids)), key=lambda p: (-durations.get(ids[p], average), p)):
            shard = min(range(self.count), key=lambda s: (loads[s], s))
            loads[shard] += durations.get(ids[position], average)
            if shard == self.index - 1:
                mine.add(position)
        return mine


def read_durations(path: Path) -> dict[str, float]:
    """A durations file's seconds by test ID."""
    return _DURATIONS.validate_json(path.read_bytes())


def write_durations(path: Path, durations: Mapping[str, float]) -> None:
    """Write seconds by test ID, rounded to a tenth, in test ID order."""
    rounded = {test: round(seconds, 1) for test, seconds in sorted(durations.items())}
    path.write_text(json.dumps(rounded, indent=1) + "\n", "utf-8")


@dataclass(eq=False)  # pytest keeps plugins in sets, so they hash by identity
class _Recorder:
    """Adds up each test's phases and writes them when the session ends."""

    path: Path
    seconds: dict[str, float] = field(default_factory=dict[str, float])

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self.seconds[report.nodeid] = self.seconds.get(report.nodeid, 0.0) + report.duration

    def pytest_sessionfinish(self) -> None:
        write_durations(self.path, self.seconds)


@dataclass(frozen=True)
class _Split:
    shard: Shard | None
    durations: dict[str, float]


_KEY = pytest.StashKey[_Split]()


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add ``--shard``, ``--shard-durations`` and ``--record-durations``."""
    parser.addoption(
        "--shard",
        metavar="K/N",
        help="run only the Kth of N shards of the collected tests",
    )
    parser.addoption(
        "--shard-durations",
        metavar="FILE",
        help="balance --shard by the seconds each test took, from a JSON file",
    )
    parser.addoption(
        "--record-durations",
        metavar="FILE",
        help="write the seconds each test took to a JSON file",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Check the options before collecting anything."""
    shard: object = config.getoption("shard")
    balance: object = config.getoption("shard_durations")
    record: object = config.getoption("record_durations")
    try:
        config.stash[_KEY] = _Split(
            Shard.parse(shard) if isinstance(shard, str) else None,
            read_durations(Path(balance)) if isinstance(balance, str) else {},
        )
    except ShardError as error:
        raise pytest.UsageError(str(error)) from error
    except (OSError, ValidationError) as error:
        message = f"--shard-durations needs a JSON object of test IDs to seconds: {error}"
        raise pytest.UsageError(message) from error
    if isinstance(record, str):
        config.pluginmanager.register(_Recorder(Path(record)), "shard-recorder")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Keep this shard's tests and report the others as deselected."""
    split = config.stash[_KEY]
    if split.shard is None:
        return
    mine = split.shard.positions([item.nodeid for item in items], split.durations)
    kept = [item for position, item in enumerate(items) if position in mine]
    dropped = [item for position, item in enumerate(items) if position not in mine]
    items[:] = kept
    config.hook.pytest_deselected(items=dropped)


def main(argv: Sequence[str]) -> int:
    """``merge OUT IN...``: one durations file from several; later files win."""
    match list(argv):
        case ["merge", out, *sources]:
            merged: dict[str, float] = {}
            for source in sources:
                merged |= read_durations(Path(source))
            write_durations(Path(out), merged)
            return 0
        case _:
            sys.stderr.write("usage: shard.py merge OUT IN...\n")
            return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
