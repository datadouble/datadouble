# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

import json
from pathlib import Path

import pytest

from datadouble.catalog import (
    CatalogError,
    CatalogLock,
    Change,
    ColumnKind,
    LockChange,
    ObjectKind,
    Requirement,
    RequirementKind,
    changes,
    describe_changes,
    normalize,
    read_lock,
    write_lock,
)
from datadouble.engine import unmet
from datadouble.names import ColumnName, ColumnRef, EngineName, TableName, check_name
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


def test_changes_between_locks() -> None:
    old = catalog(
        table("users", ["id", "email", "note", "age", "zip"], kinds={"email": ColumnKind.TEXT}),
        table(
            "posts",
            ["id", "user_id", "editor_id"],
            references=[
                ("posts_user", ("user_id",), "users", ("id",)),
                ("posts_editor", ("editor_id",), "users", ("id",)),
                ("posts_gone", ("editor_id",), "users", ("id",)),
            ],
        ),
        table("legacy", ["id"]),
    ).model_copy(
        update={
            "requirements": (
                Requirement(kind=RequirementKind.EXTENSION, name="vector", version="0.8.1"),
                Requirement(kind=RequirementKind.EXTENSION, name="old"),
                Requirement(kind=RequirementKind.EXTENSION, name="same", version="1"),
            )
        }
    )
    new = catalog(
        table(
            "users",
            ["id", "email", "zip", "age", "phone"],
            nullable=["email", "phone"],
            kinds={"email": ColumnKind.TEXT, "phone": ColumnKind.TEXT, "age": ColumnKind.DECIMAL},
        ),
        table(
            "posts",
            ["id", "user_id", "editor_id"],
            key=("id", "user_id"),
            references=[
                ("posts_user", ("user_id",), "users", ("id",)),
                ("posts_editor", ("user_id",), "users", ("id",)),
                ("posts_new", ("editor_id",), "users", ("id",)),
            ],
        ),
        table(
            "tags",
            ["id", "label", "post_id"],
            kinds={"label": ColumnKind.TEXT},
            references=[("tags_post", ("post_id",), "posts", ("id",))],
        ),
        table("bare", ["value"], key=()),
    ).model_copy(
        update={
            "requirements": (
                Requirement(kind=RequirementKind.EXTENSION, name="vector", version="0.8.2"),
                Requirement(kind=RequirementKind.EXTENSION, name="same", version="1"),
                Requirement(kind=RequirementKind.PLUGIN, name="audit"),
            )
        }
    )
    assert [str(change) for change in changes(old, new)] == [
        "added table public.bare",
        "added column public.bare.value: integer (integer) not null",
        "removed table public.legacy",
        "changed primary-key public.posts: (id, user_id), was (id)",
        (
            "changed foreign-key public.posts posts_editor:"
            " (user_id) -> public.users(id), was (editor_id) -> public.users(id)"
        ),
        "removed foreign-key public.posts posts_gone",
        "added foreign-key public.posts posts_new: (editor_id) -> public.users(id)",
        "added table public.tags",
        "added column public.tags.id: integer (integer) not null",
        "added column public.tags.label: text (text) not null",
        "added column public.tags.post_id: integer (integer) not null",
        "added primary-key public.tags: (id)",
        "added foreign-key public.tags tags_post: (post_id) -> public.posts(id)",
        "changed column public.users.email: text (text) null, was text (text) not null",
        "removed column public.users.note",
        (
            "changed column public.users.age: decimal (decimal) not null,"
            " was integer (integer) not null"
        ),
        "added column public.users.phone: text (text) null",
        "changed table public.users: column order (id, email, zip, age), was (id, email, age, zip)",
        "removed requirement extension old",
        "changed requirement extension vector: 0.8.2, was 0.8.1",
        "added requirement plugin audit",
    ]
    assert changes(new, new) == ()
    assert changes(old, old.model_copy(update={"requirements": ()}))[-1] == LockChange(
        change=Change.REMOVED, kind=ObjectKind.REQUIREMENT, object="extension vector"
    )
    versioned = Requirement(kind=RequirementKind.EXTENSION, name="v", version="1")
    unversioned = Requirement(kind=RequirementKind.EXTENSION, name="v")
    (changed,) = changes(
        old.model_copy(update={"requirements": (unversioned,)}),
        old.model_copy(update={"requirements": (versioned,)}),
    )
    assert changed.detail == "1, was no version"


def test_changes_to_the_catalog_as_a_whole() -> None:
    users = catalog(table("users", ["id"]))
    other = users.model_copy(update={"engine": EngineName("other")})
    assert [str(change) for change in changes(users, other)] == [
        "changed catalog engine: other, was fake"
    ]
    # Nothing a lock lists differs, yet the fingerprints do: say so, not "no changes".
    vector = Requirement(kind=RequirementKind.EXTENSION, name="vector", version="1")
    twice = users.model_copy(update={"requirements": (vector, vector)})
    once = users.model_copy(update={"requirements": (vector,)})
    (whole,) = changes(twice, once)
    assert whole.kind is ObjectKind.CATALOG
    assert whole.object == "fingerprint"


def test_describing_changes() -> None:
    unreadable = (
        "the earlier lock could not be read (x.lock has been edited by hand): check every"
        " table and column in the new one for personal data"
    )
    assert describe_changes(None, "x.lock has been edited by hand") == (unreadable,)
    assert describe_changes(None) == (
        "no earlier lock to compare with: check every table and column in it for personal data",
    )
    assert describe_changes(()) == ("no changes from the earlier lock",)
    added = LockChange(change=Change.ADDED, kind=ObjectKind.TABLE, object="public.tags")
    assert describe_changes((added,)) == (
        "1 change(s) from the earlier lock; check new and changed columns for personal data:",
        "  added table public.tags",
    )
