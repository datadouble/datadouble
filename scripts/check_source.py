# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Check the conventions every source file must follow.

1. Each source file starts with the copyright line and SPDX licence identifier,
   in its own comment syntax (after a shebang line, if any).
2. Each suppression of a checker states why. Directive comments (``noqa``,
   ``type: ignore``, ``pyright: ignore``, ``pragma: no cover``) need a reason
   after `` - ``, and every use of the typing escape hatches (the "any" type
   and the cast function) needs a ``# why:`` comment on the same line.
3. Checking the whole repository, the number of suppressions must equal the
   budget in ``pyproject.toml`` (``[tool.datadouble] suppressions``). Adding
   one means raising the budget in the same change, where review sees it and
   the pull request says why; removing one means lowering it, so the budget
   only ever ratchets down on its own.
"""

import io
import re
import sys
import tokenize
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from datadouble.programs import run_program

COPYRIGHT = "Copyright (c) 2026- Tight Line LLC"
SPDX = "SPDX-License-Identifier: MIT"

_HASH_COMMENT_SUFFIXES = frozenset({".py", ".sh", ".toml", ".yml", ".yaml", ".properties"})
_HASH_COMMENT_NAMES = ("Dockerfile", "Makefile", ".dockerignore", ".gitignore")
_BLOCK_COMMENT_SUFFIXES = frozenset({".css", ".js"})

_DIRECTIVE = re.compile(r"#\s*(noqa\b|type:\s*ignore|pyright:\s*ignore|pragma:\s*no cover)")
_ESCAPE_HATCHES = frozenset({"Any", "cast"})


@dataclass(frozen=True)
class Problem:
    """One convention a file breaks."""

    path: Path
    line: int
    message: str

    def __str__(self) -> str:
        """Format as ``path:line: message``, which editors and CI annotate."""
        return f"{self.path}:{self.line}: {self.message}"


def needs_header(path: Path) -> bool:
    """Whether a file is source code that must carry the header."""
    return (
        path.suffix in _HASH_COMMENT_SUFFIXES
        or path.suffix in _BLOCK_COMMENT_SUFFIXES
        or path.name.startswith(_HASH_COMMENT_NAMES)
    )


def check_header(path: Path, text: str) -> list[Problem]:
    """Check the copyright and SPDX lines at the top of a file."""
    lines = text.splitlines()
    if lines and lines[0].startswith("#!"):
        lines = lines[1:]
    top = "\n".join(lines[:3])
    missing = [marker for marker in (COPYRIGHT, SPDX) if marker not in top]
    if missing:
        return [Problem(path, 1, f"missing header line(s): {'; '.join(missing)}")]
    return []


def check_suppressions(path: Path, text: str) -> tuple[list[Problem], int]:
    """Check that every suppression in a Python file states why; count them all.

    Works on tokens, so text inside strings is never mistaken for a comment or
    for code.
    """
    comments: dict[int, str] = {}
    hatches: set[int] = set()
    imports: set[int] = set()
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        line = token.start[0]
        if token.type == tokenize.COMMENT:
            comments[line] = token.string
        elif token.type == tokenize.NAME and token.string in {"import", "from"}:
            imports.add(line)
        elif token.type == tokenize.NAME and token.string in _ESCAPE_HATCHES:
            hatches.add(line)
    problems: list[Problem] = []
    count = 0
    for line, comment in sorted(comments.items()):
        directive = _DIRECTIVE.search(comment)
        if directive:
            count += 1
            if " - " not in comment[directive.end() :]:
                problems.append(Problem(path, line, "suppression without a reason after ' - '"))
    for line in sorted(hatches - imports):
        count += 1
        if "# why:" not in comments.get(line, ""):
            problems.append(Problem(path, line, "typing escape hatch without a '# why:' comment"))
    return problems, count


def check_files(paths: Iterable[Path], root: Path | None = None) -> tuple[list[Problem], int]:
    """Check every file that needs it; return the problems and the suppression count.

    Only files inside ``root`` (the current directory by default) are read; any
    other path is reported as a problem rather than opened.
    """
    base = (root or Path.cwd()).resolve()
    problems: list[Problem] = []
    suppressions = 0
    for path in paths:
        resolved = (base / path).resolve()
        if not resolved.is_relative_to(base):
            problems.append(Problem(path, 1, "outside the repository; not checked"))
            continue
        if not needs_header(resolved) or not resolved.is_file():
            continue
        text = resolved.read_text(encoding="utf-8")
        problems.extend(check_header(path, text))
        if path.suffix == ".py":
            found, count = check_suppressions(path, text)
            problems.extend(found)
            suppressions += count
    return problems, suppressions


def tracked_files() -> list[Path]:
    """Files git tracks or would track (not ignored)."""
    listing = run_program("git", ["ls-files", "--cached", "--others", "--exclude-standard"])
    if listing.status != 0:
        message = f"git ls-files failed: {listing.last_error()}"
        raise RuntimeError(message)
    return [Path(name) for name in listing.stdout.splitlines()]


def budget(pyproject: Path) -> int | None:
    """The suppression budget in ``pyproject``, if it sets one."""
    if not pyproject.is_file():
        return None
    settings = tomllib.loads(pyproject.read_text("utf-8")).get("tool", {}).get("datadouble", {})
    allowed = settings.get("suppressions")
    return allowed if isinstance(allowed, int) else None


def over_budget(suppressions: int, pyproject: Path) -> Problem | None:
    """A problem if the count differs from the budget in ``pyproject``."""
    allowed = budget(pyproject)
    if allowed is None or suppressions == allowed:
        return None
    lines = pyproject.read_text("utf-8").splitlines()
    line = next((n for n, text in enumerate(lines, 1) if text.startswith("suppressions")), 1)
    if suppressions > allowed:
        message = (
            f"{suppressions} suppressions, over the budget of {allowed}: remove the new ones, "
            "or raise the budget here and say why in the pull request"
        )
    else:
        message = f"{suppressions} suppressions, under the budget of {allowed}: lower it to match"
    return Problem(pyproject, line, message)


def main(argv: Sequence[str] | None = None) -> int:
    """Check the given files, or every tracked file and the suppression budget, and report."""
    paths = [Path(arg) for arg in argv] if argv else tracked_files()
    problems, suppressions = check_files(paths)
    if not argv and (problem := over_budget(suppressions, Path("pyproject.toml"))):
        problems.append(problem)
    for problem in problems:
        sys.stdout.write(f"{problem}\n")
    sys.stdout.write(f"{len(problems)} problem(s); {suppressions} suppression(s) in Python code\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
