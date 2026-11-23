# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

import pytest
from test_planner import CAPABILITIES, CYCLE_FREE, planned

from datadouble.catalog import (
    Catalog,
    Dependency,
    ObjectKind,
    ObjectRef,
    SourceObject,
    definition_fingerprint,
)
from datadouble.closure import Pulled, ordered, pulled_rows
from datadouble.names import ColumnName, GroupName, TableName
from datadouble.planner import PlanError, PlannedGroup
from datadouble.spec import PulledRows
from datadouble.subset import AllRows, Edge, EdgeMode, ValuesIn
from fakes import FakeSource, shop

T = TableName.of
USERS, POSTS, LOG = T("public", "users"), T("public", "posts"), T("audit", "log")
STAMP = ObjectRef(kind=ObjectKind.FUNCTION, namespace=("public",), name="stamp", signature="()")
USERS_STAMP = ObjectRef(kind=ObjectKind.TRIGGER, namespace=("public",), name="users.users_stamp")
WRITE_LOG = ObjectRef(kind=ObjectKind.FUNCTION, namespace=("audit",), name="write_log")
POSTS_LOG = ObjectRef(kind=ObjectKind.TRIGGER, namespace=("public",), name="posts.posts_log")
POSTS_SEQ = ObjectRef(kind=ObjectKind.SEQUENCE, namespace=("public",), name="posts_id_seq")
TITLES = ObjectRef(kind=ObjectKind.VIEW, namespace=("public",), name="post_titles")
UUID = ObjectRef(kind=ObjectKind.FUNCTION, namespace=("public",), name="uuid", signature="()")
KEEP_AUDIT = 'exclude: ["store", "staff", "dba_*"]\n'


def item(ref: ObjectRef, *edges: Dependency) -> SourceObject:
    return SourceObject(ref=ref, fingerprint=definition_fingerprint(str(ref)), depends_on=edges)


def on(table: TableName, *, inferred: bool = False) -> Dependency:
    return Dependency(table=table, inferred=inferred)


def calls(ref: ObjectRef, *, inferred: bool = False) -> Dependency:
    return Dependency(object=ref, inferred=inferred)


def objects(*, log_inferred: bool = False, extra: tuple[SourceObject, ...] = ()) -> Catalog:
    """The shop with triggers, a sequence, a view and a function code expects."""
    base = shop()
    tables = tuple(
        table.model_copy(update={"depends_on": (calls(POSTS_SEQ),)})
        if table.name == POSTS
        else table
        for table in base.tables
    )
    return base.model_copy(
        update={
            "tables": tables,
            "objects": (
                item(STAMP),
                item(USERS_STAMP, on(USERS), calls(STAMP)),
                item(WRITE_LOG, on(LOG, inferred=log_inferred)),
                item(POSTS_LOG, on(POSTS), calls(WRITE_LOG)),
                item(POSTS_SEQ),
                item(TITLES, on(POSTS)),
                item(UUID),
                *extra,
            ),
        }
    )


def group(name: str, body: str, live: Catalog) -> PlannedGroup:
    result = planned(body, live=live)
    return next(g for g in result.groups if g.name == GroupName(name))


def pulled(found: PlannedGroup) -> dict[str, Pulled]:
    return {str(p.table or p.object): p for p in found.pulled}


STANDALONE = """
images:
  people:
    include: ["users"]
  content:
    include: ["posts", "comments"]
    standalone: true
    objects:
      - function: uuid
subset:
  edges:
    comments.editor_id -> users:
      on_missing: null
"""


