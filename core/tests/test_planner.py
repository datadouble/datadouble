# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

import datetime as dt

import pytest

from datadouble.catalog import (
    Catalog,
    CatalogLock,
    ColumnKind,
    Dependency,
    ObjectKind,
    ObjectRef,
    Requirement,
    RequirementKind,
    SourceObject,
    definition_fingerprint,
)
from datadouble.names import ColumnName, GroupName, TableName
from datadouble.planner import AccountNames, Capabilities, Plan, PlanError, PlanOptions, plan
from datadouble.spec import (
    ImageFormat,
    ObjectLevel,
    Spec,
    Strategy,
    StrictLevel,
    StrictOverrides,
    Unselected,
    parse_spec,
)
from datadouble.subset import AllRows, EdgeMode, NoRows, nested_conditions
from fakes import catalog, shop, table

CAPABILITIES = Capabilities(
    formats=(ImageFormat.PHYSICAL, ImageFormat.LOGICAL),
    expressions=True,
    nested=True,
    standalone=True,
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
        quiet(parse_spec(HEADER + body)),
        live,
        CatalogLock.of(lock or live),
        capabilities,
        options or PlanOptions(now=NOW),
    )


def quiet(spec: Spec) -> Spec:
    """Most tests here seed only part of the shop: let the rest keep no rows.

    Tests of ``subset.unselected`` itself call :func:`plan` directly.
    """
    if spec.subset.unselected is not None or spec.subset.jailer is not None:
        return spec
    subset = spec.subset.model_copy(update={"unselected": Unselected.NONE})
    return spec.model_copy(update={"subset": subset})


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
  seeds:
    users: "created >= 5"
  follow:
    down: ["users -> posts", "posts -> comments"]
  edges:
    comments.author_id -> users:
      on_missing: null
    comments.editor_id -> users:
      on_missing: null
    posts.category_id -> categories:
      follow: all
  virtual_fks:
    events.subject_id -> posts:
      when: "subject_type = 'Post'"
anonymize:
  users:
    email:
      faker: email
    note:
      null: {}
    name:
      expression: "'x'"
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
    spec = quiet(parse_spec(HEADER + "strict: {data: off}"))
    result = plan(spec, shop(), None, CAPABILITIES)
    assert result.strict is None
    assert "no catalog lock" in result.warnings[0]
    with pytest.raises(PlanError) as caught:
        plan(quiet(parse_spec(HEADER)), shop(), None, CAPABILITIES)
    assert "datadouble lock" in caught.value.problems[0]


def test_strict_failures_stop_planning() -> None:
    live = catalog(*shop().tables, table("secrets", ["id"]))
    with pytest.raises(PlanError) as caught:
        planned(live=live, lock=shop())
    assert caught.value.strict is not None
    assert caught.value.problems == (
        (
            "strict mode: new-table public.secrets: "
            "the table is not in the lock and matches no exclude pattern"
        ),
    )
    relaxed = planned("strict: {data: off}", live=live, lock=shop(), options=PlanOptions(now=NOW))
    assert any("new-table public.secrets" in w for w in relaxed.warnings)
    excluded = planned('exclude: ["secrets"]', live=live, lock=shop())
    assert TableName.of("public", "secrets") in excluded.excludes.excluded()
    overridden = planned(
        live=live,
        lock=shop(),
        options=PlanOptions(now=NOW, strict=StrictOverrides(data=StrictLevel.OFF)),
    )
    assert overridden.strict is not None
    assert overridden.strict.strict.data is StrictLevel.OFF
    assert overridden.strict.strict.functions is ObjectLevel.WARN


def _with_column(name: str, **changes: object) -> Catalog:
    """The shop with one column of ``users`` changed."""
    live = shop()
    users = live.tables[0]
    columns = tuple(
        column.model_copy(update=changes) if column.name == name else column
        for column in users.columns
    )
    return live.model_copy(
        update={"tables": (users.model_copy(update={"columns": columns}), *live.tables[1:])}
    )


