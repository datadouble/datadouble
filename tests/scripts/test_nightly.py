# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The nightly hosted run's committed workflows match what ci init writes."""

import io
import shutil
from pathlib import Path

import pytest

import nightly

ROOT = Path(__file__).resolve().parents[2]


def test_the_committed_workflows_are_current() -> None:
    assert nightly.differences() == []
    assert nightly.main(["check"]) == 0


def test_adapting_a_workflow() -> None:
    generated = (
        "on:\n  schedule:\n    - cron: 0 2 * * 1\n  workflow_dispatch:\n"
        "        uses: datadouble/datadouble/ci/github-actions@v1.2.3\n"
    )
    assert nightly.adapt(generated) == (
        "# Copyright (c) 2026- Tight Line LLC\n# SPDX-License-Identifier: MIT\n\n"
        "on:\n  workflow_dispatch:\n"
        "        uses: ./ci/github-actions # zizmor: ignore[self-repository]\n"
    )


def test_writing_and_checking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shutil.copytree(ROOT / "tests" / "nightly", tmp_path / "tests" / "nightly")
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "datadouble-nightly-gone.yml").write_text("old\n", encoding="utf-8")
    assert "datadouble-nightly-gone.yml" in nightly.differences(tmp_path)
    nightly.write(tmp_path)
    assert nightly.differences(tmp_path) == []
    assert not (workflows / "datadouble-nightly-gone.yml").exists()
    monkeypatch.setattr(nightly, "ROOT", tmp_path)
    monkeypatch.setattr(nightly, "differences", lambda: ["datadouble-nightly-build.yml"])
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert nightly.main(["check"]) == 1
    assert "datadouble-nightly-build.yml differs" in out.getvalue()
    monkeypatch.setattr(nightly, "write", lambda: None)
    assert nightly.main(["write"]) == 0
    assert nightly.main([]) == 2


def test_a_spec_ci_init_refuses(tmp_path: Path) -> None:
    shutil.copytree(ROOT / "tests" / "nightly", tmp_path / "tests" / "nightly")
    spec = tmp_path / "tests" / "nightly" / "nightly.yml"
    spec.write_text(spec.read_text("utf-8").replace("promote: {", "#"), encoding="utf-8")
    with pytest.raises(RuntimeError, match="ci init failed"):
        nightly.generate(tmp_path)
