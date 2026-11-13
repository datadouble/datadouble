# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The integration groups cover every test path once, with services and users that exist."""

import json
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


class Group(BaseModel):
    """A row of the table."""

    user: str
    shards: int
    services: list[str]
    paths: list[str]


class Job(BaseModel):
    """One CI job of the matrix."""

    name: str
    run: str
    slug: str


class Matrix(BaseModel):
    """The matrix `integration.sh jobs` prints."""

    include: list[Job]


def groups() -> dict[str, Group]:
    """Each group's row, from the table."""
    table = (ROOT / "tests" / "integration-groups.txt").read_text("utf-8")
    found: dict[str, Group] = {}
    for line in table.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, user, shards, services, paths = (part.strip() for part in line.split("|"))
        found[name] = Group(
            user=user, shards=int(shards), services=services.split(), paths=paths.split()
        )
    return found


def test_every_test_path_is_in_one_group() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    configured = pyproject["tool"]["pytest"]["ini_options"]["testpaths"]
    grouped = [path for group in groups().values() for path in group.paths]
    assert sorted(grouped) == sorted(configured)
    assert len(grouped) == len(set(grouped))


def test_services_and_users_exist() -> None:
    compose = Compose.model_validate(yaml.safe_load((ROOT / "compose.yaml").read_text("utf-8")))
    for name, group in groups().items():
        assert _USER.fullmatch(group.user), name
        assert group.shards >= 1, name
        assert set(group.services) <= set(compose.services), name


def test_the_script_reads_the_table() -> None:
    script = str(ROOT / "scripts" / "integration.sh")
    listed = run_program(script, ["groups"])
    assert listed.stdout.split() == list(groups())
    needed = run_program(script, ["services", "core", "mysql/2", "mysql"])
    assert needed.stdout.split() == ["postgres", "s3", "vault", "mysql", "jailer"]
    for wrong in ("nope", "core/2", "mysql/0", "mysql/x", "mysql/"):
        assert run_program(script, ["services", wrong]).status == 2, wrong
    assert run_program(script, []).status == 2


def test_every_shard_is_a_job() -> None:
    listed = run_program(str(ROOT / "scripts" / "integration.sh"), ["jobs"])
    jobs = Matrix.model_validate(json.loads(listed.stdout)).include
    expected = [
        (name, name, name)
        if group.shards == 1
        else (f"{name} {shard}/{group.shards}", f"{name}/{shard}", f"{name}-{shard}")
        for name, group in groups().items()
        for shard in range(1, group.shards + 1)
    ]
    assert [(job.name, job.run, job.slug) for job in jobs] == expected
