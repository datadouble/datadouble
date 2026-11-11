# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

import datetime as dt

import pytest

from datadouble.catalog import Catalog, CatalogLock
from datadouble.names import GroupName, TableName
from datadouble.planner import Capabilities, Plan, PlanError, PlanOptions, plan
from datadouble.spec import ImageFormat, Strategy, StrictLevel, parse_spec
from datadouble.subset import EdgeMode
from fakes import catalog, shop, table

CAPABILITIES = Capabilities(
    formats=(ImageFormat.PHYSICAL, ImageFormat.LOGICAL), expressions=True, nested=True
)
NOW = dt.datetime(2026, 11, 11, 12, 30, 15, 999, tzinfo=dt.UTC)
HEADER = "name: shop\nsource: {engine: fake}\n"
CYCLE_FREE = 'exclude: ["store", "staff", "dba_*", "audit.*"]\n'


def planned(
    body: str = "",
    *,
    live: Catalog | None = None,
    lock: Catalog | None = None,
    capabilities: Capabilities = CAPABILITIES,
    options: PlanOptions | None = None,
) -> Plan:
    live = live or shop()
    return plan(
        parse_spec(HEADER + body),
        live,
        CatalogLock.of(lock or live),
        capabilities,
        options or PlanOptions(now=NOW),
    )


def problems(
    body: str = "",
    *,
    capabilities: Capabilities = CAPABILITIES,
    options: PlanOptions | None = None,
) -> tuple[str, ...]:
    with pytest.raises(PlanError) as caught:
        planned(body, capabilities=capabilities, options=options)
    return caught.value.problems


def edge_modes(result: Plan) -> dict[str, tuple[EdgeMode, bool]]:
    return {e.name: (e.mode, e.down) for e in result.edges}


def test_a_typical_plan() -> None:
    result = planned(
        CYCLE_FREE
        + """
subset:
  seeds: {users: "created >= 5"}
  follow: {down: ["users -> posts", "posts -> comments"]}
  edges:
    comments.author_id -> users: { on_missing: null }
    comments.editor_id -> users: { on_missing: null }
    posts.category_id -> categories: { follow: all }
  virtual_fks:
    events.subject_id -> posts: { when: "subject_type = 'Post'" }
anonymize:
  users: {email: {faker: email}, note: {null: {}}, name: {expression: "'x'"}}
"""
    )
    assert result.strategy is Strategy.NESTED
    assert result.as_of == NOW.replace(microsecond=0)
    assert result.formats == (ImageFormat.PHYSICAL, ImageFormat.LOGICAL)
    modes = edge_modes(result)
    assert modes["posts_user_id_fkey"] == (EdgeMode.FOLLOW, True)
    assert modes["comments_author_id_fkey"] == (EdgeMode.NULL, False)
    assert modes["posts_category_id_fkey"] == (EdgeMode.ALL, False)
    assert modes["virtual: events.subject_id -> posts"] == (EdgeMode.FOLLOW, False)
    assert {str(t) for t in result.extracted()} == {
        "public.users",
        "public.categories",
        "public.posts",
        "public.comments",
        "public.events",
        "public.film_actor",
    }
    seeds = {str(rule.table): rule.seed for rule in result.tables if rule.seed}
    assert seeds == {"public.users": "created >= 5"}
    (group,) = result.groups
    assert group.name == "all"
    assert [f.edge.name for f in group.fixups] == [
        "comments_author_id_fkey",
        "comments_editor_id_fkey",
    ]
    assert group.fixups[0].parent_present
    assert [str(r.column) for r in result.anonymization] == [
        "public.users.email",
        "public.users.note",
        "public.users.name",
    ]
    assert result.strict is not None
    assert result.strict.findings == ()
    assert result.graph().rules.keys() == {rule.table for rule in result.tables}


def test_self_references_and_cycles_choose_keys() -> None:
    # Followed children pulling in further parents is a true recursion, too.
    recursive = planned(
        CYCLE_FREE + "subset: {follow: {down: [users -> posts, posts -> comments]}}"
    )
    assert recursive.strategy is Strategy.KEYS
    assert planned().strategy is Strategy.KEYS
    assert planned(options=PlanOptions(now=NOW, strategy=Strategy.KEYS)).strategy is Strategy.KEYS
    reasons = problems(options=PlanOptions(now=NOW, strategy=Strategy.NESTED))
    assert any("cycle" in p and "use keys" in p for p in reasons)


def test_engines_without_nested_queries_use_keys() -> None:
    caps = CAPABILITIES.model_copy(update={"nested": False})
    assert planned(CYCLE_FREE, capabilities=caps).strategy is Strategy.KEYS
    reasons = problems(
        CYCLE_FREE, capabilities=caps, options=PlanOptions(now=NOW, strategy=Strategy.NESTED)
    )
    assert reasons == ("the fake adapter cannot run the nested strategy",)


def test_spec_as_of_wins_and_the_clock_defaults_to_now() -> None:
    fixed = planned("subset: {as_of: 2026-01-01T00:00:00Z}")
    assert fixed.as_of == dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    assert planned(options=PlanOptions()).as_of.tzinfo is not None


