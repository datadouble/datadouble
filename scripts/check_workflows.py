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
from pydantic import BaseModel, ConfigDict, Field, JsonValue

WORKFLOWS = Path(".github/workflows")


class Job(BaseModel):
    """The parts of a job this check reads."""

    model_config = ConfigDict(populate_by_name=True)

    uses: str | None = None
    timeout: JsonValue = Field(default=None, alias="timeout-minutes")


class Workflow(BaseModel):
    """The parts of a workflow this check reads."""

    jobs: dict[str, Job] = Field(default_factory=dict[str, Job])


def check_workflow(path: Path, text: str) -> list[str]:
    """Problems in one workflow file, as ``path: message``."""
    workflow = Workflow.model_validate(yaml.safe_load(text))
    return [
        f"{path}: job {name} has no timeout-minutes"
        for name, job in workflow.jobs.items()
        if job.uses is None
        and not (
            isinstance(job.timeout, int) and not isinstance(job.timeout, bool) and job.timeout > 0
        )
    ]


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
