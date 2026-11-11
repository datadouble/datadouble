# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

import pytest

from datadouble.names import ColumnName, TableName
from datadouble.subset import (
    AllOf,
    AllRows,
    AnyNull,
    AnyOf,
    Edge,
    EdgeMode,
    Graph,
    NoRows,
    Predicate,
    SubsetError,
    TableRule,
    ValuesIn,
    all_of,
    any_of,
    key_conditions,
    nested_conditions,
)
from fakes import FakeSource, Row


def t(name: str) -> TableName:
    return TableName.of("public", name)


def cols(*names: str) -> tuple[ColumnName, ...]:
    return tuple(ColumnName(n) for n in names)


def edge(
    child: str,
    column: str,
    parent: str,
    *,
    mode: EdgeMode = EdgeMode.FOLLOW,
    down: bool = False,
    when: str | None = None,
) -> Edge:
    return Edge(
        name=f"{child}_{column}_fkey",
        child=t(child),
        columns=cols(column),
        parent=t(parent),
        parent_columns=cols("id"),
        mode=mode,
        down=down,
        when=when,
        virtual=when is not None,
    )


def rules(
    *names: str, seeds: dict[str, str] | None = None, **kwargs: bool
) -> dict[TableName, TableRule]:
    seeds = seeds or {}
    return {
        t(name): TableRule(
            table=t(name),
            seed=seeds.get(name),
            every_row=kwargs.get(f"{name}_all", False),
            rows_kept=not kwargs.get(f"{name}_empty", False),
        )
        for name in names
    }


def ids(rows: list[Row]) -> list[int]:
    return sorted(int(str(row["id"])) for row in rows)


@pytest.fixture
def blog() -> FakeSource:
    return FakeSource(
        rows={
            t("users"): [{"id": i, "created": i} for i in range(1, 7)],
            t("categories"): [{"id": i} for i in range(1, 4)],
            t("posts"): [
                {"id": 10, "user_id": 1, "category_id": 1},
                {"id": 11, "user_id": 1, "category_id": 2},
                {"id": 12, "user_id": 2, "category_id": None},
                {"id": 13, "user_id": 5, "category_id": 3},
            ],
            t("comments"): [
                {"id": 100, "post_id": 10, "author_id": 3},
                {"id": 101, "post_id": 10, "author_id": 4},
                {"id": 102, "post_id": 13, "author_id": 1},
                {"id": 103, "post_id": 12, "author_id": None},
            ],
            t("events"): [
                {"id": 200, "subject_type": "Post", "subject_id": 13},
                {"id": 201, "subject_type": "User", "subject_id": 6},
                {"id": 202, "subject_type": "Post", "subject_id": 12},
            ],
        }
    )


def both(graph: Graph, source: FakeSource) -> dict[str, list[int]]:
    nested = nested_conditions(graph)
    keys = key_conditions(graph, source)
    result_a = {str(name.name): ids(source.select(name, nested)) for name in graph.rules}
    result_b = {str(name.name): ids(source.select(name, keys)) for name in graph.rules}
    assert result_a == result_b
    return result_a


def test_seed_pulls_parents_and_followed_children(blog: FakeSource) -> None:
    graph = Graph(
        rules(
            "users", "categories", "posts", "comments", "events", seeds={"users": "created <= 1"}
        ),
        (
            edge("posts", "user_id", "users", down=True),
            edge("posts", "category_id", "categories"),
            edge("comments", "post_id", "posts", down=True),
            edge("comments", "author_id", "users", mode=EdgeMode.NULL),
            edge("events", "subject_id", "posts", when="subject_type = 'Post'"),
        ),
    )
    chosen = both(graph, blog)
    assert chosen == {
        "users": [1],
        "posts": [10, 11],
        "categories": [1, 2],
        "comments": [100, 101],
        "events": [],
    }


def test_children_pull_their_other_parents(blog: FakeSource) -> None:
    graph = Graph(
        rules("users", "categories", "posts", "comments", "events", seeds={"comments": "id = 102"}),
        (
            edge("posts", "user_id", "users"),
            edge("posts", "category_id", "categories"),
            edge("comments", "post_id", "posts"),
            edge("comments", "author_id", "users"),
            edge("events", "subject_id", "posts", when="subject_type = 'Post'"),
        ),
    )
    assert both(graph, blog) == {
        "users": [1, 5],
        "posts": [13],
        "categories": [3],
        "comments": [102],
        "events": [],
    }


def test_virtual_reference_pulls_parents_only_where_it_applies(blog: FakeSource) -> None:
    graph = Graph(
        rules("users", "posts", "events", seeds={"events": "id >= 200"}),
        (
            edge("posts", "user_id", "users"),
            edge("events", "subject_id", "posts", when="subject_type = 'Post'"),
        ),
    )
    assert both(graph, blog) == {"users": [2, 5], "posts": [12, 13], "events": [200, 201, 202]}


