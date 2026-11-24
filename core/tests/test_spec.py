# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

import json
from pathlib import Path

import pytest

from datadouble.catalog import ObjectKind
from datadouble.names import GroupName
from datadouble.spec import (
    AccountPrivileges,
    EnvSecret,
    ExpressionRule,
    FakerRule,
    FileSecret,
    HashRule,
    ImageFormat,
    NullRule,
    ObjectLevel,
    OnMissing,
    SpecError,
    Strict,
    StrictKind,
    StrictLevel,
    load_spec,
    parse_spec,
    spec_schema,
)

MINIMAL = "name: shop\nsources: {app: {engine: postgres}}\n"


def test_minimal_spec_defaults() -> None:
    spec = parse_spec(MINIMAL)
    assert spec.strict == Strict()
    assert spec.strict.data is StrictLevel.COLUMNS
    assert spec.strict.functions is ObjectLevel.WARN
    assert list(spec.images) == ["all"]
    assert spec.images[GroupName("all")].include == ("*",)
    assert spec.package.formats is None
    assert spec.store == ()


def test_full_spec_in_yaml() -> None:
    spec = parse_spec(
        """
name: shop
sources:
  app:
    engine: postgres
    connection:
      dsn:
        env: SHOP_DSN
exclude: ["dba_maint_*"]
structure_only: [event_log]
strict:
  data: tables
  triggers: fail
subset:
  as_of: 2026-11-09T00:00:00Z
  strategy: keys
  seeds:
    users: "created >= :as_of"
  follow:
    down: ["users -> posts"]
  edges:
    comments.author_id -> users:
      on_missing: null
    posts.category_id -> categories:
      follow: all
    comments.editor_id -> users:
      on_missing: drop
  virtual_fks:
    events.subject_id -> posts:
      when: "subject_type = 'Post'"
anonymize:
  users:
    email:
      faker: email
      unique: true
    note:
      null: {}
    name:
      expression: "CONCAT('user-', id)"
      where: "id > 1"
    created:
      hash:
        length: 8
hooks:
  before_load:
    - sql: "SET x = 1"
  after_transform:
    - sql: "UPDATE users SET name = 'x'"
      rewrites: [users.name]
images:
  all:
    include: ["*"]
  billing:
    include: ["billing.*"]
    requires: [all]
package:
  formats: [logical]
verify:
  pii_scan:
    allow:
      users.email: ["%@example.com"]
  assertions:
    - name: no admins
      sql: "SELECT 1 WHERE false"
store:
  - filesystem:
      path: /srv/images
secrets:
  anonymization_key:
    file: /run/secrets/key
"""
    )
    assert spec.strict.data is StrictLevel.TABLES
    assert spec.strict.triggers is ObjectLevel.FAIL
    assert spec.strict.views is ObjectLevel.WARN
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
    strict = parse_spec(MINIMAL + "strict: {data: off, events: off}").strict
    assert (strict.data, strict.events) == (StrictLevel.OFF, ObjectLevel.OFF)


def test_strict_levels_compare_per_kind() -> None:
    built = Strict(data=StrictLevel.TABLES, views=ObjectLevel.FAIL, events=ObjectLevel.OFF)
    assert built.weaker(Strict()) == (StrictKind.DATA, StrictKind.EVENTS)
    assert Strict.lowest().weaker(Strict.lowest()) == ()
    assert not Strict().failing()
    assert built.failing()


def test_json_is_accepted() -> None:
    spec = parse_spec(
        json.dumps(
            {
                "name": "shop",
                "sources": {"app": {"engine": "postgres"}},
                "secrets": {"anonymization_key": {"env": "KEY"}},
            }
        )
    )
    assert spec.secrets.anonymization_key == EnvSecret(env="KEY")