def test_a_standalone_group_pulls_in_what_it_needs() -> None:
    content = group("content", KEEP_AUDIT + STANDALONE, objects())
    assert content.standalone
    assert content.roots == (UUID,)
    found = pulled(content)
    # Parents of its rows, with the rows they reference; no `requires` needed.
    assert found["public.users"].rows is PulledRows.REFERENCED
    assert found["public.users"].path == ("table public.comments", "table public.users")
    assert found["public.categories"].rows is PulledRows.REFERENCED
    # What its tables and triggers need, transitively and across schemas.
    assert set(found) == {
        "public.users",
        "public.categories",
        "audit.log",
        "sequence public.posts_id_seq",
        "trigger public.users.users_stamp",
        "function public.stamp()",
        "function audit.write_log",
    }
    assert found["function audit.write_log"].path == (
        "table public.posts",
        "trigger public.posts.posts_log",
        "function audit.write_log",
    )
    assert str(found["audit.log"]) == (
        "table audit.log (rows: none), reached by table public.posts -> "
        "trigger public.posts.posts_log -> function audit.write_log -> table audit.log"
    )
    # The image's objects, each after what it needs; a view over its own table comes along.
    order = list(content.objects)
    assert {str(ref) for ref in order} == {
        str(ref) for ref in (STAMP, USERS_STAMP, WRITE_LOG, POSTS_LOG, POSTS_SEQ, TITLES, UUID)
    }
    assert order.index(STAMP) < order.index(USERS_STAMP)
    assert order.index(WRITE_LOG) < order.index(POSTS_LOG)
    # A reference whose parent is pulled in is kept, and its fallback sees the parent.
    assert "comments_editor_id_fkey" not in content.omitted_references
    assert [(f.edge.name, f.parent_present) for f in content.fixups] == [
        ("comments_editor_id_fkey", True)
    ]


def test_other_groups_pull_nothing_in() -> None:
    people = group("people", KEEP_AUDIT + STANDALONE, objects())
    assert not people.standalone
    assert people.pulled == ()
    assert {str(ref) for ref in people.objects} == {str(STAMP), str(USERS_STAMP)}


def test_objects_another_group_includes_need_requires() -> None:
    body = KEEP_AUDIT + "images: {main: {include: ['public.*']}, logs: {include: ['audit.*']}}\n"
    with pytest.raises(PlanError) as caught:
        planned(body, live=objects())
    assert caught.value.problems == (
        (
            "group 'main': function audit.write_log is in group 'logs'; add `requires:` for "
            "it, or make this group standalone; reached by table public.posts -> "
            "trigger public.posts.posts_log -> function audit.write_log"
        ),
    )
    body = body.replace("['public.*']}", "['public.*'], requires: [logs]}")
    main = group("main", body, objects())
    assert WRITE_LOG in main.objects
    assert main.pulled == ()
    assert LOG not in main.packaged()


def test_a_standalone_group_holds_the_objects_it_includes() -> None:
    body = KEEP_AUDIT + "images: {logs: {include: ['audit.*'], standalone: true}}\n"
    logs = group("logs", body, objects())
    assert WRITE_LOG in logs.objects
    assert pulled(logs) == {}


def test_rows_overrides() -> None:
    def rows(overrides: str) -> dict[str, PulledRows | None]:
        body = KEEP_AUDIT + STANDALONE.replace(
            "standalone: true", f"standalone: true\n    rows: {overrides}"
        )
        return {key: p.rows for key, p in pulled(group("content", body, objects())).items()}

    found = rows("{audit.log: referenced, 'public.c*': all}")
    assert found["audit.log"] is PulledRows.REFERENCED
    assert found["public.categories"] is PulledRows.ALL
    assert found["public.users"] is PulledRows.REFERENCED
    # A reference key wins over a table key.
    found = rows("{users: none, 'posts.user_id -> users': all}")
    assert found["public.users"] is PulledRows.ALL
    with pytest.raises(PlanError) as caught:
        planned(
            KEEP_AUDIT
            + STANDALONE.replace(
                "standalone: true",
                "standalone: true\n    rows: {'posts.user_id -> users': all,"
                " 'comments.author_id -> users': none, events: all, 'nope*': all,"
                " nope: all, 'nope -> users': all}",
            ),
            live=objects(),
        )
    assert set(caught.value.problems) == {
        "group 'content': rows: 'nope*' matches no table",
        (
            "group 'content': rows 'posts.user_id -> users', 'comments.author_id -> users' "
            "disagree about table public.users"
        ),
        "group 'content': rows: 'events' matches no table its closure pulls in",
        "table 'nope' is not in the catalog",
        "'nope' is not a table or column in the catalog",
    }