def test_no_lock() -> None:
    spec = parse_spec(HEADER + "strict: off")
    result = plan(spec, shop(), None, CAPABILITIES)
    assert result.strict is None
    assert "no catalog lock" in result.warnings[0]
    with pytest.raises(PlanError) as caught:
        plan(parse_spec(HEADER), shop(), None, CAPABILITIES)
    assert "datadouble lock" in caught.value.problems[0]


def test_strict_failures_stop_planning() -> None:
    live = catalog(*shop().tables, table("secrets", ["id"]))
    with pytest.raises(PlanError) as caught:
        planned(live=live, lock=shop())
    assert caught.value.strict is not None
    assert caught.value.problems == (
        (
            "strict mode (columns): new-table public.secrets: "
            "the table is not in the lock and matches no exclude pattern"
        ),
    )
    relaxed = planned("strict: off", live=live, lock=shop(), options=PlanOptions(now=NOW))
    assert any("new-table public.secrets" in w for w in relaxed.warnings)
    excluded = planned('exclude: ["secrets"]', live=live, lock=shop())
    assert TableName.of("public", "secrets") in excluded.excludes.excluded()
    overridden = planned(
        live=live, lock=shop(), options=PlanOptions(now=NOW, strict=StrictLevel.OFF)
    )
    assert overridden.strict is not None
    assert overridden.strict.level is StrictLevel.OFF


def test_excluded_parents_need_fallbacks() -> None:
    reasons = problems('exclude: ["users"]')
    assert any(
        "references public.users (posts_user_id_fkey), which is excluded" in p for p in reasons
    )
    result = planned(
        """
exclude: ["users"]
subset:
  edges:
    posts.user_id -> users: {on_missing: drop}
    comments.author_id -> users: {on_missing: null}
    comments.editor_id -> users: {on_missing: null}
"""
    )
    (group,) = result.groups
    assert {f.edge.name: f.parent_present for f in group.fixups} == {
        "comments_author_id_fkey": False,
        "comments_editor_id_fkey": False,
    }
    assert "posts_user_id_fkey" in group.omitted_references


def test_structure_only_parents_need_fallbacks() -> None:
    reasons = problems('structure_only: ["categories"]')
    assert any(
        "public.categories (posts_category_id_fkey), which keeps no rows" in p for p in reasons
    )
    reasons = problems(
        'structure_only: ["categories"]\n'
        "subset: {edges: {posts.category_id -> categories: {follow: all}}}"
    )
    assert any("keeps no rows" in p for p in reasons)


def test_fallback_checks() -> None:
    reasons = problems(
        """
subset:
  edges:
    posts.user_id -> users: {on_missing: null}
    staff.store_id -> store: {on_missing: stub}
    comments.post_id -> posts: {on_missing: stub}
"""
    )
    assert (
        "posts_user_id_fkey: on_missing null needs nullable columns, but user_id cannot be null"
        in reasons
    )
    assert any(
        "cannot stub public.store, whose references manager_id cannot be null" in p for p in reasons
    )
    assert any(
        "cannot stub public.posts, whose references user_id cannot be null" in p for p in reasons
    )
    stubbed = planned("subset: {edges: {posts.category_id -> categories: {on_missing: stub}}}")
    assert [f.mode for f in stubbed.groups[0].fixups] == [EdgeMode.STUB]
    reasons = problems(
        'structure_only: ["users"]\nsubset: {edges: {posts.user_id -> users: {on_missing: stub}}}'
    )
    assert any("on_missing stub needs public.users in the image, with rows" in p for p in reasons)


def test_reference_names() -> None:
    reasons = problems(
        """
subset:
  edges:
    comments -> users: {on_missing: null}
    users -> posts: {on_missing: null}
    nope -> users: {on_missing: null}
  follow: {down: ["users -> nope", "posts -> users"]}
"""
    )
    assert any("several references to public.users" in p for p in reasons)
    assert "'users -> posts' matches no reference in the catalog or virtual_fks" in reasons
    assert "'posts -> users' matches no reference in the catalog or virtual_fks" in reasons
    assert sum("is not a table or column" in p for p in reasons) == 2


def test_virtual_references() -> None:
    result = planned(
        CYCLE_FREE
        + """
subset:
  virtual_fks:
    events.subject_id -> posts: {}
    events.(id, subject_id) -> film_actor: {}
  follow: {down: ["posts -> events"]}
"""
    )
    virtual = [e for e in result.edges if e.virtual]
    assert [(e.parent_columns, e.down) for e in virtual] == [
        (("id",), True),
        (("film_id", "actor_id"), False),
    ]
    reasons = problems(
        """
subset:
  virtual_fks:
    events -> posts: {}
    events.subject_id -> film_actor: {}
    nope.x -> posts: {}
"""
    )
    assert "virtual reference 'events -> posts' must name the child columns" in reasons
    assert any("has 1 child column(s) and 2 parent column(s)" in p for p in reasons)
    assert any("'nope.x' is not a table" in p or "'nope'" in p for p in reasons)


