# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from datadouble_conformance.engine import wait_for_stage


def test_a_run_that_stops_early_fails(tmp_path: Path) -> None:
    process = subprocess.Popen([sys.executable, "-c", "pass"], text=True)
    process.wait()
    with pytest.raises(AssertionError, match="never reached transform"):
        wait_for_stage(tmp_path, "transform", process)


def test_a_reached_stage_is_found(tmp_path: Path) -> None:
    (tmp_path / "g1" / "transform").mkdir(parents=True)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], text=True)
    try:
        assert wait_for_stage(tmp_path, "transform", process) == tmp_path / "g1"
    finally:
        process.kill()
        process.wait()


class _FakeProcess:
    """A process that exits at its second poll, first doing ``on_exit``."""

    def __init__(self, on_exit: Callable[[], None], output: str) -> None:
        self.returncode: int | None = None
        self._on_exit = on_exit
        self._output = output
        self._polls = 0

    def poll(self) -> int | None:
        self._polls += 1
        if self._polls >= 2 and self.returncode is None:
            self._on_exit()
            self.returncode = 3
        return self.returncode

    def communicate(self, *, timeout: float | None = None) -> tuple[str | None, str | None]:
        del timeout
        return self._output, None


def test_a_stage_reached_just_before_the_run_exits_is_found(tmp_path: Path) -> None:
    stage = tmp_path / "g1" / "transform"
    process = _FakeProcess(lambda: stage.mkdir(parents=True), "")
    assert wait_for_stage(tmp_path, "transform", process) == tmp_path / "g1"


def test_a_run_that_exits_early_reports_its_status_and_output(tmp_path: Path) -> None:
    output = "".join(f"line {n}\n" for n in range(30))
    process = _FakeProcess(lambda: None, output)
    with pytest.raises(AssertionError) as caught:
        wait_for_stage(tmp_path, "transform", process)
    message = str(caught.value)
    assert "never reached transform" in message
    assert "exited with status 3" in message
    assert message.endswith("line 10\n" + "\n".join(f"line {n}" for n in range(11, 30)))
    assert "line 9\n" not in message


class _HeldOpen(_FakeProcess):
    """A process whose output another program it started still holds open."""

    def communicate(self, *, timeout: float | None = None) -> tuple[str | None, str | None]:
        command = "run"
        raise subprocess.TimeoutExpired(command, timeout or 0)


def test_output_held_open_by_another_program_is_not_waited_for(tmp_path: Path) -> None:
    process = _HeldOpen(lambda: None, "")
    with pytest.raises(AssertionError, match="could not be read"):
        wait_for_stage(tmp_path, "transform", process)