def test_changed_columns_fail_only_at_data_columns() -> None:
    widened = _with_column("note", type="varchar(400)")
    tables = planned(
        "strict: {data: tables}", live=widened, lock=shop(), options=PlanOptions(now=NOW)
    )
    assert any("column-changed public.users.note" in w for w in tables.warnings)
    off = planned(
        live=widened,
        lock=shop(),
        options=PlanOptions(now=NOW, strict=StrictOverrides(data=StrictLevel.OFF)),
    )
    assert off.strict is not None
    assert {f.severity.value for f in off.strict.findings} == {"warn"}
    with pytest.raises(PlanError) as unknown:
        planned(
            "strict: {data: tables}\nanonymize: {nope: {x: {faker: name}}}",
            live=widened,
            lock=shop(),
        )
    assert unknown.value.problems == ("table 'nope' is not in the catalog",)
    with pytest.raises(PlanError) as caught:
        planned(live=widened, lock=shop())
    assert caught.value.problems == (
        (
            "strict mode: column-changed public.users.note: locked as text (null), "
            "now varchar(400) (null)"
        ),
    )


@pytest.mark.parametrize("level", ["off", "tables", "columns"])
def test_changed_columns_that_break_their_rule_fail_at_every_level(level: str) -> None:
    rules = "anonymize: {users: {email: {faker: email}, note: {null: {}}}}"
    body = f"strict: {{data: {level}}}\n{rules}"
    for column, changes, why in (
        ("email", {"kind": ColumnKind.INTEGER, "type": "integer"}, "this rule needs text"),
        ("note", {"nullable": False}, "the null rule cannot apply"),
    ):
        with pytest.raises(PlanError) as caught:
            planned(body, live=_with_column(column, **changes), lock=shop())
        assert caught.value.strict is not None
        (finding,) = caught.value.strict.findings
        assert finding.object == f"public.users.{column}"
        assert finding.breaks is not None
        assert why in finding.breaks
        assert caught.value.problems[0].startswith(
            f"strict mode: column-changed public.users.{column}"
        )


def _objects(*objects: SourceObject) -> Catalog:
    return shop().model_copy(update={"objects": objects})


def test_object_changes_warn_and_removing_what_a_trigger_calls_fails() -> None:
    function = ObjectRef(kind=ObjectKind.FUNCTION, namespace=("public",), name="stamp")
    trigger = ObjectRef(kind=ObjectKind.TRIGGER, namespace=("public",), name="users_stamp")
    calls = SourceObject(
        ref=trigger,
        fingerprint=definition_fingerprint("EXECUTE FUNCTION stamp()"),
        depends_on=(
            Dependency(table=TableName.of("public", "users")),
            Dependency(object=function),
        ),
    )
    old = SourceObject(ref=function, fingerprint=definition_fingerprint("old"))
    new = old.model_copy(update={"fingerprint": definition_fingerprint("new")})
    rewritten = planned(live=_objects(new, calls), lock=_objects(old, calls))
    assert [w for w in rewritten.warnings if w.startswith("strict mode")] == [
        "strict mode: object-changed function public.stamp: its definition differs from the lock"
    ]
    hinted = calls.model_copy(update={"depends_on": (Dependency(object=function, inferred=True),)})
    assert planned(live=_objects(new, hinted)).catalog.objects[1].depends_on[0].inferred
    with pytest.raises(PlanError) as caught:
        planned("strict: {functions: fail}", live=_objects(new, calls), lock=_objects(old, calls))
    assert caught.value.problems[0].startswith("strict mode: object-changed function public.stamp")
    with pytest.raises(PlanError) as caught:
        planned(live=_objects(calls), lock=_objects(old, calls))
    assert caught.value.problems == (
        (
            "strict mode: object-removed function public.stamp: the object is in the lock but "
            "not in the live catalog; trigger public.users_stamp still needs it"
        ),
    )


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
    posts.user_id -> users:
      on_missing: drop
    comments.author_id -> users:
      on_missing: null
    comments.editor_id -> users:
      on_missing: null
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
    posts.user_id -> users:
      on_missing: null
    staff.store_id -> store:
      on_missing: stub
    comments.post_id -> posts:
      on_missing: stub
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
    comments -> users:
      on_missing: null
    users -> posts:
      on_missing: null
    nope -> users:
      on_missing: null
  follow:
    down: ["users -> nope", "posts -> users"]
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
    events.id -> users.created: {}
  follow:
    down: ["posts -> events"]
