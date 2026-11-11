# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

import pytest

from datadouble.catalog import Catalog, CatalogLock, Table
from datadouble.names import ColumnName, TableName
from datadouble.resolve import ResolveError, Resolver
from datadouble.spec import StrictLevel
from datadouble.strict import Difference, Severity, check_strict, resolve_excludes
from fakes import catalog, shop, table


def resolver(*, case_sensitive: bool = False) -> Resolver:
    return Resolver(shop(), case_sensitive=case_sensitive)


def test_globs() -> None:
    r = resolver()
    assert [str(n) for n in r.glob("dba_maint_*")] == ["public.dba_maint_runs"]
    assert [str(n) for n in r.glob("audit.*")] == ["audit.log"]
    assert [str(n) for n in r.glob("*.LOG")] == ["audit.log"]
    assert r.glob("*.LOG") != resolver(case_sensitive=True).glob("*.LOG")


def test_tables_and_columns() -> None:
    r = resolver()
    assert str(r.table("Users").name) == "public.users"
    assert str(r.table("audit.log").name) == "audit.log"
    assert str(r.column("users.EMAIL")) == "public.users.email"
    assert str(r.column("public.users.email")) == "public.users.email"
    with pytest.raises(ResolveError, match="not in the catalog"):
        r.table("nope")
    with pytest.raises(ResolveError, match="not in table"):
        r.column("users.nope")
    with pytest.raises(ResolveError, match="not a column name"):
        r.column("users")


def test_ambiguous_tables() -> None:
    r = Resolver(
        catalog(table("t", ["id"]), table("t", ["id"], namespace="x")), case_sensitive=False
    )
    with pytest.raises(ResolveError, match="ambiguous"):
        r.table("t")
    assert str(r.table("x.t").name) == "x.t"


def test_references() -> None:
    r = resolver()
    ref = r.reference("comments.author_id -> users")
    assert (str(ref.child.name), ref.columns, str(ref.parent.name), ref.parent_columns) == (
        "public.comments",
        ("author_id",),
        "public.users",
        None,
    )
    ref = r.reference("film_actor.(film_id, actor_id) -> users.(id, created)")
    assert ref.columns == ("film_id", "actor_id")
    assert ref.parent_columns == ("id", "created")
    ref = r.reference("posts -> users.id")
    assert ref.columns is None
    assert ref.parent_columns == ("id",)
    down = r.downward("users -> posts.user_id")
    assert (str(down.parent.name), str(down.child.name), down.columns) == (
        "public.users",
        "public.posts",
        ("user_id",),
    )
    for bad in ("users", "a -> b -> c", " -> users"):
        with pytest.raises(ResolveError, match="is not a reference"):
            r.reference(bad)
        with pytest.raises(ResolveError, match="is not a downward edge"):
            r.downward(bad)
    with pytest.raises(ResolveError, match="is not a table or column"):
        r.reference("nope -> users")


def test_excludes() -> None:
    excludes = resolve_excludes(resolver(), ("dba_*", "audit.*", "zzz"), ("events",))
    assert {str(n) for n in excludes.excluded()} == {"public.dba_maint_runs", "audit.log"}
    assert excludes.exclude[2].tables == ()
    assert {str(n) for n in excludes.structure_only_tables()} == {"public.events"}


def lock_of(*tables: Table) -> CatalogLock:
    return CatalogLock.of(catalog(*tables))


USERS = table("users", ["id", "email"])


def findings(
    live: Catalog,
    lock: CatalogLock,
    level: StrictLevel,
    excluded: frozenset[TableName] = frozenset(),
) -> set[tuple[str, str, str]]:
    report = check_strict(live, lock.catalog, lock.fingerprint, excluded, level)
    assert report.level is level
    return {(f.difference.value, f.severity.value, f.object) for f in report.findings}


@pytest.mark.parametrize(
    ("level", "table_severity", "column_severity"),
    [
        (StrictLevel.OFF, Severity.WARN, Severity.WARN),
        (StrictLevel.TABLES, Severity.FAIL, Severity.WARN),
        (StrictLevel.COLUMNS, Severity.FAIL, Severity.FAIL),
    ],
)
def test_strict_levels(
    level: StrictLevel, table_severity: Severity, column_severity: Severity
) -> None:
    live = catalog(
        table("users", ["id", "email", "phone"]), table("orders", ["id"]), table("tmp_x", ["id"])
    )
    result = findings(live, lock_of(USERS), level, frozenset({TableName.of("public", "tmp_x")}))
    assert result == {
        ("new-table", table_severity.value, "public.orders"),
        ("new-column", column_severity.value, "public.users.phone"),
    }


def test_removed_and_changed_objects_always_fail() -> None:
    lock = lock_of(
        USERS,
        table("orders", ["id", "user_id"], references=[("fk", ("user_id",), "users", ("id",))]),
        table("gone", ["id"]),
        table("ignored", ["id"]),
    )
    live = catalog(
        table("users", ["id"], kinds={}),
        table("orders", ["id", "user_id"]),
    )
    live = live.model_copy(
        update={
            "tables": (
                live.tables[0],
                live.tables[1].model_copy(
                    update={
                        "columns": (
                            live.tables[1].columns[0],
                            live.tables[1].columns[1].model_copy(update={"nullable": True}),
                        )
                    }
                ),
            )
        }
    )
    result = findings(live, lock, StrictLevel.OFF, frozenset({TableName.of("public", "ignored")}))
    assert result == {
        ("column-removed", "fail", "public.users.email"),
        ("column-changed", "fail", "public.orders.user_id"),
        ("table-changed", "fail", "public.orders"),
        ("table-removed", "fail", "public.gone"),
    }
    report = check_strict(live, lock.catalog, lock.fingerprint, frozenset(), StrictLevel.OFF)
    assert report.failed()
    changed = next(f for f in report.findings if f.difference is Difference.COLUMN_CHANGED)
    assert changed.detail == "locked as integer (not null), now integer (null)"


def test_clean_report() -> None:
    lock = lock_of(USERS)
    report = check_strict(
        catalog(USERS), lock.catalog, lock.fingerprint, frozenset(), StrictLevel.COLUMNS
    )
    assert not report.failed()
    assert report.findings == ()
    assert ColumnName("id") in USERS.primary_key