def test_seeds() -> None:
    reasons = problems(
        """
exclude: ["dba_*"]
structure_only: ["events"]
subset:
  seeds: {dba_maint_runs: "id = 1", events: "id = 1", nope: "id = 1"}
"""
    )
    assert "seed 'dba_maint_runs' names public.dba_maint_runs, which is excluded" in reasons
    assert "seed 'events' names public.events, which is structure-only" in reasons
    assert "table 'nope' is not in the catalog" in reasons


def test_groups() -> None:
    result = planned(
        CYCLE_FREE
        + """
images:
  people: {include: ["users"]}
  content: {include: ["posts", "comments", "categories"], requires: [people]}
subset:
  edges:
    comments.editor_id -> users: {on_missing: null}
"""
    )
    groups = {g.name: g for g in result.groups}
    assert set(groups) == {"people", "content"}
    assert groups[GroupName("content")].requires == ("people",)
    assert set(groups[GroupName("content")].omitted_references) == {
        "posts_user_id_fkey",
        "comments_author_id_fkey",
        "comments_editor_id_fkey",
    }
    assert [(f.edge.name, f.parent_present) for f in groups[GroupName("content")].fixups] == [
        ("comments_editor_id_fkey", False)
    ]
    assert any("public.events is in no image group" in w for w in result.warnings)


def test_groups_without_requires_fail() -> None:
    reasons = problems(
        CYCLE_FREE
        + """
images:
  base: {include: ["users"]}
  people: {include: ["users"], requires: [base]}
  other: {include: ["events"], requires: [base]}
  content: {include: ["posts", "categories", "comments"], requires: [people, other]}
  orphans: {include: ["comments"]}
subset:
  edges:
    comments.editor_id -> users: {on_missing: drop}
"""
    )
    assert any("group 'orphans': public.comments references public.posts" in p for p in reasons)
    assert any(
        "group 'content': comments_editor_id_fkey leaves the group with on_missing drop" in p
        for p in reasons
    )
    assert not any("group 'content': public.posts references" in p for p in reasons)


def test_anonymization_rules() -> None:
    caps = CAPABILITIES.model_copy(update={"expressions": False})
    reasons = problems(
        """
exclude: ["dba_*"]
anonymize:
  dba_maint_runs: {id: {null: {}}}
  nope: {x: {null: {}}}
  users:
    id: {hash: {}}
    email: {expression: "'x'"}
    name: {null: {}}
    created: {faker: first_name}
    nope: {null: {}}
  posts:
    user_id: {format_preserving: {}}
    title: {faker: date_of_birth}
""",
        capabilities=caps,
    )
    assert "anonymization rules name public.dba_maint_runs, which is excluded" in reasons
    assert "table 'nope' is not in the catalog" in reasons
    assert any("public.users.id is part of a primary or foreign key" in p for p in reasons)
    assert "public.users.email: the fake adapter has no expression language" in reasons
    assert "public.users.name cannot be null, so the null rule cannot apply" in reasons
    assert "public.users.created is a integer column; this rule needs text" in reasons
    assert "column 'nope' is not in table public.users" in reasons
    assert any("public.posts.user_id is part of a primary or foreign key" in p for p in reasons)
    assert not any("public.posts.title is a" in p for p in reasons)


def test_formats() -> None:
    assert planned("package: {formats: [logical]}").formats == (ImageFormat.LOGICAL,)
    override = PlanOptions(now=NOW, formats=(ImageFormat.PHYSICAL, ImageFormat.PHYSICAL))
    assert planned(options=override).formats == (ImageFormat.PHYSICAL,)
    caps = CAPABILITIES.model_copy(update={"formats": (ImageFormat.LOGICAL,)})
    assert planned(capabilities=caps).formats == (ImageFormat.LOGICAL,)
    reasons = problems("package: {formats: [physical]}", capabilities=caps)
    assert reasons == ("the fake adapter cannot produce physical images; it supports logical",)


def test_hook_rewrites() -> None:
    result = planned(
        """
hooks:
  after_transform:
    - {sql: "UPDATE users SET note = NULL", rewrites: [users.note]}
"""
    )
    assert [str(c) for c in result.rewrites] == ["public.users.note"]
    assert problems("hooks: {after_transform: [{sql: x, rewrites: [users.nope]}]}") == (
        "column 'nope' is not in table public.users",
    )


def test_verify_settings() -> None:
    result = planned(
        """
verify:
  pii_scan: {allow: {users.email: ["%@company.com"]}}
  assertions: [{name: none, sql: "SELECT 1 WHERE false"}]
exclude: ["dba_*"]
"""
    )
    assert [(str(a.column), a.patterns) for a in result.pii_allow] == [
        ("public.users.email", ("%@company.com",))
    ]
    assert [a.name for a in result.assertions] == ["none"]
    assert TableName.of("public", "dba_maint_runs") not in result.catalog.names()
    assert problems("verify: {pii_scan: {allow: {users.nope: []}}}") == (
        "column 'nope' is not in table public.users",
    )


def test_plan_error_message() -> None:
    error = PlanError(("one",))
    assert str(error) == "the plan has 1 problem"
    assert str(PlanError(("a", "b"))) == "the plan has 2 problems"