"""
    )
    virtual = [e for e in result.edges if e.virtual]
    assert [(e.parent_columns, e.down, e.parent_unique) for e in virtual] == [
        (("id",), True, True),
        (("film_id", "actor_id"), False, True),
        (("created",), False, False),
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


def test_virtual_references_between_unjoinable_namespaces_fail() -> None:
    caps = CAPABILITIES.model_copy(update={"joins_outermost": False})
    body = CYCLE_FREE.replace(', "audit.*"', "") + "subset:\n  virtual_fks:\n"
    across = body + "    audit.log.user_id -> public.users: {}\n"
    with pytest.raises(PlanError) as caught:
        planned(across, capabilities=caps)
    assert caught.value.problems == (
        (
            "virtual reference 'audit.log.user_id -> public.users' joins audit and public,"
            " which one fake query cannot read together"
        ),
    )
    within = body + "    events.subject_id -> posts: {}\n"
    assert any(e.virtual for e in planned(within, capabilities=caps).edges)
    assert any(e.virtual for e in planned(across).edges)


def test_drop_needs_an_unconditional_reference() -> None:
    reasons = problems(
        """
subset:
  virtual_fks:
    events.subject_id -> posts:
      when: "subject_type = 'Post'"
  edges:
    events.subject_id -> posts:
      on_missing: drop
"""
    )
    assert any("on_missing drop is not supported on a reference with `when`" in p for p in reasons)


def test_seeds() -> None:
    reasons = problems(
        """
exclude: ["dba_*"]
structure_only: ["events"]
subset:
  seeds:
    dba_maint_runs: "id = 1"
    events: "id = 1"
    nope: "id = 1"
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
  people:
    include: ["users"]
  content:
    include: ["posts", "comments", "categories"]
    requires: [people]
subset:
  edges:
    comments.editor_id -> users:
      on_missing: null
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
  base:
    include: ["users"]
  people:
    include: ["users"]
    requires: [base]
  other:
    include: ["events"]
    requires: [base]
  content:
    include: ["posts", "categories", "comments"]
    requires: [people, other]
  orphans:
    include: ["comments"]
subset:
  edges:
    comments.editor_id -> users:
      on_missing: drop
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
  dba_maint_runs:
    id:
      null: {}
  nope:
    x:
      null: {}
  users:
    id:
      hash: {}
    email:
      expression: "'x'"
    name:
      null: {}
    created:
      faker: first_name
    nope:
      null: {}
  posts:
    user_id:
      format_preserving: {}
    title:
      faker: date_of_birth
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
    - sql: "UPDATE users SET note = NULL"
      rewrites: [users.note]
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
  pii_scan:
    allow:
      users.email: ["%@company.com"]
  assertions:
    - name: none
      sql: "SELECT 1 WHERE false"
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


def test_engines_without_hooks() -> None:
    caps = CAPABILITIES.model_copy(update={"hooks": False})
    reasons = problems("hooks: {before_load: [{sql: x}]}", capabilities=caps)
    assert reasons == ("the fake adapter does not run hooks",)
    assert planned(capabilities=caps).spec == "shop"