def test_followed_virtual_references_apply_their_condition(blog: FakeSource) -> None:
    virtual = edge("events", "subject_id", "posts", when="subject_type = 'Post'", down=True)
    graph = Graph(rules("posts", "events", seeds={"posts": "id >= 12"}), (virtual,))
    # Event 201 has subject_id 6, but it refers to a user, not post 6.
    assert both(graph, blog) == {"posts": [12, 13], "events": [200, 202]}


def test_references_to_non_keys_are_not_inlined(blog: FakeSource) -> None:
    keyed = edge("events", "subject_id", "posts", when="subject_type = 'Post'", down=True)
    loose = Edge(
        name=keyed.name,
        child=keyed.child,
        columns=keyed.columns,
        parent=keyed.parent,
        parent_columns=keyed.parent_columns,
        down=True,
        when=keyed.when,
        virtual=True,
        parent_unique=False,
    )
    assert Graph(rules("posts", "events", seeds={"posts": "id = 12"}), (keyed,)).is_acyclic()
    assert not Graph(rules("posts", "events", seeds={"posts": "id = 12"}), (loose,)).is_acyclic()
    assert key_conditions(
        Graph(rules("posts", "events", seeds={"posts": "id = 12"}), (loose,)), blog
    )


def test_follow_all_takes_the_whole_parent(blog: FakeSource) -> None:
    graph = Graph(
        rules("categories", "posts", seeds={"posts": "id = 12"}),
        (edge("posts", "category_id", "categories", mode=EdgeMode.ALL),),
    )
    assert both(graph, blog) == {"categories": [1, 2, 3], "posts": [12]}


def test_every_row_rule(blog: FakeSource) -> None:
    graph = Graph(rules("categories", categories_all=True), ())
    assert both(graph, blog) == {"categories": [1, 2, 3]}


def test_drop_leaves_out_children_without_parents(blog: FakeSource) -> None:
    graph = Graph(
        rules("users", "posts", "comments", seeds={"comments": "id >= 100", "posts": "id = 13"}),
        (
            edge("comments", "post_id", "posts", mode=EdgeMode.DROP),
            edge("comments", "author_id", "users"),
            edge("posts", "user_id", "users"),
        ),
    )
    # Comment 103 refers to post 12, which is not chosen; 102 refers to post 13.
    assert both(graph, blog) == {"users": [1, 5], "posts": [13], "comments": [102]}


def test_drop_keeps_rows_with_null_references(blog: FakeSource) -> None:
    graph = Graph(
        rules("users", "comments", users_empty=True, seeds={"comments": "id >= 102"}),
        (edge("comments", "author_id", "users", mode=EdgeMode.DROP),),
    )
    assert both(graph, blog) == {"users": [], "comments": [103]}


def test_cycles_need_the_keys_strategy() -> None:
    source = FakeSource(
        rows={
            t("store"): [{"id": 1, "manager_id": 10}, {"id": 2, "manager_id": 20}],
            t("staff"): [
                {"id": 10, "store_id": 1},
                {"id": 11, "store_id": 1},
                {"id": 20, "store_id": 2},
            ],
        }
    )
    graph = Graph(
        rules("store", "staff", seeds={"staff": "id = 11"}),
        (edge("store", "manager_id", "staff"), edge("staff", "store_id", "store")),
    )
    assert not graph.is_acyclic()
    with pytest.raises(SubsetError, match="cycle"):
        nested_conditions(graph)
    chosen = key_conditions(graph, source)
    assert ids(source.select(t("staff"), chosen)) == [10, 11]
    assert ids(source.select(t("store"), chosen)) == [1]


def test_structure_only_tables_have_no_rows(blog: FakeSource) -> None:
    graph = Graph(rules("users", users_empty=True), ())
    assert nested_conditions(graph) == {t("users"): NoRows()}
    assert key_conditions(graph, blog) == {t("users"): NoRows()}
    assert blog.reads == []


def test_any_of_simplifies() -> None:
    values = ValuesIn(cols("id"), frozenset({(1,)}))
    assert any_of([]) == NoRows()
    assert any_of([NoRows(), ValuesIn(cols("id"), frozenset())]) == NoRows()
    assert any_of([values, AllRows()]) == AllRows()
    assert any_of([values]) == values
    assert any_of([values, Predicate("x")]) == AnyOf((values, Predicate("x")))


def test_all_of_simplifies() -> None:
    null = AnyNull(cols("id"))
    assert all_of([]) == AllRows()
    assert all_of([AllRows(), null]) == null
    assert all_of([null, NoRows()]) == NoRows()
    assert all_of([null, Predicate("x")]) == AllOf((null, Predicate("x")))
