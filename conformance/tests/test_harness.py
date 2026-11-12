# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

import subprocess
import sys
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
