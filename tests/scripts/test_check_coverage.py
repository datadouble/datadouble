# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

from pathlib import Path

import pytest

import check_coverage

PROPERTIES = "sonar.organization=x\nsonar.sources=a/src, b/src\nsonar.tests=t\n"


def report(sources: list[str], files: list[str]) -> str:
    roots = "".join(f"<source>{s}</source>" for s in sources)
    classes = "".join(f'<class filename="{f}"/>' for f in files)
    packages = f"<packages><package><classes>{classes}</classes></package></packages>"
    return f"<coverage><sources>{roots}</sources>{packages}</coverage>"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    for package in ("a/src/pa", "b/src/pb"):
        (tmp_path / package).mkdir(parents=True)
        (tmp_path / package / "__init__.py").write_text("", "utf-8")
    (tmp_path / "a/src/pa/working.py").write_text("", "utf-8")
    return tmp_path


def test_sources() -> None:
    assert check_coverage.sonar_sources(PROPERTIES) == ["a/src", "b/src"]
    assert check_coverage.sonar_sources("sonar.tests=t\n") == []


def test_unique_names_pass(tree: Path) -> None:
    good = report(["a/src", "b/src"], ["pa/__init__.py", "pa/working.py", "pb/__init__.py"])
    assert check_coverage.check(good, ["a/src", "b/src"], tree) == []


def test_ambiguous_missing_and_unknown_names_fail(tree: Path) -> None:
    bad = report(["a/src/pa", "b/src/pb", ""], ["__init__.py", "working.py", "gone.py"])
    problems = check_coverage.check(bad, ["a/src", "b/src"], tree)
    assert problems == [
        "__init__.py: resolves to a/src/pa/__init__.py, b/src/pb/__init__.py",
        "gone.py: resolves to no source root",
        "a/src/pa/__init__.py: not in the coverage report",
        "b/src/pb/__init__.py: not in the coverage report",
    ]


def test_main(
    tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tree)
    (tree / "sonar.properties").write_text(PROPERTIES, "utf-8")
    (tree / "good.xml").write_text(
        report(["a/src", "b/src"], ["pa/__init__.py", "pa/working.py", "pb/__init__.py"]), "utf-8"
    )
    (tree / "bad.xml").write_text(report(["a/src"], ["pa/__init__.py"]), "utf-8")
    assert check_coverage.main(["good.xml", "sonar.properties"]) == 0
    assert check_coverage.main(["bad.xml", "sonar.properties"]) == 1
    output = capsys.readouterr().out
    assert "0 problem(s)" in output
    assert "a/src/pa/working.py: not in the coverage report" in output
