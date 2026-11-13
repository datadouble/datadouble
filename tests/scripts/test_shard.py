# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

import json
from pathlib import Path

import pytest
from shard import Shard, ShardError, main, read_durations

pytest_plugins = ["pytester"]

TESTS = "\n".join(f"def test_{number}() -> None:\n    pass\n" for number in range(7))
HOOKS = "from shard import pytest_addoption, pytest_collection_modifyitems, pytest_configure"


def run(pytester: pytest.Pytester, *arguments: str) -> pytest.RunResult:
    """A run of seven tests that pass, with ``arguments``."""
    pytester.makeconftest(HOOKS)
    pytester.makepyfile(test_numbers=TESTS)
    return pytester.runpytest("-p", "no:cacheprovider", "-v", *arguments)


def ran(pytester: pytest.Pytester, *arguments: str) -> list[str]:
    """The tests a run with ``arguments`` passed, by name."""
    result = run(pytester, *arguments)
    return [line.split("::")[1].split()[0] for line in result.outlines if " PASSED" in line]


def test_without_durations_shards_take_every_nth_test() -> None:
    assert Shard.parse("2/3").positions([str(n) for n in range(7)], {}) == {1, 4}


def test_durations_balance_the_shards() -> None:
    ids = ["a", "b", "c", "d", "e"]
    durations = {"a": 10.0, "b": 1.0, "c": 9.0, "d": 2.0}
    shards = [Shard(index, 2).positions(ids, durations) for index in (1, 2)]
    # Longest first, each to the shard with less so far: a (10), d (2) and
    # b (1) against c (9) and e (5.5, the average of the others).
    assert shards == [{0, 1, 3}, {2, 4}]


def test_shards_together_run_every_test_once(pytester: pytest.Pytester) -> None:
    shards = [ran(pytester, "--shard", f"{index}/3") for index in (1, 2, 3)]
    assert shards == [
        ["test_0", "test_3", "test_6"],
        ["test_1", "test_4"],
        ["test_2", "test_5"],
    ]
    assert ran(pytester) == [f"test_{number}" for number in range(7)]


def test_balanced_shards_use_recorded_durations(pytester: pytest.Pytester) -> None:
    durations = pytester.path / "durations.json"
    seconds = {f"test_numbers.py::test_{number}": 1.0 for number in range(6)}
    durations.write_text(json.dumps(seconds | {"test_numbers.py::test_6": 60.0}), "utf-8")
    first = ran(pytester, "--shard", "1/2", "--shard-durations", str(durations))
    second = ran(pytester, "--shard", "2/2", "--shard-durations", str(durations))
    assert first == ["test_6"]
    assert sorted(first + second) == sorted(f"test_{number}" for number in range(7))


def test_deselected_tests_are_reported(pytester: pytest.Pytester) -> None:
    run(pytester, "--shard", "1/2").assert_outcomes(passed=4, deselected=3)


def test_durations_are_recorded_and_merged(pytester: pytest.Pytester, tmp_path: Path) -> None:
    recorded = tmp_path / "one.json"
    run(pytester, "--record-durations", str(recorded)).assert_outcomes(passed=7)
    seconds = read_durations(recorded)
    assert sorted(seconds) == [f"test_numbers.py::test_{number}" for number in range(7)]
    assert all(value >= 0 for value in seconds.values())
    other = tmp_path / "two.json"
    other.write_text(json.dumps({"test_numbers.py::test_0": 3.14159, "elsewhere": 2}), "utf-8")
    merged = tmp_path / "merged.json"
    assert main(["merge", str(merged), str(recorded), str(other)]) == 0
    combined = read_durations(merged)
    assert combined["test_numbers.py::test_0"] == pytest.approx(3.1)
    assert combined["elsewhere"] == pytest.approx(2.0)
    assert len(combined) == 8


def test_merge_needs_an_output(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["merge"]) == 2
    assert main([]) == 2
    assert "usage" in capsys.readouterr().err


@pytest.mark.parametrize("text", ["0/2", "3/2", "2", "a/b", "1/0", " 1/2"])
def test_bad_shards_are_refused(text: str) -> None:
    with pytest.raises(ShardError, match="K/N"):
        Shard.parse(text)


@pytest.mark.parametrize(
    "arguments",
    [
        ("--shard", "3/2"),
        ("--shard", "1/2", "--shard-durations", "missing.json"),
        ("--shard", "1/2", "--shard-durations", "test_numbers.py"),
    ],
)
def test_bad_options_stop_the_run(pytester: pytest.Pytester, arguments: tuple[str, ...]) -> None:
    assert run(pytester, *arguments).ret == pytest.ExitCode.USAGE_ERROR
