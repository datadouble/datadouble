# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

import subprocess
from pathlib import Path

import pytest

import check_source

HEADER = "# Copyright (c) 2026- Tight Line LLC\n# SPDX-License-Identifier: MIT\n"


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("name", "needed"),
    [
        ("module.py", True),
        ("Dockerfile", True),
        ("Dockerfile.test", True),
        ("Makefile", True),
        ("ci.yml", True),
        ("theme.css", True),
        ("README.md", False),
        ("data.json", False),
        ("LICENSE", False),
    ],
)
def test_which_files_need_a_header(name: str, needed: bool) -> None:
    assert check_source.needs_header(Path(name)) is needed


def test_header_after_shebang_is_accepted(tmp_path: Path) -> None:
    path = write(tmp_path, "tool.sh", "#!/bin/sh\n" + HEADER + "echo hi\n")

    assert check_source.check_files(root=tmp_path, paths=[path]) == ([], 0)


def test_css_header_in_block_comment(tmp_path: Path) -> None:
    css = "/* Copyright (c) 2026- Tight Line LLC\n   SPDX-License-Identifier: MIT */\nbody {}\n"

    assert check_source.check_files(root=tmp_path, paths=[write(tmp_path, "theme.css", css)]) == (
        [],
        0,
    )


def test_missing_header_is_reported(tmp_path: Path) -> None:
    path = write(tmp_path, "module.py", "x = 1\n")

    problems, _ = check_source.check_files(root=tmp_path, paths=[path])

    assert [str(p) for p in problems] == [
        (
            f"{path}:1: missing header line(s): Copyright (c) 2026- Tight Line LLC; "
            "SPDX-License-Identifier: MIT"
        )
    ]


def test_suppressions_need_reasons(tmp_path: Path) -> None:
    code = HEADER + (
        "x = 1  # noqa: E501\n"
        "y = 2  # noqa: E501 - a long literal\n"
        "z = 3  # type: ignore[misc]\n"
        "from typing import Any\n"
        "a: Any = 1\n"
        "b: Any = 2  # why: untyped third-party value\n"
    )

    problems, count = check_source.check_files(
        root=tmp_path, paths=[write(tmp_path, "module.py", code)]
    )

    assert [p.line for p in problems] == [3, 5, 7]
    assert count == 5


def test_paths_outside_the_root_are_not_read(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = write(tmp_path, "secret.py", "x = 1\n")

    problems, _ = check_source.check_files(root=root, paths=[Path("..") / outside.name])

    assert [p.message for p in problems] == ["outside the repository; not checked"]


def test_strings_are_not_comments_or_code(tmp_path: Path) -> None:
    code = HEADER + 'text = "# noqa: E501 and Any and cast("\n'

    assert check_source.check_files(root=tmp_path, paths=[write(tmp_path, "module.py", code)]) == (
        [],
        0,
    )


def test_skips_other_and_missing_files(tmp_path: Path) -> None:
    assert check_source.check_files(
        root=tmp_path, paths=[write(tmp_path, "notes.md", "x"), tmp_path / "gone.py"]
    ) == (
        [],
        0,
    )


def test_main_reports_and_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write(tmp_path, "module.py", "x = 1\n")
    monkeypatch.chdir(tmp_path)

    assert check_source.main(["module.py"]) == 1
    assert "1 problem(s); 0 suppression(s)" in capsys.readouterr().out


def test_main_checks_tracked_files_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    write(tmp_path, "good.py", HEADER)
    write(tmp_path, "bad.py", "x = 1\n")
    write(tmp_path, ".gitignore", HEADER + "bad.py\n")
    monkeypatch.chdir(tmp_path)

    assert check_source.main([]) == 0
    assert "0 problem(s)" in capsys.readouterr().out
