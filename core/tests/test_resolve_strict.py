# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

import pytest

from datadouble.catalog import (
    Catalog,
    CatalogLock,
    Dependency,
    ObjectKind,
    ObjectRef,
    Requirement,
    RequirementKind,
    SourceObject,
    Table,
    definition_fingerprint,
)
from datadouble.names import ColumnName, TableName
from datadouble.resolve import ResolveError, Resolver
from datadouble.spec import ObjectLevel, Strict, StrictKind, StrictLevel
from datadouble.strict import Difference, Finding, Severity, check_strict, resolve_excludes
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


def test_names_match_from_the_right() -> None:
    r = Resolver(
        catalog(
            table("users", ["id"], namespace="shop.public"),
            table("orders", ["id"], namespace="shop.public"),
            table("users", ["id"], namespace="billing.public"),
            table("fs.files", ["id"], namespace="media"),
        ),
        case_sensitive=False,
    )
    assert str(r.table("orders").name) == "shop.public.orders"
    assert str(r.table("public.orders").name) == "shop.public.orders"
    assert str(r.table("billing.public.users").name) == "billing.public.users"
    with pytest.raises(
        ResolveError,
        match=r"'public.users' is ambiguous: it matches shop.public.users, billing.public.users",
    ):
        r.table("public.users")
    with pytest.raises(ResolveError, match="not in the catalog"):
        r.table("other.shop.public.users")
    assert str(r.column("shop.public.users.id")) == "shop.public.users.id"
    # A glob's star stays within one part: shop.* is a schema, shop.*.* a database.
    assert [str(n) for n in r.glob("shop.*")] == []
    assert [str(n) for n in r.glob("shop.*.*")] == ["shop.public.users", "shop.public.orders"]
    assert [str(n) for n in r.glob("*.users")] == ["shop.public.users", "billing.public.users"]
    # Names that hold dots still match whole, and by their dotted pieces.
    assert str(r.table("fs.files").name) == "media.fs.files"
    assert [str(n) for n in r.glob("fs.*")] == ["media.fs.files"]
    assert [str(n) for n in r.glob("media.*")] == ["media.fs.files"]


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
    report = check_strict(live, lock, excluded, Strict(data=level))
    assert report.strict.data is level
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


def test_removed_tables_and_columns_and_changed_keys_always_fail() -> None:
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
        ("column-changed", "warn", "public.orders.user_id"),
        ("table-changed", "fail", "public.orders"),
        ("table-removed", "fail", "public.gone"),
    }
    report = check_strict(live, lock, frozenset(), Strict(data=StrictLevel.OFF))
    assert report.failed()
    changed = next(f for f in report.findings if f.difference is Difference.COLUMN_CHANGED)
    assert changed.detail == "locked as integer (not null), now integer (null)"
    assert "re-lock" in changed.resolutions[0]


@pytest.mark.parametrize(
    ("level", "severity"),
    [(StrictLevel.OFF, "warn"), (StrictLevel.TABLES, "warn"), (StrictLevel.COLUMNS, "fail")],
)
def test_changed_columns(level: StrictLevel, severity: str) -> None:
    lock = lock_of(USERS)
    live = catalog(table("users", ["id", "email"], nullable=["email"]))
    assert findings(live, lock, level) == {("column-changed", severity, "public.users.email")}


def test_breaking_differences_fail_at_every_level() -> None:
    lock = lock_of(USERS, table("gone", ["id"]))
    live = catalog(table("users", ["id", "email"], nullable=["email"]))

    def breaks(finding: Finding) -> str | None:
        return f"it breaks {finding.object}"

    report = check_strict(live, lock, frozenset(), Strict(data=StrictLevel.OFF), breaks)
    assert {(f.object, f.severity.value, f.breaks) for f in report.findings} == {
        ("public.users.email", "fail", "it breaks public.users.email"),
        ("public.gone", "fail", None),
    }
    changed = next(f for f in report.findings if f.breaks is not None)
    assert str(changed).endswith("(null); it breaks public.users.email")


