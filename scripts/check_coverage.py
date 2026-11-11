# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Check that a coverage report names every analysed source file exactly once.

SonarCloud finds a report's files by joining each ``<source>`` root with each
class's ``filename``. A filename that resolves under several roots (two
packages' ``__init__.py``) is merged or dropped, and the file counts as
uncovered, so the quality gate fails on code the tests do run. This check runs
before the scan:

    python3 scripts/check_coverage.py coverage.xml sonar-project.properties
"""

import sys
from collections.abc import Sequence
from pathlib import Path
from xml.etree import ElementTree as ET


def sonar_sources(properties: str) -> list[str]:
    """The ``sonar.sources`` directories from ``sonar-project.properties`` text."""
    for line in properties.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "sonar.sources":
            return [part.strip() for part in value.split(",") if part.strip()]
    return []


def check(report: str, sources: Sequence[str], root: Path) -> list[str]:
    """Problems with ``report`` (coverage XML text) for the given source directories."""
    tree = ET.fromstring(report)  # noqa: S314 - the report this repository's own tests wrote
    roots = [Path(text) for element in tree.iter("source") if (text := element.text)]
    problems: list[str] = []
    reported: set[Path] = set()
    for element in tree.iter("class"):
        name = element.get("filename", "")
        matches = [r / name for r in roots if (root / r / name).is_file()]
        if len(matches) == 1:
            reported.add(matches[0])
        else:
            places = ", ".join(str(m) for m in matches) or "no source root"
            problems.append(f"{name}: resolves to {places}")
    for source in sources:
        for path in sorted((root / source).rglob("*.py")):
            relative = path.relative_to(root)
            if relative not in reported:
                problems.append(f"{relative}: not in the coverage report")
    return problems


def main(argv: Sequence[str]) -> int:
    """Check ``argv[0]`` (coverage XML) against ``argv[1]`` (Sonar properties)."""
    report, properties = (Path(arg) for arg in argv)
    sources = sonar_sources(properties.read_text("utf-8"))
    problems = check(report.read_text("utf-8"), sources, Path.cwd())
    for problem in problems:
        sys.stdout.write(f"{problem}\n")
    sys.stdout.write(f"{len(problems)} problem(s) in the coverage report\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
