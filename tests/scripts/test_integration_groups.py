# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The integration groups cover every test path once, with services and users that exist."""

import re
import tomllib
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, JsonValue

from datadouble.programs import run_program

ROOT = Path(__file__).parents[2]
_USER = re.compile(r"\d+:\d+")


class Compose(BaseModel):
    """The part of compose.yaml this test reads."""

    services: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])


def groups() -> dict[str, tuple[str, list[str], list[str]]]:
    """Each group's user, services and test paths, from the table."""
    table = (ROOT / "tests" / "integration-groups.txt").read_text("utf-8")
    found: dict[str, tuple[str, list[str], list[str]]] = {}
    for line in table.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, user, services, paths = (part.strip() for part in line.split("|"))
        found[name] = (user, services.split(), paths.split())
    return found


def test_every_test_path_is_in_one_group() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    configured = pyproject["tool"]["pytest"]["ini_options"]["testpaths"]
    grouped = [path for _, _, paths in groups().values() for path in paths]
    assert sorted(grouped) == sorted(configured)
    assert len(grouped) == len(set(grouped))


def test_services_and_users_exist() -> None:
    compose = Compose.model_validate(yaml.safe_load((ROOT / "compose.yaml").read_text("utf-8")))
    for name, (user, services, _) in groups().items():
        assert _USER.fullmatch(user), name
        assert set(services) <= set(compose.services), name


def test_the_script_reads_the_table() -> None:
    script = str(ROOT / "scripts" / "integration.sh")
    listed = run_program(script, ["groups"])
    assert listed.stdout.split() == list(groups())
    needed = run_program(script, ["services", "core", "mysql"])
    assert needed.stdout.split() == ["postgres", "s3", "vault", "mysql", "jailer"]
    assert run_program(script, ["services", "nope"]).status == 2
    assert run_program(script, []).status == 2