def test_structure_only_tables_take_no_rows() -> None:
    with pytest.raises(PlanError) as caught:
        planned(
            KEEP_AUDIT
            + 'structure_only: ["comments"]\n'
            + "images: {people: {include: [users], standalone: true, rows: {comments: all}}}\n"
            + "subset: {follow: {down: ['users -> posts', 'posts -> comments']}}\n",
            live=shop(),
        )
    assert caught.value.problems == (
        "group 'people': rows asks for rows of table public.comments, which is structure-only",
    )


def test_engines_that_cannot_build_standalone_images() -> None:
    limited = CAPABILITIES.model_copy(update={"standalone": False})
    with pytest.raises(PlanError) as caught:
        planned(KEEP_AUDIT + STANDALONE, live=objects(), capabilities=limited)
    assert caught.value.problems == (
        "group 'content' is standalone, and the fake adapter cannot build standalone images yet",
    )
    # Nothing to pull in and no objects: a standalone group is an ordinary one.
    alone = "images: {all: {include: ['*'], standalone: true}}\n"
    assert planned(CYCLE_FREE + alone, capabilities=limited).groups[0].standalone


def test_references_to_tables_without_rows_are_left_out() -> None:
    content = group(
        "content",
        KEEP_AUDIT
        + STANDALONE.replace("standalone: true", "standalone: true\n    rows: {users: none}"),
        objects(),
    )
    assert content.rowless() == {USERS, LOG}
    assert {"posts_user_id_fkey", "comments_author_id_fkey"} <= set(content.omitted_references)
    assert content.rows()[USERS] is PulledRows.NONE


def test_followed_children_and_structure_only_tables() -> None:
    body = (
        KEEP_AUDIT
        + 'structure_only: ["comments"]\n'
        + """
images:
  people:
    include: ["users"]
    standalone: true
subset:
  follow:
    down: ["users -> posts", "posts -> comments"]
"""
    )
    found = pulled(group("people", body, shop()))
    assert found["public.posts"].path == ("table public.users", "table public.posts")
    assert found["public.categories"].rows is PulledRows.REFERENCED
    assert found["public.comments"].rows is PulledRows.NONE


def test_what_an_image_cannot_have_fails_naming_the_path() -> None:
    body = CYCLE_FREE + "images: {content: {include: [posts, comments, users, categories]}}\n"
    with pytest.raises(PlanError) as caught:
        planned(body, live=objects())
    assert caught.value.problems == (
        (
            "group 'content': function audit.write_log needs table audit.log, which is excluded, "
            "so copies leave it out; reached by table public.posts -> "
            "trigger public.posts.posts_log -> function audit.write_log"
        ),
    )
    # Through an inferred edge it only warns.
    result = planned(body, live=objects(log_inferred=True))
    assert any(
        w.startswith("group 'content': function audit.write_log may need table audit.log")
        and w.endswith("(an inferred dependency: check it)")
        for w in result.warnings
    )


def test_pulled_in_items_are_a_table_or_an_object() -> None:
    with pytest.raises(ValueError, match="either a table or an object"):
        Pulled(path=())


def test_inferred_routines_pulled_in_warn() -> None:
    result = planned(KEEP_AUDIT + STANDALONE, live=objects(log_inferred=True))
    assert (
        "group 'content': function audit.write_log's dependencies are inferred from its body; "
        "check that the image has what it uses, or declare that in objects"
    ) in result.warnings
    found = pulled(next(g for g in result.groups if g.name == GroupName("content")))
    assert found["audit.log"].inferred


