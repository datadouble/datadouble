# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""The generated reference pages match the code they come from."""

import io
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import reference

if TYPE_CHECKING:
    from pydantic import JsonValue


def test_the_committed_pages_are_current() -> None:
    assert reference.main(["check"]) == 0


def test_types() -> None:
    assert reference.type_of({"$ref": "#/$defs/Source"}) == "[Source](#source)"
    assert reference.type_of({"$ref": "#/$defs/JsonValue"}) == "JSON"
    either: dict[str, JsonValue] = {
        "anyOf": [{"type": "string"}, {"type": "null"}, {"type": "string"}]
    }
    assert reference.type_of(either) == "string"
    assert reference.type_of({"enum": ["a", "b"]}) == "`a` or `b`"
    assert reference.type_of({"type": "array", "items": {"type": "integer"}}) == "list of integer"
    mapping: dict[str, JsonValue] = {"type": "object", "additionalProperties": {"type": "string"}}
    assert reference.type_of(mapping) == "map of string"
    assert reference.type_of({}) == "any"


def test_defaults() -> None:
    assert reference.default_of({}) == ""
    assert reference.default_of({"default": None}) == ""
    assert reference.default_of({"default": False}) == "`false`"
    assert reference.default_of({"default": {"a": 1}}) == "see type"


def test_writing_and_checking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert reference.main(["check"], root=tmp_path) == 1
    assert "docs/using/spec-reference.md differs" in out.getvalue()
    assert reference.main(["write"], root=tmp_path) == 0
    assert reference.main(["check"], root=tmp_path) == 0
    assert reference.main([], root=tmp_path) == 2


def test_sections_skip_what_is_not_a_field() -> None:
    lines = list(reference.section("Odd", {"properties": {"a": True}}, "###"))
    assert lines == [
        "### Odd\n",
        "| Field | Type | Default | Description |",
        "|---|---|---|---|",
        "",
    ]