def test_complete_engines_need_every_table_selected_or_excluded() -> None:
    caps = CAPABILITIES.model_copy(update={"complete": True})
    tree = catalog(table("avatars", ["path"], key=("path",)), table("tmp", ["path"], key=("path",)))
    with pytest.raises(PlanError) as caught:
        plan(
            parse_spec(HEADER + "strict: {data: off}\nsubset: {seeds: {avatars: '-type f'}}"),
            tree,
            None,
            caps,
            PlanOptions(now=NOW),
        )
    assert caught.value.problems == ("public.tmp is neither selected nor excluded",)
    chosen = plan(
        parse_spec(
            HEADER + "strict: {data: off}\nexclude: [tmp]\nsubset: {seeds: {avatars: '-type f'}}"
        ),
        tree,
        None,
        caps,
        PlanOptions(now=NOW),
    )
    assert chosen.extracted() == (TableName.of("public", "avatars"),)


def test_paths_from_names_a_kept_column() -> None:
    files = "files: {paths_from: users.note}\n"
    found = planned(files).paths_from
    assert found is not None
    assert (found.table, found.columns, found.template) == (
        TableName.of("public", "users"),
        (ColumnName("note"),),
        "{note}",
    )
    assert problems(files + 'exclude: ["users", "posts", "comments"]\n') == (
        "files.paths_from names public.users, which is excluded",
    )


def test_paths_from_templates_and_patterns() -> None:
    template = (
        "files: {paths_from: {table: users, path: 'u/{ID}/{name}.{id}', match: '^u/(.+)$'}}\n"
    )
    found = planned(template).paths_from
    assert found is not None
    assert found.columns == (ColumnName("id"), ColumnName("name"))
    assert found.template == "u/{id}/{name}.{id}"
    assert found.path((7, "Ann")) == "7/Ann.7"
    assert found.path((7, None)) is None
    unmatched = found.model_copy(update={"match": "^x/(.+)$"})
    assert unmatched.path((7, "Ann")) is None
    assert found.model_copy(update={"match": None}).path((7, "Ann")) == "u/7/Ann.7"

    def wrong(paths_from: str) -> tuple[str, ...]:
        return problems(f"files: {{paths_from: {paths_from}}}\n")

    assert wrong("{table: users, path: plain}") == (
        "files.paths_from.path names no column, as {column}",
    )
    assert wrong("{table: users, path: '{nope}'}") == (
        "column 'nope' is not in table public.users",
        "files.paths_from.path names no column, as {column}",
    )
    assert wrong("{table: users, path: '{id}', match: '('}")[0].startswith(
        "files.paths_from.match is not a regular expression"
    )
    assert wrong("{table: users, path: '{id}', match: 'x'}") == (
        "files.paths_from.match needs a group for the path",
    )
    assert wrong("{table: nope, path: '{id}'}") == ("table 'nope' is not in the catalog",)
    assert wrong("users.nope") == ("column 'nope' is not in table public.users",)


def test_plan_error_message() -> None:
    error = PlanError(("one",))
    assert str(error) == "the plan has 1 problem"
    assert str(PlanError(("a", "b"))) == "the plan has 2 problems"


def test_omitted_requirements() -> None:
    vector = Requirement(kind=RequirementKind.EXTENSION, name="vector", version="0.8.2")
    cube = Requirement(kind=RequirementKind.EXTENSION, name="cube", version="1.5")
    live = shop().model_copy(update={"requirements": (vector, cube)})

    def plan_omitting(names: str) -> Plan:
        spec = quiet(
            parse_spec(f"name: shop\nsource: {{engine: fake, omit: [{names}]}}\n" + CYCLE_FREE)
        )
        return plan(spec, live, CatalogLock.of(live), CAPABILITIES, PlanOptions(now=NOW))

    result = plan_omitting("vector")
    assert result.omitted == (vector,)
    assert result.catalog.requirements == (cube,)
    with pytest.raises(PlanError) as caught:
        plan_omitting("vector, nope")
    assert "source.omit names 'nope', which the source does not use" in caught.value.problems


# Tables nothing selects, and seed patterns (subset.unselected, glob seeds)