def test_clean_report() -> None:
    lock = lock_of(USERS)
    report = check_strict(catalog(USERS), lock, frozenset(), Strict())
    assert not report.failed()
    assert report.findings == ()
    assert ColumnName("id") in USERS.primary_key


def _requiring(*requirements: Requirement) -> Catalog:
    return catalog(USERS).model_copy(update={"requirements": requirements})


@pytest.mark.parametrize(
    ("level", "severity"),
    [(StrictLevel.OFF, "warn"), (StrictLevel.TABLES, "fail"), (StrictLevel.COLUMNS, "fail")],
)
def test_requirement_drift(level: StrictLevel, severity: str) -> None:
    vector = Requirement(kind=RequirementKind.EXTENSION, name="vector", version="0.8.2")
    cube = Requirement(kind=RequirementKind.EXTENSION, name="cube", version="1.5")
    hstore = Requirement(kind=RequirementKind.EXTENSION, name="hstore", version="1.8")
    lock = CatalogLock.of(_requiring(vector, cube))
    live = _requiring(vector.model_copy(update={"version": "0.8.7"}), hstore)
    report = check_strict(live, lock, frozenset(), Strict(data=level))
    assert {(f.difference.value, f.severity.value, f.object) for f in report.findings} == {
        ("requirement-changed", "fail", "extension vector 0.8.7"),
        ("requirement-removed", "fail", "extension cube 1.5"),
        ("new-requirement", severity, "extension hstore 1.8"),
    }
    new = next(f for f in report.findings if f.difference is Difference.NEW_REQUIREMENT)
    assert "sources.<name>.omit" in new.resolutions[1]


def ref(kind: ObjectKind, name: str) -> ObjectRef:
    return ObjectRef(kind=kind, namespace=("public",), name=name)


AUDIT = ref(ObjectKind.FUNCTION, "audit")
TOTAL = ref(ObjectKind.FUNCTION, "total")
TRIGGER = ref(ObjectKind.TRIGGER, "users_audit")
ACTIVE = ref(ObjectKind.VIEW, "active_users")


def item(name: ObjectRef, body: str = "body", *needs: Dependency) -> SourceObject:
    return SourceObject(ref=name, fingerprint=definition_fingerprint(body), depends_on=needs)


def with_objects(*objects: SourceObject) -> Catalog:
    return catalog(USERS).model_copy(update={"objects": objects})


def object_findings(
    live: Catalog, lock: Catalog, strict: Strict, excluded: frozenset[TableName] = frozenset()
) -> set[tuple[str, str, str, str | None]]:
    report = check_strict(live, CatalogLock.of(lock), excluded, strict)
    return {
        (f.difference.value, f.severity.value, f.object, f.breaks)
        for f in report.findings
        if f.kind is not StrictKind.DATA
    }


def test_object_differences_follow_each_kinds_level() -> None:
    lock = with_objects(item(AUDIT), item(TOTAL), item(ACTIVE))
    live = with_objects(item(AUDIT, "changed body"), item(ACTIVE), item(TRIGGER))
    assert object_findings(live, lock, Strict()) == {
        ("object-changed", "warn", "function public.audit", None),
        ("object-removed", "warn", "function public.total", None),
        ("new-object", "warn", "trigger public.users_audit", None),
    }
    assert object_findings(live, lock, Strict(functions=ObjectLevel.FAIL)) == {
        ("object-changed", "fail", "function public.audit", None),
        ("object-removed", "fail", "function public.total", None),
        ("new-object", "warn", "trigger public.users_audit", None),
    }
    quiet = Strict(functions=ObjectLevel.OFF, triggers=ObjectLevel.OFF)
    assert object_findings(live, lock, quiet) == set()
    report = check_strict(live, CatalogLock.of(lock), frozenset(), Strict())
    assert not report.failed()
    changed = next(f for f in report.findings if f.difference is Difference.OBJECT_CHANGED)
    assert changed.ref == AUDIT
    assert "re-lock" in changed.resolutions[0]


