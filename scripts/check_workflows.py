# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Check that every GitHub Actions job has a time limit.

Without ``timeout-minutes`` a hung job runs for GitHub's default of six hours
and holds its pull request's checks pending all that time. Every job must set
one, sized to its normal run with room to spare; a job that calls a reusable
workflow (``uses:``) is limited by the called workflow's own jobs instead.
"""

import sys
from collections.abc import Sequence
from pathlib import Path

import yaml

WORKFLOWS = Path(".github/workflows")


def check_workflow(path: Path, text: str) -> list[str]:
    """Problems in one workflow file, as ``path: message``."""
    document = yaml.safe_load(text)
    jobs = document.get("jobs", {}) if isinstance(document, dict) else {}
    problems: list[str] = []
    for name, job in jobs.items() if isinstance(jobs, dict) else ():
        if not isinstance(job, dict) or "uses" in job:
            continue
        limit = job.get("timeout-minutes")
        if not isinstance(limit, int) or limit <= 0:
            problems.append(f"{path}: job {name} has no timeout-minutes")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    """Check the given workflow files, or every one in ``.github/workflows``."""
    paths = [Path(arg) for arg in argv] if argv else sorted(WORKFLOWS.glob("*.yml"))
    problems = [
        problem for path in paths for problem in check_workflow(path, path.read_text("utf-8"))
    ]
    for problem in problems:
        sys.stdout.write(f"{problem}\n")
    sys.stdout.write(f"{len(problems)} problem(s) in {len(paths)} workflow(s)\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