def strict_plan(body: str, capabilities: Capabilities = CAPABILITIES) -> Plan:
    """Plan without the :func:`quiet` default, as a spec on its own would."""
    live = shop()
    spec = parse_spec(HEADER + CYCLE_FREE + body)
    return plan(spec, live, CatalogLock.of(live), capabilities, PlanOptions(now=NOW))


def strict_problems(body: str) -> tuple[str, ...]:
    with pytest.raises(PlanError) as caught:
        strict_plan(body)
    return caught.value.problems


def named(problems_or_warnings: tuple[str, ...]) -> set[str]:
    """The tables messages about unselected tables name."""
    return {
        text.split("selects rows of ", 1)[1].split(";", 1)[0]
        for text in problems_or_warnings
        if "selects rows of" in text
    }


KEPT = {
    "public.users",
    "public.categories",
    "public.posts",
    "public.comments",
    "public.events",
    "public.film_actor",
}


def test_a_spec_without_seeds_fails_naming_every_table() -> None:
    reasons = strict_problems("")
    assert named(reasons) == KEPT
    assert all("set subset.unselected" in r for r in reasons if "selects rows of" in r)


def test_followed_references_select_tables() -> None:
    reasons = strict_problems("subset: {seeds: {users: 'id = 1'}}")
    assert named(reasons) == KEPT - {"public.users"}
    # Following users -> posts brings posts in, and posts pull in their categories.
    reasons = strict_problems(
        "subset: {seeds: {users: 'id = 1'}, follow: {down: [users -> posts]}}"
    )
    assert named(reasons) == {"public.comments", "public.events", "public.film_actor"}


def test_follow_all_selects_the_parent() -> None:
    reasons = strict_problems(
        "subset:\n  seeds: {users: 'id = 1'}\n"
        "  edges: {posts.category_id -> categories: {follow: all}}"
    )
    assert "public.categories" not in named(reasons)


def test_unselected_none_keeps_no_rows_and_warns() -> None:
    result = strict_plan("subset: {seeds: {users: 'id = 1'}, unselected: none}")
    assert named(result.warnings) == KEPT - {"public.users"}
    assert {str(t) for t in result.unselected} == KEPT - {"public.users"}
    conditions = nested_conditions(result.graph())
    assert isinstance(conditions[TableName.of("public", "events")], NoRows)


def test_unselected_all_copies_every_row() -> None:
    result = strict_plan("subset: {seeds: {users: 'id = 1'}, unselected: all}")
    assert result.warnings == ()
    assert {str(t) for t in result.unselected} == KEPT - {"public.users"}
    every = {str(rule.table) for rule in result.tables if rule.every_row}
    assert every == KEPT - {"public.users"}
    conditions = nested_conditions(result.graph())
    assert isinstance(conditions[TableName.of("public", "events")], AllRows)
    assert not isinstance(conditions[TableName.of("public", "users")], AllRows)


def test_a_glob_seed_selects_every_kept_table() -> None:
    result = strict_plan('structure_only: [film_actor]\nsubset: {seeds: {"*": "TRUE"}}')
    seeds = {str(rule.table): rule.seed for rule in result.tables if rule.seed}
    # Excluded and structure-only tables are skipped, not refused.
    assert seeds == dict.fromkeys(KEPT - {"public.film_actor"}, "TRUE")
    (seed,) = result.seeds
    assert seed.pattern == "*"
    assert {str(t) for t in seed.tables} == KEPT - {"public.film_actor"}
    assert result.unselected == ()


def test_a_table_name_wins_over_a_pattern() -> None:
    result = strict_plan('subset: {seeds: {"*": "TRUE", users: "id = 1"}}')
    seeds = {str(rule.table): rule.seed for rule in result.tables if rule.seed}
    assert seeds["public.users"] == "id = 1"
    assert seeds["public.posts"] == "TRUE"
    # The plan records what each key actually seeded.
    recorded = {seed.pattern: {str(t) for t in seed.tables} for seed in result.seeds}
    assert recorded == {"*": KEPT - {"public.users"}, "users": {"public.users"}}


