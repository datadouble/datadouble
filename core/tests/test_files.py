# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

import fcntl
from pathlib import Path

import pytest

from datadouble.files import Reflink, copy_tree


def tree(root: Path) -> Path:
    source = root / "source"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "data").write_text("rows", "utf-8")
    (source / "nested" / "data").chmod(0o600)
    (source / "link").symlink_to("nested/data")
    return source


def test_trees_keep_files_modes_and_links(tmp_path: Path) -> None:
    source = tree(tmp_path)
    assert copy_tree(source, tmp_path / "copy") is None
    copied = tmp_path / "copy" / "nested" / "data"
    assert copied.read_text("utf-8") == "rows"
    assert copied.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "copy" / "link").readlink() == Path("nested/data")
    into = tmp_path / "into"
    into.mkdir()
    assert copy_tree(source, into, contents=True) is None
    assert (into / "nested" / "data").is_file()


def test_failures_are_returned(tmp_path: Path) -> None:
    problem = copy_tree(tmp_path / "missing", tmp_path / "copy")
    assert problem is not None
    assert "missing" in problem
    source = tree(tmp_path)
    assert copy_tree(source, tmp_path / "copy") is None
    assert copy_tree(source, tmp_path / "copy") is not None


def test_reflinks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(_fd: int, _request: int, _arg: int) -> int:
        raise OSError(95, "Operation not supported")

    monkeypatch.setattr(fcntl, "ioctl", refuse)
    source = tree(tmp_path)
    problem = copy_tree(source, tmp_path / "shared", Reflink.ALWAYS)
    assert problem is not None
    assert problem.startswith("Operation not supported")
    assert copy_tree(source, tmp_path / "copied") is None
    assert (tmp_path / "copied" / "nested" / "data").read_text("utf-8") == "rows"

    def clone(_fd: int, _request: int, _arg: int) -> int:
        return 0

    monkeypatch.setattr(fcntl, "ioctl", clone)
    assert copy_tree(source, tmp_path / "cloned", Reflink.ALWAYS) is None
    assert (tmp_path / "cloned" / "nested" / "data").exists()
