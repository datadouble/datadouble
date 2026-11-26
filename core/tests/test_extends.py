# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Specs that extend other specs: reading, merging, checking and the files recorded."""

import hashlib
import io
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

from datadouble import api, cli
from datadouble.api import ErrorCode, default_lock
from datadouble.catalog import CatalogError
from datadouble.names import GroupName
from datadouble.outcome import ExitCode
from datadouble.spec import (
    EXTENDS,
    LoadedSpec,
    SpecError,
    merge_documents,
    parse_spec,
    read_spec,
    spec_schema,
)

if TYPE_CHECKING:
    from pydantic import JsonValue

BASE = """\
name: shop
sources:
  app:
    engine: postgres
exclude:
  - audit.*
  - logs.*
subset:
  seeds:
    customer: id <= 1000
    orders: id <= 50
"""
MICRO = """\
extends: shop.yml
name: shop-micro
exclude:
  - audit.*
subset:
  seeds:
    customer: id <= 5
"""


def write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def paths(loaded: LoadedSpec) -> list[str]:
    return [str(layer.path) for layer in loaded.layers]


def cli_run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = cli.main(list(argv), stdin=io.StringIO(""), stdout=out)
    return code, out.getvalue()


def test_a_spec_without_extends_is_one_layer(tmp_path: Path) -> None:
    path = write(tmp_path, "shop.yml", BASE)
    loaded = read_spec(path)
    assert loaded.spec == parse_spec(BASE)
    assert [(str(layer.path), layer.digest) for layer in loaded.layers] == [
        ("shop.yml", digest(path))
    ]
    assert loaded.directory == tmp_path


def test_mappings_merge_and_everything_else_replaces(tmp_path: Path) -> None:
    write(tmp_path, "shop.yml", BASE)
    micro = write(tmp_path, "shop-micro.yml", MICRO)
    loaded = read_spec(micro)
    assert loaded.spec.name == "shop-micro"
    # A list replaces the parent's whole list, not item by item.
    assert loaded.spec.exclude == ("audit.*",)
    # Mappings merge key by key: orders keeps the parent's seed.
    assert loaded.document["subset"] == {"seeds": {"customer": "id <= 5", "orders": "id <= 50"}}
    assert loaded.document["sources"] == {"app": {"engine": "postgres"}}
    assert EXTENDS not in loaded.document
    assert paths(loaded) == ["shop.yml", "shop-micro.yml"]


def test_merge_documents() -> None:
    base: dict[str, JsonValue] = {"a": {"b": 1, "c": [1, 2]}, "d": "x", "e": {"f": 1}}
    over: dict[str, JsonValue] = {"a": {"c": [3]}, "d": {"now": "a mapping"}, "e": None}
    assert merge_documents(base, over) == {
        "a": {"b": 1, "c": [3]},
        "d": {"now": "a mapping"},
        "e": None,
    }
    assert base == {"a": {"b": 1, "c": [1, 2]}, "d": "x", "e": {"f": 1}}


def test_parents_merge_left_to_right_then_the_file(tmp_path: Path) -> None:
    write(tmp_path, "shop.yml", BASE)
    write(tmp_path, "small.yml", "subset:\n  seeds:\n    customer: id <= 10\n")
    write(tmp_path, "eu.yml", "subset:\n  seeds:\n    customer: region = 'eu'\nexclude: []\n")
    both = write(tmp_path, "shop-eu.yml", "extends:\n  - shop.yml\n  - small.yml\n  - eu.yml\n"
                 "name: shop-eu\n")  # fmt: skip
    loaded = read_spec(both)
    assert loaded.spec.subset.seeds["customer"] == "region = 'eu'"
    assert loaded.spec.exclude == ()
    assert paths(loaded) == [
        "shop.yml", "small.yml", "eu.yml", "shop-eu.yml"
    ]  # fmt: skip
    own = write(tmp_path, "own.yml", "extends: [eu.yml, small.yml, shop.yml]\nname: own\n")
    # shop.yml comes last here, so its own seeds win.
    assert read_spec(own).spec.subset.seeds["customer"] == "id <= 1000"


def test_chains_resolve_depth_first(tmp_path: Path) -> None:
    write(tmp_path, "shop.yml", BASE)
    write(tmp_path, "shop-micro.yml", MICRO)
    tiny = write(
        tmp_path, "shop-tiny.yml", "extends: shop-micro.yml\nname: shop-tiny\n"
        "subset:\n  seeds:\n    orders: id <= 2\n"
    )  # fmt: skip
    loaded = read_spec(tiny)
    assert loaded.spec.subset.seeds == {"customer": "id <= 5", "orders": "id <= 2"}
    assert paths(loaded) == [
        "shop.yml", "shop-micro.yml", "shop-tiny.yml"
    ]  # fmt: skip