def test_seed_pattern_problems() -> None:
    reasons = strict_problems('subset: {seeds: {"*": "TRUE", "p*": "id > 1", "zz*": "TRUE"}}')
    assert "public.posts matches more than one seed pattern ('*', 'p*')" in " ".join(reasons)
    assert "seed pattern 'zz*' matches no table that keeps rows" in reasons
    # Two names for one table are as ambiguous as two patterns.
    reasons = strict_problems('subset: {seeds: {users: "id = 1", public.users: "id = 2"}}')
    assert "seeds 'users' and 'public.users' both name public.users" in reasons
    # Patterns are case-insensitive unless the spec says otherwise.
    result = strict_plan('subset: {seeds: {"USER*": "TRUE"}, unselected: none}')
    assert {str(t) for t in result.seeds[0].tables} == {"public.users"}


def test_complete_engines_keep_their_own_check() -> None:
    caps = CAPABILITIES.model_copy(update={"complete": True})
    with pytest.raises(PlanError) as caught:
        strict_plan("subset: {unselected: all}", capabilities=caps)
    assert any("neither selected nor excluded" in p for p in caught.value.problems)
    assert named(caught.value.problems) == set()


def test_unselected_all_still_honours_drop() -> None:
    result = strict_plan(
        "subset:\n  seeds: {posts: 'id = 13'}\n  unselected: all\n"
        "  edges: {comments.post_id -> posts: {on_missing: drop}}"
    )
    rule = next(r for r in result.tables if str(r.table) == "public.comments")
    assert rule.every_row
    condition = nested_conditions(result.graph())[rule.table]
    assert not isinstance(condition, AllRows)


def test_copy_accounts_need_the_capability_and_a_physical_image() -> None:
    accounts = "package: {accounts: [{user: app, password: x}]}\n"
    reasons = problems(CYCLE_FREE + accounts)
    assert "the fake adapter cannot give copies accounts (package.accounts)" in reasons
    able = CAPABILITIES.model_copy(update={"accounts": AccountNames()})
    assert planned(CYCLE_FREE + accounts, capabilities=able).formats == (
        ImageFormat.PHYSICAL,
        ImageFormat.LOGICAL,
    )
    logical = "package: {formats: [logical], accounts: [{user: app, password: x}]}\n"
    reasons = problems(CYCLE_FREE + logical, capabilities=able)
    assert "package.accounts applies to physical images, and this build makes none" in reasons


def test_copy_account_names() -> None:
    names = AccountNames(
        max_characters=5,
        max_bytes=6,
        reserved=("builder",),
        reserved_prefixes=("pg_",),
        keep_all=("boss", "root"),
    )
    able = CAPABILITIES.model_copy(update={"accounts": names})
    users = ["app", "app", "toolong", "éééé", "builder", "pg_x", "root"]
    accounts = ", ".join(f"{{user: {user}, password: x}}" for user in users)
    accounts += ", {user: boss, password: x, privileges: read}"
    reasons = problems(CYCLE_FREE + f"package: {{accounts: [{accounts}]}}\n", capabilities=able)
    assert set(reasons) == {
        "package.accounts names 'app' more than once",
        "package.accounts: 'toolong' is longer than the 5 characters fake allows",
        "package.accounts: 'toolong' is longer than the 6 bytes fake allows",
        "package.accounts: 'éééé' is longer than the 6 bytes fake allows",
        "package.accounts: 'builder' is longer than the 5 characters fake allows",
        "package.accounts: 'builder' is longer than the 6 bytes fake allows",
        "package.accounts: 'builder' is a name fake keeps for itself",
        "package.accounts: 'pg_x' starts with 'pg_', which fake keeps for itself",
        "package.accounts: 'boss' must keep every privilege on fake (privileges: all)",
    }
