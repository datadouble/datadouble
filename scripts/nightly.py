# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Write, or check, the workflows the nightly hosted run drives.

The nightly run (.github/workflows/nightly.yml) runs the jobs `datadouble ci
init --system github-actions` writes for tests/nightly/nightly.yml, on a
self-hosted runner it starts for the night. GitHub runs only workflows in the
repository, so they are committed, adapted in two ways: they use this
checkout's action rather than a release's, and they have no schedules, since
the nightly run starts each one itself while its runner is up.

    uv run python scripts/nightly.py write   # after changing the spec or ci init
    uv run python scripts/nightly.py check   # fails if the committed ones differ
"""

import json
import re
import shutil
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from datadouble import api
from datadouble.outcome import Outcome
from datadouble.spec import load_spec

ROOT = Path(__file__).resolve().parents[1]
SPEC = Path("tests/nightly/nightly.yml")
WORKFLOWS = Path(".github/workflows")
IMAGE = "datadouble:nightly"
RUNNER = "datadouble-nightly"
_USES = re.compile(r"uses: datadouble/datadouble/ci/github-actions@\S+")
_SCHEDULE = re.compile(r"  schedule:\n(    - cron: [^\n]+\n)+")


# zizmor suggests GitHub's `$/` self-repository syntax for a local action, a
# style note that actionlint does not accept yet, so the path form stays.
_LOCAL = "uses: ./ci/github-actions # zizmor: ignore[self-repository]"


def adapt(workflow: str) -> str:
    """A generated workflow, using this checkout's action and with no schedule."""
    return _SCHEDULE.sub("", _USES.sub(_LOCAL, workflow))


def _work(spec: Path) -> str:
    """The build's work directory: beside the spec's store."""
    store = load_spec(spec).store[0]["filesystem"]["path"]
    return str(PurePosixPath(str(store)).parent / "work")


def generate(root: Path = ROOT) -> dict[str, str]:
    """The nightly workflows, by file name."""
    sides: tuple[dict[str, str | list[str]], ...] = (
        {"jobs": ["build", "promote", "prune"], "work": _work(root / SPEC)},
        {"jobs": ["fetch", "provision", "prune"]},
    )
    with tempfile.TemporaryDirectory(prefix="dd-nightly-") as scratch:
        repository = Path(scratch)
        shutil.copytree(root / SPEC.parent, repository / SPEC.parent)
        for side in sides:
            request = {
                "command": "ci.init",
                "system": "github-actions",
                "spec": str(repository / SPEC),
                "output": str(repository),
                "runner": RUNNER,
                "image": IMAGE,
                **side,
            }
            response = api.handle_json(json.dumps(request))
            if response.outcome is not Outcome.SUCCESS:
                message = f"ci init failed: {response.model_dump_json()}"
                raise RuntimeError(message)
        generated = sorted((repository / WORKFLOWS).glob("datadouble-nightly-*.yml"))
        return {path.name: adapt(path.read_text(encoding="utf-8")) for path in generated}


def differences(root: Path = ROOT) -> list[str]:
    """Workflows that are missing, stale or left over in ``root``."""
    expected = generate(root)
    committed = {
        path.name: path.read_text(encoding="utf-8")
        for path in (root / WORKFLOWS).glob("datadouble-nightly-*.yml")
    }
    names = sorted(set(expected) | set(committed))
    return [name for name in names if expected.get(name) != committed.get(name)]


def write(root: Path = ROOT) -> None:
    """Replace the committed nightly workflows with freshly generated ones."""
    for path in (root / WORKFLOWS).glob("datadouble-nightly-*.yml"):
        path.unlink()
    for name, text in generate(root).items():
        (root / WORKFLOWS / name).write_text(text, encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    """``write`` or ``check``."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["write"]:
        write()
        return 0
    if arguments == ["check"]:
        stale = differences()
        for name in stale:
            sys.stdout.write(f"{WORKFLOWS / name} differs from what scripts/nightly.py writes\n")
        return 1 if stale else 0
    sys.stderr.write(__doc__ or "")
    return 2


if __name__ == "__main__":
    sys.exit(main())