def test_a_file_reached_twice_is_one_layer(tmp_path: Path) -> None:
    write(tmp_path, "shop.yml", BASE)
    write(tmp_path, "a.yml", "extends: shop.yml\nsubset:\n  seeds:\n    customer: id <= 7\n")
    write(tmp_path, "b.yml", "extends: shop.yml\n")
    top = write(tmp_path, "top.yml", "extends: [a.yml, b.yml]\nname: top\n")
    loaded = read_spec(top)
    # shop.yml merges once, before a.yml, so b.yml doesn't bring its seed back.
    assert loaded.spec.subset.seeds["customer"] == "id <= 7"
    assert paths(loaded) == ["shop.yml", "a.yml", "b.yml", "top.yml"]
    # Named another way, it is still the same file.
    write(tmp_path, "c.yml", "extends: ./sub/../shop.yml\n")
    again = write(tmp_path, "again.yml", "extends: [a.yml, c.yml]\nname: again\n")
    assert read_spec(again).spec.subset.seeds["customer"] == "id <= 7"
    assert paths(read_spec(again)) == ["shop.yml", "a.yml", "c.yml", "again.yml"]


def test_two_sizes_of_one_base_combine(tmp_path: Path) -> None:
    write(tmp_path, "shop.yml", BASE + "store:\n  - filesystem:\n      path: /srv/store\n")
    write(
        tmp_path, "few.yml",
        "extends: shop.yml\nname: few\nexclude:\n  - audit.*\n"
        "subset:\n  seeds:\n    customer: id <= 5\n",
    )  # fmt: skip
    write(tmp_path, "nostore.yml", "extends: shop.yml\nname: nostore\nstore: []\n")
    combo = write(tmp_path, "combo.yml", "extends: [few.yml, nostore.yml]\nname: combo\n")
    loaded = read_spec(combo)
    # shop.yml doesn't merge again under nostore.yml and undo few.yml.
    assert loaded.spec.subset.seeds == {"customer": "id <= 5", "orders": "id <= 50"}
    assert loaded.spec.exclude == ("audit.*",)
    assert loaded.spec.store == ()
    assert paths(loaded) == ["shop.yml", "few.yml", "nostore.yml", "combo.yml"]


@pytest.mark.parametrize(
    ("files", "chain"),
    [
        ({"a.yml": "extends: a.yml\n"}, "a.yml -> a.yml"),
        ({"a.yml": "extends: b.yml\n", "b.yml": "extends: a.yml\n"}, "a.yml -> b.yml -> a.yml"),
        (
            {"a.yml": "extends: b.yml\n", "b.yml": "extends: c.yml\n", "c.yml": "extends: b.yml\n"},
            "b.yml -> c.yml -> b.yml",
        ),
    ],
)
def test_cycles_fail_naming_the_chain(tmp_path: Path, files: dict[str, str], chain: str) -> None:
    for name, text in files.items():
        write(tmp_path, name, text)
    with pytest.raises(SpecError, match=f"specs extend each other in a cycle: {chain}$"):
        read_spec(tmp_path / "a.yml")


def test_the_merged_name_must_differ_from_every_parent(tmp_path: Path) -> None:
    write(tmp_path, "shop.yml", BASE)
    same = write(tmp_path, "copy.yml", "extends: shop.yml\n")
    with pytest.raises(SpecError, match=r"has the name shop, which shop\.yml also has"):
        read_spec(same)
    write(tmp_path, "shop-micro.yml", MICRO)
    unnamed = write(tmp_path, "eu.yml", "extends: shop-micro.yml\n")
    with pytest.raises(SpecError, match=r"name shop-micro, which shop-micro\.yml also has"):
        read_spec(unnamed)
    again = write(tmp_path, "again.yml", "extends: shop-micro.yml\nname: shop\n")
    with pytest.raises(SpecError, match=r"name shop, which shop\.yml also has"):
        read_spec(again)


