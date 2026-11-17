# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The Claude Code plugin is released with Datadouble and lists every skill."""

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_plugin_is_versioned_with_the_core() -> None:
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text("utf-8"))
    core = tomllib.loads((ROOT / "core" / "pyproject.toml").read_text("utf-8"))
    assert plugin["version"] == core["project"]["version"]
    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text("utf-8"))
    assert [entry["name"] for entry in marketplace["plugins"]] == [plugin["name"]]


def test_every_skill_follows_the_shared_rules() -> None:
    skills = sorted((ROOT / "skills").glob("*/SKILL.md"))
    assert len(skills) == 10
    for skill in skills:
        text = skill.read_text("utf-8")
        assert text.startswith(f"---\nname: {skill.parent.name}\ndescription: "), skill
        assert "../RULES.md" in text, skill


def test_every_scenario_has_graders() -> None:
    cases = sorted(path.parent for path in (ROOT / "evals").glob("*/prompt.md"))
    assert len(cases) >= 6
    for case in cases:
        assert list((case / "graders").glob("*.md")), case
        assert (case / "scaffold.sh").exists(), case