@pytest.mark.parametrize(
    ("text", "location", "problem"),
    [
        ("name: Shop\nsources: {app: {engine: postgres}}", "name", "lowercase"),
        (MINIMAL + "strict: sometimes", "strict", "a mapping of levels per kind"),
        (MINIMAL + "strict: off", "strict", "a mapping of levels per kind"),
        (MINIMAL + "strict: {data: warn}", "strict.data", "Input should be"),
        (MINIMAL + "strict: {functions: tables}", "strict.functions", "Input should be"),
        (MINIMAL + "strict: {rules: off}", "strict.rules", "Extra inputs"),
        (MINIMAL + "promote: {to: [{s3: {}}], strict: tables}", "promote.strict", "a mapping"),
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
        (MINIMAL + "objects: [{function: a, view: b}]", "objects.0", "at most 1"),
        (
            MINIMAL
            + "images: {a: {include: [x]}, b: {include: [y], standalone: true, requires: [a]}}",
            "images.b",
            "cannot also have `requires`",
        ),
        (MINIMAL + "images: {a: {include: [x], rows: {y: all}}}", "images.a", "set `standalone"),
        (
            MINIMAL + "images: {a: {include: [x], standalone: true, rows: {y: some}}}",
            "images.a.rows.y",
            "Input should be",
        ),
        (MINIMAL + "objects: [{table: a}]", "objects.0.table.[key]", "Input should be"),
        ("name: shop\nsource: {engine: postgres}", "source", "Extra inputs are not permitted"),
        ("name: shop\nsources: {}", "sources", "at least 1"),
        ("name: shop\nsources: {App: {engine: postgres}}", "sources", "lowercase"),
        (
            "name: shop\nsources: {a: {engine: postgres}, b: {engine: mysql}}",
            "sources",
            "every source must use one engine: source a is postgres and source b is mysql",
        ),
        (
            "name: shop\nsources: {a: {engine: postgres, image: x}, b: {engine: postgres}}",
            "sources",
            "sources a and b give different `image` settings",
        ),
        (
            "name: shop\nsources: {a: {engine: postgres}, b: {engine: postgres, server: {x: 1}}}",
            "sources",
            "sources a and b give different `server` settings",
        ),
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


def test_standalone_groups_and_declared_objects() -> None:
    spec = parse_spec(
        MINIMAL
        + "objects: [{function: public.uuid_generate_v4}]\n"
        + "images: {billing: {include: [billing.*], standalone: true,"
        + " objects: [{materialized-view: reporting.daily}]}}\n"
        + "files: {connection: {root: /srv}, select: {'*': 'true'}}\n"
    )
    billing = spec.images[GroupName("billing")]
    assert billing.standalone
    assert billing.objects == ({ObjectKind.MATERIALIZED_VIEW: "reporting.daily"},)
    assert spec.objects == ({ObjectKind.FUNCTION: "public.uuid_generate_v4"},)
    files = spec.files_spec()
    assert files is not None
    assert files.images[GroupName("billing")].objects == ()
    assert files.images[GroupName("billing")].standalone


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


def test_copy_accounts() -> None:
    written = "app-password"
    spec = parse_spec(
        MINIMAL
        + "package:\n  accounts:\n"
        + f"    - {{user: app, password: {written}}}\n"
        + "    - {user: reports, password: {env: COPY_PW}, privileges: read}\n"
    )
    app, reports = spec.package.accounts
    assert (app.user, app.password, app.privileges) == ("app", written, AccountPrivileges.ALL)
    assert reports.password == EnvSecret(env="COPY_PW")
    assert reports.privileges is AccountPrivileges.READ
    assert parse_spec(MINIMAL).package.accounts == ()
    for wrong in (
        "{user: '', password: x}",
        "{user: app, password: ''}",
        "{user: app, password: x, privileges: write}",
    ):
        with pytest.raises(SpecError):
            parse_spec(MINIMAL + f"package: {{accounts: [{wrong}]}}")
    with pytest.raises(SpecError):
        parse_spec(MINIMAL + "package: {account: {user: app, password: x}}")


def test_files_builds_carry_no_accounts() -> None:
    spec = parse_spec(
        MINIMAL
        + "package: {accounts: [{user: app, password: x}]}\n"
        + "files: {connection: {root: /srv}, select: {a: '*'}}\n"
    )
    files = spec.files_spec()
    assert files is not None
    assert files.package.accounts == ()


def test_the_sources_share_an_engine_and_a_working_server() -> None:
    spec = parse_spec(
        "name: shop\nsources:\n  app: {engine: postgres, image: pg:18}\n"
        "  warehouse: {engine: postgres, image: pg:18}\n"
    )
    assert spec.engine == "postgres"
    assert spec.working.image == "pg:18"
    assert list(spec.sources) == ["app", "warehouse"]