def test_parents_need_not_be_specs_but_the_result_is_checked(tmp_path: Path) -> None:
    write(tmp_path, "seeds.yml", "subset:\n  seeds:\n    customer: id <= 5\n")
    write(tmp_path, "shop.yml", BASE.replace("name: shop\n", "name: shop\nextends: seeds.yml\n"))
    assert read_spec(tmp_path / "shop.yml").spec.subset.seeds["customer"] == "id <= 1000"
    write(tmp_path, "typo.yml", "subset:\n  sedes:\n    customer: id <= 5\n")
    broken = write(tmp_path, "micro.yml", "extends: [shop.yml, typo.yml]\nname: micro\n")
    with pytest.raises(SpecError, match="with the specs it extends does not match") as raised:
        read_spec(broken)
    assert ("subset.sedes", "Extra inputs are not permitted") in raised.value.details


def test_paths_are_relative_to_the_file_that_names_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, "shop.yml", BASE)
    write(tmp_path, "sizes/common/seeds.yml", "subset:\n  seeds:\n    customer: id <= 3\n")
    write(tmp_path, "sizes/micro.yml", "extends: [../shop.yml, common/seeds.yml]\n")
    top = write(tmp_path, "shop-micro.yml", "extends: sizes/micro.yml\nname: shop-micro\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    loaded = read_spec(Path(os.path.relpath(top, elsewhere)))
    assert loaded.spec.subset.seeds["customer"] == "id <= 3"
    assert paths(loaded) == [
        "shop.yml", "sizes/common/seeds.yml", "sizes/micro.yml", "shop-micro.yml"
    ]  # fmt: skip


def test_parents_stay_inside_the_spec_directory(tmp_path: Path) -> None:
    write(tmp_path, "shop.yml", BASE)
    spec_dir = tmp_path / "datadouble"
    outside = write(spec_dir, "micro.yml", "extends: ../shop.yml\nname: micro\n")
    with pytest.raises(SpecError, match=r"extends \.\./shop\.yml, which is outside"):
        read_spec(outside)
    absolute = write(spec_dir, "abs.yml", f"extends: {tmp_path / 'shop.yml'}\nname: abs\n")
    with pytest.raises(SpecError, match="an absolute path"):
        read_spec(absolute)
    # A spec in a subdirectory is confined to its own directory, not the parent's.
    write(spec_dir, "shop.yml", BASE)
    write(spec_dir, "sizes/micro.yml", "extends: ../shop.yml\nname: micro\n")
    with pytest.raises(SpecError, match="outside"):
        read_spec(spec_dir / "sizes" / "micro.yml")
    assert read_spec(write(spec_dir, "ok.yml", "extends: shop.yml\nname: ok\n")).spec.name == "ok"


def test_symbolic_links_keep_their_names(tmp_path: Path) -> None:
    write(tmp_path, "shared/shop.yml", BASE)
    spec_dir = tmp_path / "datadouble"
    spec_dir.mkdir()
    # Checked as written, so a link may point out of the directory: its content travels by
    # its own name.
    (spec_dir / "linked.yml").symlink_to(tmp_path / "shared" / "shop.yml")
    micro = write(spec_dir, "micro.yml", "extends: linked.yml\nname: micro\n")
    loaded = read_spec(micro)
    assert paths(loaded) == ["linked.yml", "micro.yml"]
    assert loaded.layers[0].digest == digest(tmp_path / "shared" / "shop.yml")
    # A link that names its parent by a relative path finds it from the link's directory,
    # not the target's.
    write(tmp_path, "shared/seeds.yml", "subset:\n  seeds:\n    customer: id <= 99\n")
    write(spec_dir, "seeds.yml", "subset:\n  seeds:\n    customer: id <= 3\n")
    write(tmp_path, "shared/overlay.yml", "extends: seeds.yml\n")
    (spec_dir / "overlay.yml").symlink_to(tmp_path / "shared" / "overlay.yml")
    tiny = write(spec_dir, "tiny.yml", "extends: [linked.yml, overlay.yml]\nname: tiny\n")
    loaded = read_spec(tiny)
    assert loaded.spec.subset.seeds["customer"] == "id <= 3"
    assert paths(loaded) == ["linked.yml", "seeds.yml", "overlay.yml", "tiny.yml"]


def test_a_config_map_directory_reads_by_its_link_names(tmp_path: Path) -> None:
    # A ConfigMap mounted as a directory holds each key as a link into ..data.
    data = tmp_path / "spec" / "..2026_11_25"
    write(data, "base.yml", BASE)
    write(data, "spec.yml", "extends: base.yml\nname: shop-micro\n")
    (tmp_path / "spec" / "..data").symlink_to("..2026_11_25")
    for name in ("base.yml", "spec.yml"):
        (tmp_path / "spec" / name).symlink_to(f"..data/{name}")
    loaded = read_spec(tmp_path / "spec" / "spec.yml")
    assert loaded.spec.name == "shop-micro"
    assert paths(loaded) == ["base.yml", "spec.yml"]
    assert loaded.directory == tmp_path / "spec"


def test_a_linked_spec_reads_from_the_links_directory(tmp_path: Path) -> None:
    write(tmp_path, "real/shop.yml", BASE)
    write(tmp_path, "real/micro.yml", "extends: shop.yml\nname: micro\n")
    write(tmp_path, "sizes/shop.yml", BASE.replace("id <= 1000", "id <= 4"))
    link = tmp_path / "sizes" / "micro.yml"
    link.symlink_to(tmp_path / "real" / "micro.yml")
    loaded = read_spec(link)
    assert loaded.directory == tmp_path / "sizes"
    assert loaded.spec.subset.seeds["customer"] == "id <= 4"
    assert paths(loaded) == ["shop.yml", "micro.yml"]
    # The link's directory confines its parents.
    write(tmp_path, "real/up.yml", "extends: ../real/shop.yml\nname: up\n")
    (tmp_path / "sizes" / "up.yml").symlink_to(tmp_path / "real" / "up.yml")
    with pytest.raises(SpecError, match="outside"):
        read_spec(tmp_path / "sizes" / "up.yml")


def test_a_cycle_through_a_link_fails(tmp_path: Path) -> None:
    write(tmp_path, "a.yml", "extends: b.yml\n")
    (tmp_path / "b.yml").symlink_to("a.yml")
    with pytest.raises(SpecError, match=r"in a cycle: a\.yml -> b\.yml$"):
        read_spec(tmp_path / "a.yml")


@pytest.mark.parametrize("value", ["3", "[a.yml, 3]", "''", "{a: b}", "null"])
def test_extends_takes_paths(tmp_path: Path, value: str) -> None:
    path = write(tmp_path, "s.yml", f"extends: {value}\nname: s\n")
    with pytest.raises(SpecError, match="takes a path, or a list of paths"):
        read_spec(path)


def test_unreadable_layers(tmp_path: Path) -> None:
    missing = write(tmp_path, "s.yml", "extends: nope.yml\nname: s\n")
    with pytest.raises(SpecError, match="cannot read"):
        read_spec(missing)
    with pytest.raises(SpecError, match="cannot read"):
        read_spec(tmp_path / "absent.yml")
    write(tmp_path, "list.yml", "- a\n- b\n")
    listed = write(tmp_path, "l.yml", "extends: list.yml\nname: l\n")
    with pytest.raises(SpecError, match=r"list\.yml is not a mapping"):
        read_spec(listed)
    write(tmp_path, "bad.yml", "a: [\n")
    with pytest.raises(SpecError, match=r"bad\.yml is not valid YAML"):
        read_spec(write(tmp_path, "b.yml", "extends: bad.yml\n"))
    (tmp_path / "binary.yml").write_bytes(b"\xff\xfe")
    with pytest.raises(SpecError, match=r"binary\.yml is not UTF-8"):
        read_spec(write(tmp_path, "c.yml", "extends: binary.yml\n"))


def test_an_empty_parent_adds_nothing(tmp_path: Path) -> None:
    write(tmp_path, "empty.yml", "")
    write(tmp_path, "shop.yml", "extends: empty.yml\n" + BASE)
    assert read_spec(tmp_path / "shop.yml").spec == parse_spec(BASE)


def test_parse_spec_refuses_extends() -> None:
    with pytest.raises(SpecError, match="extends other specs, which only a spec read from its"):
        parse_spec(MICRO)


def test_extends_is_in_the_schema() -> None:
    properties = spec_schema()["properties"]
    assert isinstance(properties, dict)
    assert next(iter(properties)) == EXTENDS
    extends = properties[EXTENDS]
    assert isinstance(extends, dict)
    assert "relative to this file" in str(extends["description"])


# The lock each spec uses


def test_the_default_lock_is_named_after_the_spec(tmp_path: Path) -> None:
    assert default_lock(tmp_path / "shop-micro.yml") == tmp_path / "shop-micro.lock"
    assert default_lock(tmp_path / "shop.json") == tmp_path / "shop.lock"
    request = api.PlanRequest(command=api.Command.PLAN, spec=tmp_path / "shop.yml")
    assert request.lock_path() == tmp_path / "shop.lock"
    given = request.model_copy(update={"lock": tmp_path / "other.lock"})
    assert given.lock_path() == tmp_path / "other.lock"


def test_an_old_catalog_lock_fails_saying_to_rename_it(tmp_path: Path) -> None:
    write(tmp_path, "shop.yml", BASE)
    write(tmp_path, "catalog.lock", "{}\n")
    with pytest.raises(CatalogError, match=r"rename it to shop\.lock"):
        default_lock(tmp_path / "shop.yml")
    response = api.handle_json(json.dumps({"command": "plan", "spec": str(tmp_path / "shop.yml")}))
    assert isinstance(response, api.ErrorResponse)
    assert response.error is ErrorCode.CATALOG_LOCK
    assert "has no lock shop.lock, but" in response.message
    # The spec's own lock, or --lock, are used as they are.
    write(tmp_path, "shop.lock", "{}\n")
    assert default_lock(tmp_path / "shop.yml") == tmp_path / "shop.lock"
    # A spec named catalog has catalog.lock as its own lock.
    assert default_lock(tmp_path / "catalog.yml") == tmp_path / "catalog.lock"


# The spec command


def test_the_spec_command(tmp_path: Path) -> None:
    base = write(tmp_path, "shop.yml", BASE)
    micro = write(tmp_path, "shop-micro.yml", MICRO)
    code, out = cli_run("spec", "--spec", str(micro))
    assert code == ExitCode.SUCCESS
    assert out == (
        "# read from, in the order they merged:\n"
        f"#   shop.yml ({digest(base)})\n"
        f"#   shop-micro.yml ({digest(micro)})\n"
        "name: shop-micro\n"
        "sources:\n"
        "  app:\n"
        "    engine: postgres\n"
        "exclude:\n"
        "  - audit.*\n"
        "subset:\n"
        "  seeds:\n"
        "    customer: id <= 5\n"
        "    orders: id <= 50\n"
    )
    # The printed spec is itself a spec, with the same meaning.
    assert parse_spec(out) == read_spec(micro).spec
    code, out = cli_run("--json", "spec", "--spec", str(micro))
    response = json.loads(out)
    assert response["command"] == "spec"
    assert response["outcome"] == "success"
    assert response["spec"]["subset"]["seeds"]["customer"] == "id <= 5"
    assert response["layers"] == [
        {"path": "shop.yml", "digest": digest(base)},
        {"path": "shop-micro.yml", "digest": digest(micro)},
    ]


def test_the_spec_command_in_json(tmp_path: Path) -> None:
    write(tmp_path, "shop.yml", BASE)
    micro = write(tmp_path, "shop-micro.yml", MICRO)
    response = api.handle_json(json.dumps({"command": "spec", "spec": str(micro)}))
    assert isinstance(response, api.SpecResponse)
    assert response.spec["name"] == "shop-micro"
    _, out = cli_run("--dry-run", "spec", "--spec", "s.yml")
    assert json.loads(out) == {"command": "spec", "spec": "s.yml"}
    write(tmp_path, "copy.yml", "extends: shop.yml\n")
    failed = api.handle_json(json.dumps({"command": "spec", "spec": str(tmp_path / "copy.yml")}))
    assert isinstance(failed, api.ErrorResponse)
    assert failed.error is ErrorCode.INVALID_SPEC
    code, out = cli_run("spec", "--spec", str(tmp_path / "copy.yml"))
    assert code == ExitCode.FAILURE
    assert out.startswith("error: ")


def test_the_spec_command_keeps_timestamps_and_groups(tmp_path: Path) -> None:
    write(
        tmp_path, "shop.yml",
        BASE + "images:\n  orders:\n    include:\n      - orders\n",
    )  # fmt: skip
    micro = write(
        tmp_path, "m.yml", "extends: shop.yml\nname: m\nsubset:\n  as_of: 2026-01-02T03:04:05Z\n"
    )
    response = api.handle(api.SpecRequest(command=api.Command.SPEC, spec=micro))
    assert isinstance(response, api.SpecResponse)
    assert response.spec["subset"] == {
        "seeds": {"customer": "id <= 1000", "orders": "id <= 50"},
        "as_of": "2026-01-02T03:04:05+00:00",
    }
    _, out = cli_run("spec", "--spec", str(micro))
    assert list(read_spec(micro).spec.images) == [GroupName("orders")]
    assert yaml.safe_load(out)["images"] == {"orders": {"include": ["orders"]}}
