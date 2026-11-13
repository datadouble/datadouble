# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

import pytest
from shard import Shard, ShardError

pytest_plugins = ["pytester"]

TESTS = "\n".join(f"def test_{number}() -> None:\n    pass\n" for number in range(7))


def ran(pytester: pytest.Pytester, *arguments: str) -> list[str]:
    """The tests a run with ``arguments`` passed, by name."""
    pytester.makeconftest(
        "from shard import pytest_addoption, pytest_collection_modifyitems, pytest_configure"
    )
    pytester.makepyfile(test_numbers=TESTS)
    result = pytester.runpytest("-p", "no:cacheprovider", "-v", *arguments)
    return [line.split("::")[1].split()[0] for line in result.outlines if " PASSED" in line]


def test_shards_take_every_nth_test() -> None:
    shard = Shard.parse("2/3")
    assert [position for position in range(7) if shard.takes(position)] == [1, 4]


def test_shards_together_run_every_test_once(pytester: pytest.Pytester) -> None:
    shards = [ran(pytester, "--shard", f"{index}/3") for index in (1, 2, 3)]
    assert shards == [
        ["test_0", "test_3", "test_6"],
        ["test_1", "test_4"],
        ["test_2", "test_5"],
    ]
    assert ran(pytester) == [f"test_{number}" for number in range(7)]


def test_deselected_tests_are_reported(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(
        "from shard import pytest_addoption, pytest_collection_modifyitems, pytest_configure"
    )
    pytester.makepyfile(test_numbers=TESTS)
    pytester.runpytest("-p", "no:cacheprovider", "--shard", "1/2").assert_outcomes(
        passed=4, deselected=3
    )


@pytest.mark.parametrize("text", ["0/2", "3/2", "2", "a/b", "1/0", " 1/2"])
def test_bad_shards_are_refused(text: str) -> None:
    with pytest.raises(ShardError, match="K/N"):
        Shard.parse(text)


def test_a_bad_shard_stops_the_run(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(
        "from shard import pytest_addoption, pytest_collection_modifyitems, pytest_configure"
    )
    pytester.makepyfile(test_numbers=TESTS)
    result = pytester.runpytest("-p", "no:cacheprovider", "--shard", "3/2")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
