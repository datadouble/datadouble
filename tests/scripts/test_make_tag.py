# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

import subprocess
from datetime import date
from pathlib import Path

import pytest

import make_tag

DAY = date(2026, 12, 1)

CHANGELOG = """# Changelog

Intro.

## [Unreleased]

### Added

- **A feature** that does a thing.

## [0.1.0] - 2026-11-30

### Added

- Earlier work.
"""

EMPTY_CHANGELOG = "# Changelog\n\n## [Unreleased]\n\n### Added\n"


class FakeRunner:
    def __init__(self, replies: dict[tuple[str, ...], str] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.replies = {
            ("git", "rev-parse", "--abbrev-ref", "HEAD"): "main\n",
            ("gh", "pr", "create"): "https://example.invalid/pull/9\n",
        } | (replies or {})

    def __call__(self, *command: str) -> str:
        self.calls.append(command)
        for prefix, reply in self.replies.items():
            if command[: len(prefix)] == prefix:
                return reply
        return ""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "ws"\nversion = "0.0.0"\n\n[tool.uv.workspace]\nmembers = ["core"]\n',
        encoding="utf-8",
    )
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "pyproject.toml").write_text(
        '[project]\nname = "datadouble"\nversion = "0.1.0"\n\n[tool.x]\nversion = "keep"\n',
        encoding="utf-8",
    )
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("version", ["1.2", "v1.2.3", "1.2.3-rc1", ""])
def test_rejects_non_semver(version: str) -> None:
    with pytest.raises(make_tag.ReleaseError):
        make_tag.validate_version(version)


def test_unreleased_entries() -> None:
    assert make_tag.unreleased_entries(CHANGELOG) == ["- **A feature** that does a thing."]
    assert make_tag.unreleased_entries(EMPTY_CHANGELOG) == []


def test_changelog_without_unreleased_section() -> None:
    with pytest.raises(make_tag.ReleaseError, match="no ## \\[Unreleased\\]"):
        make_tag.unreleased_entries("# Changelog\n")


def test_release_changelog_moves_entries() -> None:
    released = make_tag.release_changelog(CHANGELOG, "0.2.0", DAY)

    assert make_tag.unreleased_entries(released) == []
    assert make_tag.has_release_section(released, "0.2.0")
    assert released.index("## [Unreleased]") < released.index("## [0.2.0] - 2026-12-01")
    assert released.index("A feature") < released.index("## [0.1.0]")


def test_release_changelog_as_last_section() -> None:
    changelog = "# Changelog\n\n## [Unreleased]\n\n- One.\n"

    released = make_tag.release_changelog(changelog, "0.1.0", DAY)

    assert released == "# Changelog\n\n## [Unreleased]\n\n## [0.1.0] - 2026-12-01\n\n- One.\n"


def test_empty_unreleased_blocks_a_release() -> None:
    with pytest.raises(make_tag.ReleaseError, match="nothing to release"):
        make_tag.release_changelog(EMPTY_CHANGELOG, "0.1.0", DAY)


def test_set_project_version_only_touches_project_table() -> None:
    text = '[project]\nversion = "0.1.0"\n\n[tool.x]\nversion = "keep"\n'

    updated = make_tag.set_project_version(text, "0.2.0")

    assert make_tag.project_version(updated) == "0.2.0"
    assert 'version = "keep"' in updated


def test_project_without_version() -> None:
    with pytest.raises(make_tag.ReleaseError, match="no \\[project\\] version"):
        make_tag.project_version('[project]\nname = "x"\n')


def test_workspace_without_members(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0"\n', encoding="utf-8")

    with pytest.raises(make_tag.ReleaseError, match="no workspace members"):
        make_tag.package_files(tmp_path)


def test_prepare_opens_release_pull_request(repo: Path) -> None:
    runner = FakeRunner()

    code = make_tag.main(["prepare", "0.2.0"], root=repo, runner=runner, day=DAY)

    assert code == 0
    assert ("make", "check") in runner.calls
    assert ("git", "switch", "--create", "release/v0.2.0") in runner.calls
    assert runner.calls[-1][:3] == ("gh", "pr", "create")
    pyproject = (repo / "core" / "pyproject.toml").read_text(encoding="utf-8")
    assert make_tag.project_version(pyproject) == "0.2.0"
    assert make_tag.has_release_section(
        (repo / "CHANGELOG.md").read_text(encoding="utf-8"), "0.2.0"
    )


def test_prepare_refuses_empty_unreleased(repo: Path) -> None:
    (repo / "CHANGELOG.md").write_text(EMPTY_CHANGELOG, encoding="utf-8")
    runner = FakeRunner()

    assert make_tag.main(["prepare", "0.2.0"], root=repo, runner=runner, day=DAY) == 1
    assert ("make", "check") not in runner.calls


@pytest.mark.parametrize(
    ("replies", "message"),
    [
        ({("git", "rev-parse", "--abbrev-ref", "HEAD"): "feature\n"}, "run this on main"),
        ({("git", "status", "--porcelain"): " M file\n"}, "uncommitted changes"),
        ({("git", "tag", "--list"): "v0.2.0\n"}, "already exists"),
    ],
)
def test_preconditions(
    repo: Path,
    replies: dict[tuple[str, ...], str],
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert make_tag.main(["prepare", "0.2.0"], root=repo, runner=FakeRunner(replies)) == 1
    assert message in capsys.readouterr().err


def test_tag_pushes_after_merge(repo: Path) -> None:
    make_tag.main(["prepare", "0.2.0"], root=repo, runner=FakeRunner(), day=DAY)
    runner = FakeRunner()

    assert make_tag.main(["tag", "0.2.0"], root=repo, runner=runner) == 0
    assert runner.calls[-1] == ("git", "push", "origin", "v0.2.0")


def test_tag_refuses_unmerged_release(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert make_tag.main(["tag", "0.2.0"], root=repo, runner=FakeRunner()) == 1
    assert "has the release PR merged?" in capsys.readouterr().err


def test_tag_refuses_missing_changelog_section(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    core = repo / "core" / "pyproject.toml"
    core.write_text(
        make_tag.set_project_version(core.read_text(encoding="utf-8"), "0.2.0"), encoding="utf-8"
    )

    assert make_tag.main(["tag", "0.2.0"], root=repo, runner=FakeRunner()) == 1
    assert "no dated [0.2.0] section" in capsys.readouterr().err


def test_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert make_tag.main(["publish"]) == 2
    assert "Cut a release" in capsys.readouterr().out


def test_failed_command_is_reported(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def failing(*command: str) -> str:
        raise subprocess.CalledProcessError(1, command)

    assert make_tag.main(["tag", "0.2.0"], root=repo, runner=failing) == 1
    assert "error:" in capsys.readouterr().err


def test_default_runner_and_root(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    monkeypatch.chdir(repo)
    assert make_tag.run("echo", "hi") == "hi\n"
    with pytest.raises(subprocess.CalledProcessError):
        make_tag.run("false")
    assert make_tag.main(["tag", "9.9.9"], runner=FakeRunner()) == 1
