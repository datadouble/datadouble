# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

from pathlib import Path

import check_workflows
import pytest

WORKFLOW = """
jobs:
  limited:
    runs-on: ubuntu-latest
    timeout-minutes: 10
  unlimited:
    runs-on: ubuntu-latest
  reusable:
    uses: ./.github/workflows/other.yml
  expression:
    timeout-minutes: ${{ inputs.minutes }}
"""


def test_jobs_without_a_limit() -> None:
    assert check_workflows.check_workflow(Path("w.yml"), WORKFLOW) == [
        "w.yml: job unlimited has no timeout-minutes",
        "w.yml: job expression has no timeout-minutes",
    ]
    assert check_workflows.check_workflow(Path("w.yml"), "on: push\n") == []


def test_main(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = tmp_path / "good.yml"
    good.write_text("jobs:\n  a:\n    timeout-minutes: 5\n", "utf-8")
    bad = tmp_path / "bad.yml"
    bad.write_text(WORKFLOW, "utf-8")
    assert check_workflows.main([str(good)]) == 0
    assert check_workflows.main([str(good), str(bad)]) == 1
    assert "2 problem(s) in 2 workflow(s)" in capsys.readouterr().out


def test_the_repository_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(Path(__file__).parents[2])
    assert check_workflows.main([]) == 0