def test_dependencies_outside_the_catalog_fail() -> None:
    ghost = ObjectRef(kind=ObjectKind.FUNCTION, namespace=("other",), name="ghost")
    gone = T("other", "gone")
    haunted = ObjectRef(kind=ObjectKind.TRIGGER, namespace=("public",), name="users.haunted")
    live = objects(extra=(item(haunted, on(USERS), calls(ghost), on(gone)),))
    with pytest.raises(PlanError) as caught:
        planned(KEEP_AUDIT + "objects: [{trigger: users.haunted}]\n", live=live)
    assert set(caught.value.problems) == {
        (
            "group 'all': function other.ghost is not in the catalog; reached by "
            "trigger public.users.haunted -> function other.ghost"
        ),
        (
            "group 'all': trigger public.users.haunted needs table other.gone, which is not in "
            "the catalog; reached by trigger public.users.haunted"
        ),
    }


def test_roots_resolve_by_kind_and_name() -> None:
    reasons: tuple[str, ...]
    with pytest.raises(PlanError) as caught:
        planned(
            KEEP_AUDIT
            + "objects: [{function: nope}]\n"
            + "images: {all: {include: ['*'], objects: [{view: nope}]}}\n",
            live=objects(),
        )
    reasons = caught.value.problems
    assert reasons == (
        "the spec: objects: function 'nope' is not in the catalog",
        "group 'all': objects: view 'nope' is not in the catalog",
    )
    result = planned(
        KEEP_AUDIT + "objects: [{trigger: users.users_stamp}, {function: 'stamp ( )'}]\n",
        live=objects(),
    )
    assert result.groups[0].roots == (USERS_STAMP, STAMP)
    twice = ObjectRef(kind=ObjectKind.FUNCTION, namespace=("audit",), name="stamp")
    with pytest.raises(PlanError) as caught:
        planned(KEEP_AUDIT + "objects: [{function: stamp}]\n", live=objects(extra=(item(twice),)))
    assert caught.value.problems == (
        (
            "the spec: objects: function 'stamp' is ambiguous: it matches function "
            "public.stamp(), function audit.stamp; qualify it"
        ),
    )


def _lock_without(*refs: ObjectRef) -> Catalog:
    live = objects()
    return live.model_copy(update={"objects": tuple(o for o in live.objects if o.ref not in refs)})


def test_removing_what_a_packaged_table_or_root_needs_fails() -> None:
    live = _lock_without(POSTS_SEQ)
    live = live.model_copy(
        update={"tables": tuple(t.model_copy(update={"depends_on": ()}) for t in live.tables)}
    )
    with pytest.raises(PlanError) as caught:
        planned(KEEP_AUDIT, live=live, lock=objects())
    assert caught.value.problems == (
        (
            "strict mode: object-removed sequence public.posts_id_seq: the object is in the lock "
            "but not in the live catalog; packaged table public.posts still needs it"
        ),
    )
    with pytest.raises(PlanError) as caught:
        planned(
            KEEP_AUDIT + "images: {web: {include: ['*'], objects: [{function: uuid}]}}\n",
            live=_lock_without(UUID),
            lock=objects(),
        )
    assert caught.value.problems[0].endswith("group 'web' declares it in objects")
    with pytest.raises(PlanError) as caught:
        planned(
            KEEP_AUDIT + "objects: [{view: nope}, {function: uuid}]\n",
            live=_lock_without(POSTS_SEQ),
            lock=objects(),
        )
    assert caught.value.problems[0].startswith("strict mode: object-removed sequence")
    # Nothing needs the view: removing it warns.
    result = planned(KEEP_AUDIT, live=_lock_without(TITLES), lock=objects())
    assert any(w.startswith("strict mode: object-removed view") for w in result.warnings)


