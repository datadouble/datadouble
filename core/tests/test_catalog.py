# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

import json
from pathlib import Path

import pytest

from datadouble.catalog import CatalogError, CatalogLock, normalize, read_lock, write_lock
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
