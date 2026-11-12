# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Cut a release in two steps.

``main`` only accepts pull requests, so a release is a pull request followed by
a tag:

    uv run scripts/make_tag.py prepare 0.1.0   # from an up-to-date main
    uv run scripts/make_tag.py tag 0.1.0       # after the release PR merges

``prepare`` refuses to run while ``[Unreleased]`` in ``CHANGELOG.md`` has no
entries, runs ``make check``, sets every package to the new version (they share
one version until 1.0), moves the unreleased entries into a dated section, and
opens the release pull request. ``tag`` checks the merged result and pushes the
``v<version>`` tag, which starts the release workflow.
"""

import re
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol

from pydantic import TypeAdapter, ValidationError

from datadouble.programs import run_program

_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_UNRELEASED = "## [Unreleased]"
_PROJECT_VERSION = re.compile(r'^(version\s*=\s*)"[^"]*"', re.MULTILINE)
_MEMBERS = TypeAdapter(list[str])


class ReleaseError(Exception):
    """A release precondition that does not hold."""


class Runner(Protocol):
    """Runs a command and returns its standard output."""

    def __call__(self, *command: str) -> str:
        """Run ``command``; raise on a non-zero exit."""
        ...


def run(*command: str) -> str:
    """Run a command in the current directory and return its standard output."""
    done = run_program(command[0], command[1:])
    if done.status != 0:
        raise subprocess.CalledProcessError(done.status, command, done.stdout, done.stderr)
    return done.stdout


def validate_version(version: str) -> str:
    """Return ``version`` if it is ``MAJOR.MINOR.PATCH``."""
    if not _SEMVER.match(version):
        message = f"version must look like 1.2.3, not {version!r}"
        raise ReleaseError(message)
    return version


def _unreleased_block(changelog: str) -> tuple[int, int]:
    start = changelog.find(_UNRELEASED)
    if start < 0:
        message = f"CHANGELOG.md has no {_UNRELEASED} section"
        raise ReleaseError(message)
    body = start + len(_UNRELEASED)
    following = changelog.find("\n## [", body)
    return body, len(changelog) if following < 0 else following + 1


def unreleased_entries(changelog: str) -> list[str]:
    """The entry lines under ``[Unreleased]``, ignoring blank lines and headings."""
    body, end = _unreleased_block(changelog)
    return [
        line
        for line in changelog[body:end].splitlines()
        if line.strip() and not line.startswith("#")
    ]


def release_changelog(changelog: str, version: str, day: date) -> str:
    """Move the unreleased entries into a dated ``[version]`` section."""
    if not unreleased_entries(changelog):
        message = f"{_UNRELEASED} in CHANGELOG.md has no entries; nothing to release"
        raise ReleaseError(message)
    body, end = _unreleased_block(changelog)
    entries = changelog[body:end].strip("\n")
    section = f"\n\n## [{version}] - {day.isoformat()}\n\n{entries}\n"
    rest = changelog[end:]
    return changelog[:body] + section + ("\n" + rest if rest else "")


def has_release_section(changelog: str, version: str) -> bool:
    """Whether the changelog has a dated section for ``version``."""
    return (
        re.search(
            rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}$", changelog, re.MULTILINE
        )
        is not None
    )


def project_version(pyproject: str) -> str:
    """The ``[project] version`` of a ``pyproject.toml``."""
    version: object = tomllib.loads(pyproject).get("project", {}).get("version")
    if not isinstance(version, str):
        message = "pyproject.toml has no [project] version"
        raise ReleaseError(message)
    return version


def set_project_version(pyproject: str, version: str) -> str:
    """Set the ``[project] version`` of a ``pyproject.toml``."""
    project_version(pyproject)
    return _PROJECT_VERSION.sub(rf'\g<1>"{version}"', pyproject, count=1)


def package_files(root: Path) -> list[Path]:
    """The ``pyproject.toml`` of every workspace member."""
    workspace = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    raw: object = workspace.get("tool", {}).get("uv", {}).get("workspace", {}).get("members")
    try:
        members = _MEMBERS.validate_python(raw)
    except ValidationError:
        members = []
    if not members:
        message = "the root pyproject.toml lists no workspace members"
        raise ReleaseError(message)
    return [root / member / "pyproject.toml" for member in members]


def _require_clean_main(runner: Runner) -> None:
    branch = runner("git", "rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch != "main":
        message = f"run this on main, not {branch!r}"
        raise ReleaseError(message)
    if runner("git", "status", "--porcelain").strip():
        message = "the working tree has uncommitted changes"
        raise ReleaseError(message)
    runner("git", "pull", "--ff-only")


def _require_new_tag(runner: Runner, tag: str) -> None:
    if runner("git", "tag", "--list", tag).strip():
        message = f"tag {tag} already exists"
        raise ReleaseError(message)


def prepare(version: str, root: Path, runner: Runner, day: date) -> str:
    """Open the release pull request for ``version``; return its URL."""
    tag = f"v{version}"
    _require_clean_main(runner)
    _require_new_tag(runner, tag)
    changelog_path = root / "CHANGELOG.md"
    changelog = release_changelog(changelog_path.read_text(encoding="utf-8"), version, day)
    runner("make", "check")
    branch = f"release/{tag}"
    runner("git", "switch", "--create", branch)
    changelog_path.write_text(changelog, encoding="utf-8")
    for path in package_files(root):
        path.write_text(
            set_project_version(path.read_text(encoding="utf-8"), version), encoding="utf-8"
        )
        runner("git", "add", str(path))
    runner("git", "add", str(changelog_path))
    runner(
        "git",
        "commit",
        "--message",
        f"Release {tag}\n\nSet every package to {version} and move the unreleased\n"
        f"changelog entries into the {version} section.",
    )
    runner("git", "push", "--set-upstream", "origin", branch)
    return runner(
        "gh",
        "pr",
        "create",
        "--title",
        f"Release {tag}",
        "--body",
        f"Sets every package to {version} and dates the changelog section. "
        f"After this merges, run `uv run scripts/make_tag.py tag {version}`.",
    ).strip()


def tag(version: str, root: Path, runner: Runner) -> str:
    """Tag the merged release on main and push the tag; return the tag."""
    name = f"v{version}"
    _require_clean_main(runner)
    _require_new_tag(runner, name)
    for path in package_files(root):
        found = project_version(path.read_text(encoding="utf-8"))
        if found != version:
            message = f"{path} is at {found}, not {version}; has the release PR merged?"
            raise ReleaseError(message)
    if not has_release_section((root / "CHANGELOG.md").read_text(encoding="utf-8"), version):
        message = f"CHANGELOG.md has no dated [{version}] section"
        raise ReleaseError(message)
    runner("git", "tag", "--annotate", name, "--message", f"Release {version}")
    runner("git", "push", "origin", name)
    return name


def main(
    argv: Sequence[str],
    *,
    root: Path | None = None,
    runner: Runner = run,
    day: date | None = None,
) -> int:
    """Run ``prepare`` or ``tag``; report the outcome; return the exit code."""
    match list(argv):
        case [("prepare" | "tag") as step, raw_version]:
            pass
        case _:
            sys.stdout.write(f"{__doc__}\n")
            return 2
    root = root or Path.cwd()
    try:
        version = validate_version(raw_version)
        if step == "prepare":
            url = prepare(version, root, runner, day or datetime.now(tz=UTC).date())
            sys.stdout.write(
                f"Opened {url}. When it merges: uv run scripts/make_tag.py tag {version}\n"
            )
        else:
            pushed = tag(version, root, runner)
            sys.stdout.write(f"Pushed {pushed}; the release workflow takes it from here.\n")
    except (ReleaseError, subprocess.CalledProcessError) as error:
        sys.stderr.write(f"error: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
