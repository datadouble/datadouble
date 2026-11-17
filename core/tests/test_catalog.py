# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

import json
from pathlib import Path

import pytest

from datadouble.catalog import (
    CatalogError,
    CatalogLock,
    Requirement,
    RequirementKind,
    normalize,
    read_lock,
    write_lock,
)
from datadouble.engine import unmet
from datadouble.names import ColumnName, ColumnRef, TableName, check_name
from fakes import catalog, table


def test_table_and_column_lookup() -> None:
    users = table("users", ["id", "email"])
    shop_catalog = catalog(users)
    assert shop_catalog.table(TableName.of("public", "users")) == users
    assert shop_catalog.table(TableName.of("public", "nope")) is None
    assert users.column(ColumnName("email")) is not None
    assert users.column(ColumnName("nope")) is None
    assert shop_catalog.names() == (users.name,)
    assert shop_catalog.without(frozenset({users.name})).tables == ()


def test_fingerprint_ignores_table_and_reference_order() -> None:
    users = table("users", ["id"])
    posts = table(
        "posts",
        ["id", "a", "b"],
        references=[("z_fk", ("a",), "users", ("id",)), ("a_fk", ("b",), "users", ("id",))],
    )
    reordered = posts.model_copy(update={"foreign_keys": tuple(reversed(posts.foreign_keys))})
    assert catalog(users, posts).fingerprint() == catalog(reordered, users).fingerprint()
    assert normalize(catalog(posts, users)).names() == (posts.name, users.name)


def test_column_order_is_part_of_the_fingerprint() -> None:
    assert (
        catalog(table("t", ["id", "a"])).fingerprint()
        != catalog(table("t", ["a", "id"])).fingerprint()
    )


def test_lock_round_trip(tmp_path: Path) -> None:
    lock = CatalogLock.of(catalog(table("users", ["id"]), table("a", ["id"])))
    path = tmp_path / "catalog.lock"
    write_lock(lock, path)
    assert path.read_text(encoding="utf-8").endswith("}\n")
    assert read_lock(path) == lock


def test_missing_lock(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="datadouble lock"):
        read_lock(tmp_path / "catalog.lock")


def test_invalid_lock(tmp_path: Path) -> None:
    path = tmp_path / "catalog.lock"
    path.write_text('{"format": 2}', encoding="utf-8")
    with pytest.raises(CatalogError, match="not a valid catalog lock"):
        read_lock(path)


def test_hand_edited_lock(tmp_path: Path) -> None:
    path = tmp_path / "catalog.lock"
    write_lock(CatalogLock.of(catalog(table("users", ["id"]))), path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["catalog"]["tables"][0]["name"]["name"] = "people"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CatalogError, match="edited by hand"):
        read_lock(path)


def test_names() -> None:
    assert str(TableName.of("public", "users")) == "public.users"
    assert str(TableName.of("", "users")) == "users"
    ref = ColumnRef(table=TableName.of("public", "users"), column=ColumnName("email"))
    assert str(ref) == "public.users.email"
    assert check_name("spec name", "pagila-1") == "pagila-1"
    with pytest.raises(ValueError, match="lowercase"):
        check_name("spec name", "Pagila")


def test_a_lock_it_cannot_write(tmp_path: Path) -> None:
    (tmp_path / "taken").write_text("not a directory", encoding="utf-8")
    with pytest.raises(CatalogError, match=r"cannot write the catalog lock .*taken/catalog.lock"):
        write_lock(
            CatalogLock.of(catalog(table("public.items", "id"))),
            tmp_path / "taken" / "catalog.lock",
        )


VECTOR = Requirement(kind=RequirementKind.EXTENSION, name="vector", version="0.8.2")
CUBE = Requirement(kind=RequirementKind.EXTENSION, name="cube", version="1.5")


def test_requirements_change_the_fingerprint_but_not_their_order() -> None:
    plain = catalog(table("users", ["id"]))
    with_two = plain.model_copy(update={"requirements": (VECTOR, CUBE)})
    reordered = plain.model_copy(update={"requirements": (CUBE, VECTOR)})
    assert with_two.fingerprint() != plain.fingerprint()
    assert with_two.fingerprint() == reordered.fingerprint()
    assert with_two.requirement("cube") == CUBE
    assert with_two.requirement("nope") is None
    assert str(VECTOR) == "extension vector 0.8.2"
    assert str(Requirement(kind=RequirementKind.MODULE, name="ReJSON")) == "module ReJSON"


def test_a_lock_without_requirements_is_written_as_before(tmp_path: Path) -> None:
    path = tmp_path / "catalog.lock"
    write_lock(CatalogLock.of(catalog(table("users", ["id"]))), path)
    assert "requirements" not in json.loads(path.read_text(encoding="utf-8"))["catalog"]
    lock = CatalogLock.of(
        catalog(table("users", ["id"])).model_copy(update={"requirements": (VECTOR,)})
    )
    write_lock(lock, path)
    assert read_lock(path) == lock


def test_unmet_requirements() -> None:
    available = (
        VECTOR.model_copy(update={"version": "0.8.7"}),
        VECTOR,
        Requirement(kind=RequirementKind.MODULE, name="search"),
    )
    assert unmet((VECTOR,), available) == ()
    assert (
        unmet((Requirement(kind=RequirementKind.MODULE, name="search", version="2"),), available)
        == ()
    )
    assert unmet((CUBE, VECTOR.model_copy(update={"version": "0.9"})), available) == (
        "extension cube is not in the working image",
        "extension vector 0.9 is not in the working image, which has 0.8.2, 0.8.7",
    )