def test_removing_an_object_another_needs_fails_at_every_level() -> None:
    users = TableName.of("public", "users")
    orders = TableName.of("public", "orders")
    trigger = item(TRIGGER, "t", Dependency(table=users), Dependency(object=AUDIT))
    lock = with_objects(item(AUDIT), trigger)
    live = with_objects(trigger)
    expected = {
        (
            "object-removed",
            "fail",
            "function public.audit",
            "trigger public.users_audit still needs it",
        )
    }
    for level in ObjectLevel:
        assert object_findings(live, lock, Strict(functions=level)) == expected
    # An unchanged object's locked edges count, though its live ones no longer name it.
    unparsed = with_objects(item(TRIGGER, "t", Dependency(table=users)))
    assert object_findings(unparsed, lock, Strict(functions=ObjectLevel.OFF)) == expected
    # A changed object counts only by its live edges.
    rewritten = with_objects(item(TRIGGER, "new", Dependency(table=users)))
    assert object_findings(rewritten, lock, Strict(functions=ObjectLevel.OFF)) == {
        ("object-changed", "warn", "trigger public.users_audit", None)
    }
    # An object needing an excluded table is left out of copies, so needs nothing.
    calls = item(
        TRIGGER, "t", Dependency(table=users), Dependency(table=orders), Dependency(object=AUDIT)
    )
    assert object_findings(
        with_objects(calls), with_objects(item(AUDIT), calls), Strict(), frozenset({orders})
    ) == {("object-removed", "warn", "function public.audit", None)}
    # One needing only excluded tables is ignored, as they are.
    assert object_findings(live, lock, Strict(), frozenset({users})) == {
        ("object-removed", "warn", "function public.audit", None)
    }
    assert (
        object_findings(with_objects(), with_objects(trigger), Strict(), frozenset({users}))
        == set()
    )


def test_inferred_edges_warn_and_never_fail() -> None:
    users = TableName.of("public", "users")
    trigger = item(TRIGGER, "t", Dependency(table=users), Dependency(object=AUDIT, inferred=True))
    lock = with_objects(item(AUDIT), trigger)
    expected = (
        "object-removed",
        "warn",
        "function public.audit",
        None,
    )
    for level in (ObjectLevel.OFF, ObjectLevel.WARN):
        (found,) = object_findings(with_objects(trigger), lock, Strict(functions=level))
        assert found == expected
    report = check_strict(with_objects(trigger), CatalogLock.of(lock), frozenset(), Strict())
    (finding,) = report.findings
    assert finding.detail.endswith(
        "; trigger public.users_audit may still need it (an inferred dependency)"
    )
    # An inferred edge knows the name, not the signature: an overload left behind still serves.
    overloaded = AUDIT.model_copy(update={"signature": "(integer)"})
    assert (
        object_findings(
            with_objects(item(overloaded), trigger), lock, Strict(functions=ObjectLevel.OFF)
        )
        == set()
    )
    # An inferred edge to an excluded table does not leave the object out.
    hinted = item(TRIGGER, "t", Dependency(table=users, inferred=True), Dependency(object=AUDIT))
    assert object_findings(
        with_objects(hinted), with_objects(item(AUDIT), hinted), Strict(), frozenset({users})
    ) == {
        (
            "object-removed",
            "fail",
            "function public.audit",
            "trigger public.users_audit still needs it",
        )
    }


def test_the_planner_hook_judges_objects_too() -> None:
    lock = with_objects(item(AUDIT))
    live = with_objects(item(AUDIT, "changed"))

    def breaks(finding: Finding) -> str | None:
        return "a packaged table needs it" if finding.ref == AUDIT else None

    report = check_strict(live, CatalogLock.of(lock), frozenset(), Strict(), breaks)
    (finding,) = report.findings
    assert (finding.severity, finding.breaks) == (Severity.FAIL, "a packaged table needs it")
