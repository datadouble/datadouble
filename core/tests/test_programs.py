# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

import sys
from pathlib import Path

import pytest

from datadouble.programs import Completed, locate, run_program, start_program


def test_locate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DD_TOOL", "/opt/tool")
    assert locate("sh", "DD_TOOL") == "/opt/tool"
    monkeypatch.delenv("DD_TOOL")
    assert locate("sh", "DD_TOOL") == locate("sh")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert locate("sh") is None


def test_run_program() -> None:
    done = run_program(
        sys.executable,
        ["-c", "import os, sys; print(sys.stdin.read() + os.environ['DD_X']); sys.exit(2)"],
        stdin="in-",
        env={"DD_X": "env"},
    )
    assert (done.status, done.stdout) == (2, "in-env\n")
    raw = run_program(
        sys.executable,
        ["-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
        stdin=b"\xff",
    )
    assert raw.stdout == "�"


def test_last_error() -> None:
    assert Completed(1, "", "first\n\nlast\n\n").last_error() == "last"
    assert Completed(5, "", " \n").last_error() == "status 5"


def test_start_program() -> None:
    process = start_program(sys.executable, ["-c", "print('ready')"])
    assert process.stdout is not None
    assert process.stdout.readline() == "ready\n"
    assert process.wait() == 0