def test_new_or_changed_objects_that_need_what_an_image_cannot_have_fail() -> None:
    with pytest.raises(PlanError) as caught:
        planned(CYCLE_FREE, live=objects(), lock=_lock_without(POSTS_LOG))
    assert caught.value.problems == (
        (
            "strict mode: new-object trigger public.posts.posts_log: the object is not in the "
            "lock; group 'all': function audit.write_log needs table audit.log, which is excluded, "
            "so copies leave it out; reached by table public.posts -> "
            "trigger public.posts.posts_log -> function audit.write_log"
        ),
    )

    # A changed function that now reads an excluded table, as well as a kept one.
    def with_log(*edges: Dependency) -> Catalog:
        base = objects()
        replaced = item(WRITE_LOG, *edges).model_copy(
            update={"fingerprint": definition_fingerprint(str(edges))}
        )
        return base.model_copy(
            update={"objects": tuple(replaced if o.ref == WRITE_LOG else o for o in base.objects)}
        )

    with pytest.raises(PlanError) as caught:
        planned(CYCLE_FREE, live=with_log(on(LOG), on(USERS)), lock=with_log(on(USERS)))
    assert caught.value.problems[0].startswith(
        "strict mode: object-changed function audit.write_log: its definition differs from the "
        "lock; group 'all': function audit.write_log needs table audit.log"
    )


def test_pulled_rows_follow_references_both_ways() -> None:
    source = FakeSource(
        rows={
            POSTS: [
                {"id": 10, "user_id": 1, "category_id": 1},
                {"id": 11, "user_id": 2, "category_id": 2},
                {"id": 12, "user_id": 4, "category_id": None},
            ],
            USERS: [{"id": i} for i in range(1, 6)],
            T("public", "comments"): [
                {"id": 100, "post_id": 10, "author_id": 3},
                {"id": 101, "post_id": 10, "author_id": 5},
                {"id": 102, "post_id": 11, "author_id": None},
            ],
            T("public", "categories"): [{"id": 1}, {"id": 2}],
        }
    )
    comments, categories = T("public", "comments"), T("public", "categories")
    up = Edge("posts_user", POSTS, (ColumnName("user_id"),), USERS, (ColumnName("id"),))
    down = Edge(
        "comments_post",
        comments,
        (ColumnName("post_id"),),
        POSTS,
        (ColumnName("id"),),
        down=True,
        when="author_id != 5",
    )
    authors = Edge(
        "comments_author", comments, (ColumnName("author_id"),), USERS, (ColumnName("id"),)
    )
    every = Edge(
        "posts_category",
        POSTS,
        (ColumnName("category_id"),),
        categories,
        (ColumnName("id"),),
        mode=EdgeMode.ALL,
    )
    first = ValuesIn((ColumnName("id"),), frozenset({(10,)}))
    elsewhere = Edge(
        "staff_user", T("public", "staff"), (ColumnName("id"),), USERS, (ColumnName("id"),)
    )
    found = pulled_rows(
        (up, down, authors, every, elsewhere),
        frozenset({POSTS}),
        {
            USERS: PulledRows.REFERENCED,
            comments: PulledRows.REFERENCED,
            categories: PulledRows.REFERENCED,
            T("public", "events"): PulledRows.NONE,
        },
        {POSTS: first},
        source,
    )
    # Posts the comments reference are read again with the group's own rows.
    assert set(found) == {USERS, comments, categories, POSTS}
    assert [r["id"] for r in source.select(POSTS, found)] == [10]
    assert sorted(str(r["id"]) for r in source.select(USERS, found)) == ["1", "3"]
    assert [r["id"] for r in source.select(comments, found)] == [100]
    assert isinstance(found[categories], AllRows)
    # An own table gains the rows that every row of a pulled-in table references.
    grown = pulled_rows(
        (up,),
        frozenset({USERS}),
        {POSTS: PulledRows.ALL},
        {USERS: ValuesIn((ColumnName("id"),), frozenset({(5,)}))},
        source,
    )
    assert sorted(str(r["id"]) for r in source.select(USERS, grown)) == ["1", "2", "4", "5"]


def test_objects_in_dependency_order() -> None:
    catalog = objects().objects
    found = ordered([POSTS_LOG, WRITE_LOG, USERS_STAMP, STAMP], catalog)
    assert found == (WRITE_LOG, STAMP, POSTS_LOG, USERS_STAMP)
