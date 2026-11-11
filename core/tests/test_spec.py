# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

import json
from pathlib import Path

import pytest

from datadouble.names import GroupName
from datadouble.spec import (
    EnvSecret,
    ExpressionRule,
    FakerRule,
    FileSecret,
    HashRule,
    ImageFormat,
    NullRule,
    OnMissing,
    SpecError,
    StrictLevel,
    load_spec,
    parse_spec,
    spec_schema,
)

MINIMAL = "name: shop\nsource: {engine: postgres}\n"


def test_minimal_spec_defaults() -> None:
    spec = parse_spec(MINIMAL)
    assert spec.strict is StrictLevel.COLUMNS
    assert list(spec.images) == ["all"]
    assert spec.images[GroupName("all")].include == ("*",)
    assert spec.package.formats is None
    assert spec.store == ()


def test_full_spec_in_yaml() -> None:
    spec = parse_spec(
        """
name: shop
source:
  engine: postgres
  connection: {dsn: {env: SHOP_DSN}}
exclude: ["dba_maint_*"]
structure_only: [event_log]
strict: tables
subset:
  as_of: 2026-11-09T00:00:00Z
  strategy: keys
  seeds: {users: "created >= :as_of"}
  follow: {down: ["users -> posts"]}
  edges:
    comments.author_id -> users: { on_missing: null }
    posts.category_id -> categories: { follow: all }
    comments.editor_id -> users: { on_missing: drop }
  virtual_fks:
    events.subject_id -> posts: { when: "subject_type = 'Post'" }
anonymize:
  users:
    email: {faker: email, unique: true}
    note: {null: {}}
    name: {expression: "CONCAT('user-', id)", where: "id > 1"}
    created: {hash: {length: 8}}
hooks:
  before_load: [{sql: "SET x = 1"}]
  after_transform:
    - sql: "UPDATE users SET name = 'x'"
      rewrites: [users.name]
images:
  all: {include: ["*"]}
  billing: {include: ["billing.*"], requires: [all]}
package: {formats: [logical]}
verify:
  pii_scan: {allow: {users.email: ["%@example.com"]}}
  assertions: [{name: no admins, sql: "SELECT 1 WHERE false"}]
store:
  - filesystem: {path: /srv/images}
secrets:
  anonymization_key: {file: /run/secrets/key}
"""
    )
    assert spec.strict is StrictLevel.TABLES
    assert spec.subset.as_of is not None
    assert spec.subset.as_of.tzinfo is not None
    edges = spec.subset.edges
    assert edges["comments.author_id -> users"].on_missing is OnMissing.NULL
    assert edges["comments.editor_id -> users"].on_missing is OnMissing.DROP
    assert edges["posts.category_id -> categories"].follow == "all"
    rules = spec.anonymize["users"]
    assert isinstance(rules["email"], FakerRule)
    assert rules["email"].unique
    assert isinstance(rules["note"], NullRule)
    assert isinstance(rules["name"], ExpressionRule)
    assert isinstance(rules["created"], HashRule)
    assert rules["created"].hash.length == 8
    assert spec.package.formats == (ImageFormat.LOGICAL,)
    assert spec.secrets.anonymization_key == FileSecret(file=Path("/run/secrets/key"))
    assert spec.images[GroupName("billing")].requires == ("all",)


def test_yaml_off_is_the_off_level() -> None:
    assert parse_spec(MINIMAL + "strict: off").strict is StrictLevel.OFF


def test_json_is_accepted() -> None:
    spec = parse_spec(
        json.dumps(
            {
                "name": "shop",
                "source": {"engine": "postgres"},
                "secrets": {"anonymization_key": {"env": "KEY"}},
            }
        )
    )
    assert spec.secrets.anonymization_key == EnvSecret(env="KEY")


@pytest.mark.parametrize(
    ("text", "location", "problem"),
    [
        ("name: Shop\nsource: {engine: postgres}", "name", "lowercase"),
        (MINIMAL + "strict: sometimes", "strict", "Input should be"),
        (MINIMAL + "surprise: 1", "surprise", "Extra inputs"),
        (MINIMAL + "images: {}", "images", "at least one image group"),
        (MINIMAL + "images: {Bad: {include: ['*']}}", "images", "lowercase"),
        (MINIMAL + "images: {a: {include: ['*'], requires: [b]}}", "images", "not defined"),
        (MINIMAL + "store: [{a: {}, b: {}}]", "store", "one key"),
        (
            MINIMAL + "subset: {edges: {'a -> b': {follow: all, on_missing: drop}}}",
            "subset.edges.a -> b",
            "exactly one",
        ),
        (MINIMAL + "subset: {edges: {'a -> b': {}}}", "subset.edges.a -> b", "exactly one"),
        (MINIMAL + "package: {formats: []}", "package.formats", "at least 1"),
    ],
)
def test_invalid_specs(text: str, location: str, problem: str) -> None:
    with pytest.raises(SpecError) as caught:
        parse_spec(text)
    assert any(loc == location and problem in msg for loc, msg in caught.value.details), (
        caught.value.details
    )


def test_invalid_yaml() -> None:
    with pytest.raises(SpecError, match="not valid YAML"):
        parse_spec("name: [unclosed")


def test_unparseable_values() -> None:
    with pytest.raises(SpecError, match="no JSON form"):
        parse_spec(MINIMAL + "exclude: !!binary aGk=")


def test_root_level_problem() -> None:
    with pytest.raises(SpecError) as caught:
        parse_spec("[]")
    assert caught.value.details[0][0] == "(spec)"


def test_load_spec(tmp_path: Path) -> None:
    path = tmp_path / "shop.yml"
    path.write_text(MINIMAL, encoding="utf-8")
    assert load_spec(path).name == "shop"
    with pytest.raises(SpecError, match="cannot read"):
        load_spec(tmp_path / "missing.yml")


def test_schema() -> None:
    schema = spec_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["title"] == "Datadouble spec"
    assert "subset" in json.dumps(schema)
