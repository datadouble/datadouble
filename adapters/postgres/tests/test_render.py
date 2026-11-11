# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Rendering, TOC filtering, error redaction and placeholders, without a database."""

import datetime as dt
from collections.abc import Mapping

import pytest

from datadouble.catalog import Catalog, Column, ColumnKind, Table
from datadouble.engine import EngineError
from datadouble.names import ColumnName, EngineName, TableName
from datadouble.subset import (
    AllOf,
    AllRows,
    AnyNull,
    AnyOf,
    Condition,
    NoRows,
    Predicate,
    SelectedBy,
    ValuesIn,
)
from datadouble_postgres.errors import redact, tool_error
from datadouble_postgres.introspect import kind_of
from datadouble_postgres.render import Renderer, native
from datadouble_postgres.working import filter_toc, placeholder

AS_OF = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
USERS = TableName.of("public", "users")
POSTS = TableName.of("public", "posts")
PAYMENT = TableName.of("public", "payment")
ODD_TYPE = 'public."b\u0131g\u0131nt"'  # Pagila's domain name has dotless i's


def column(name: str, type_name: str = "integer", kind: ColumnKind = ColumnKind.INTEGER) -> Column:
    return Column(name=ColumnName(name), type=type_name, kind=kind, nullable=True)


CATALOG = Catalog(
    engine=EngineName("postgres"),
    tables=(
        Table(name=USERS, columns=(column("id"), column("org", ODD_TYPE))),
        Table(name=POSTS, columns=(column("id"), column("user_id"))),
        Table(name=PAYMENT, columns=(column("id"),)),
    ),
)
C = ColumnName


def render(
    condition: Condition,
    table: TableName = USERS,
    selections: Mapping[TableName, Condition] | None = None,
) -> tuple[str, dict[str, object]]:
    renderer = Renderer(CATALOG, frozenset({PAYMENT}), AS_OF, dict(selections or {}))
    query = renderer.select(table, (C("id"),), condition)
    return query.text.as_string(None), query.params


def test_simple_conditions() -> None:
    assert render(AllRows())[0] == 'SELECT "id" FROM ONLY "public"."users" WHERE TRUE'
    assert render(NoRows())[0].endswith("WHERE FALSE")
    assert render(AnyNull((C("id"), C("org"))))[0].endswith('WHERE ("id" IS NULL OR "org" IS NULL)')
    assert render(AllRows(), PAYMENT)[0] == 'SELECT "id" FROM "public"."payment" WHERE TRUE'


def test_predicates_bind_as_of_and_escape_percent() -> None:
    sql, params = render(
        Predicate("created >= :as_of - interval '1 day' AND name LIKE 'a%' AND x::text = 'y'")
    )
    assert sql.endswith(
        "WHERE (created >= %(as_of)s - interval '1 day' AND name LIKE 'a%%' AND x::text = 'y')"
    )
    assert params == {"as_of": AS_OF}
    assert native("a::as_of").as_string(None) == "a::as_of"


def test_value_sets() -> None:
    sql, params = render(ValuesIn((C("id"),), frozenset({(1,), (2,)})))
    assert sql.endswith('WHERE "id" = ANY(%(p0)s::integer[])')
    assert params["p0"] in ([1, 2], [2, 1])
    sql, params = render(ValuesIn((C("id"), C("org")), frozenset({(1, 5)})))
    expected = 'WHERE ("id", "org") IN (SELECT * FROM unnest(%(p0)s::integer[], %(p1)s::TYPE[]))'
    assert sql.endswith(expected.replace("TYPE", ODD_TYPE))
    assert params == {"p0": [1], "p1": [5]}
    assert render(ValuesIn((C("id"),), frozenset()))[0].endswith("WHERE FALSE")


def test_unknown_columns_fail() -> None:
    with pytest.raises(EngineError, match=r"public\.users\.nope is not in the catalog"):
        render(ValuesIn((C("nope"),), frozenset({(1,)})))
    with pytest.raises(EngineError, match=r"nowhere\.x\.id is not in the catalog"):
        render(ValuesIn((C("id"),), frozenset({(1,)})), TableName.of("nowhere", "x"))


def test_nested_selections_use_ctes() -> None:
    selections: dict[TableName, Condition] = {
        USERS: Predicate("id < 3"),
        POSTS: SelectedBy((C("user_id"),), USERS, (C("id"),)),
    }
    condition = AnyOf(
        (
            SelectedBy((C("id"),), POSTS, (C("user_id"),), where="kind = 'x'"),
            AllOf((Predicate("id > 0"), AnyNull((C("org"),)))),
        )
    )
    sql, params = render(condition, selections=selections)
    assert sql == (
        'WITH "dd_selected_0" AS MATERIALIZED '
        '(SELECT * FROM ONLY "public"."users" WHERE (id < 3)), '
        '"dd_selected_1" AS MATERIALIZED (SELECT * FROM ONLY "public"."posts" WHERE "user_id" IN '
        '(SELECT "id" FROM "dd_selected_0")) '
        'SELECT "id" FROM ONLY "public"."users" WHERE ("id" IN '
        '(SELECT "user_id" FROM "dd_selected_1" '
        "WHERE (kind = 'x')) OR ((id > 0) AND (\"org\" IS NULL)))"
    )
    assert params == {"as_of": AS_OF}


def test_partial_sources_are_inlined() -> None:
    condition = SelectedBy((C("id"),), POSTS, (C("user_id"),), source_condition=Predicate("id = 1"))
    sql, _ = render(condition)
    assert sql.endswith(
        'WHERE "id" IN (SELECT "user_id" FROM ONLY "public"."posts" WHERE (id = 1))'
    )
    composite = SelectedBy(
        (C("id"), C("org")), POSTS, (C("id"), C("user_id")), source_condition=AllRows()
    )
    assert (
        '("id", "org") IN (SELECT "id", "user_id" FROM ONLY "public"."posts" WHERE TRUE)'
        in render(composite)[0]
    )


def test_missing_selection() -> None:
    with pytest.raises(EngineError, match=r"no selection for public\.posts"):
        render(SelectedBy((C("id"),), POSTS, (C("user_id"),)))


def test_distinct() -> None:
    renderer = Renderer(CATALOG, frozenset(), AS_OF)
    query = renderer.select(USERS, (C("id"), C("org")), AllRows(), distinct=True)
    assert (
        query.text.as_string(None)
        == 'SELECT DISTINCT "id", "org" FROM ONLY "public"."users" WHERE TRUE'
    )


LISTING = """;
; Archive created at 2026-11-11
;
215; 1259 16400 TABLE public actor postgres
300; 1259 16500 VIEW public actor_info postgres
301; 0 0 COMMENT public VIEW actor_info postgres
302; 1259 16501 VIEW dba recent_runs postgres
310; 1259 16510 MATERIALIZED VIEW public rental_by_category postgres
311; 0 16510 MATERIALIZED VIEW DATA public rental_by_category postgres
400; 2606 16600 FK CONSTRAINT public film_actor film_actor_actor_id_fkey postgres
401; 2606 16601 FK CONSTRAINT public film_actor film_actor_film_id_fkey postgres
"""


def test_filter_toc() -> None:
    kept = filter_toc(
        LISTING,
        frozenset(
            {
                ("VIEW", "public", "actor_info"),
                ("VIEW", "dba", "recent_runs"),
                ("MATERIALIZED VIEW", "public", "rental_by_category"),
                ("MATERIALIZED VIEW DATA", "public", "rental_by_category"),
                ("FK CONSTRAINT", "public", "film_actor film_actor_film_id_fkey"),
            }
        ),
    )
    assert kept.splitlines() == [
        ";",
        "; Archive created at 2026-11-11",
        ";",
        "215; 1259 16400 TABLE public actor postgres",
        "400; 2606 16600 FK CONSTRAINT public film_actor film_actor_actor_id_fkey postgres",
    ]


def test_redaction() -> None:
    assert redact('duplicate key value violates unique constraint "users_email_key"') == (
        'duplicate key value violates unique constraint "…"'
    )
    assert redact("invalid input syntax for type integer: 'abc'") == (
        'invalid input syntax for type integer: "…"'
    )
    error = tool_error(
        "pg_restore",
        1,
        "pg_restore: error: could not execute query: ERROR:  insert failed\n"
        "DETAIL:  Key (email)=(a@b.c) exists.\n",
    )
    assert str(error) == (
        "pg_restore exited with status 1: "
        "pg_restore: error: could not execute query: ERROR:  insert failed"
    )
    assert "a@b.c" not in str(error)
    assert str(tool_error("initdb", 2, "")) == "initdb exited with status 2: no output"


def test_placeholders() -> None:
    for kind in (ColumnKind.TEXT, ColumnKind.INTEGER, ColumnKind.TIMESTAMP, ColumnKind.UUID):
        assert (
            placeholder(USERS, column("c", "integer", kind)).as_string(None).endswith("::integer")
        )
    with pytest.raises(EngineError, match="no placeholder for c"):
        placeholder(USERS, column("c", "public.mpaa_rating", ColumnKind.OTHER))


@pytest.mark.parametrize(
    ("base", "category", "kind"),
    [
        ("int4", "N", ColumnKind.INTEGER),
        ("_int4", "A", ColumnKind.ARRAY),
        ("varchar", "S", ColumnKind.TEXT),
        ("mpaa_rating", "E", ColumnKind.OTHER),
        ("vector", "U", ColumnKind.OTHER),
    ],
)
def test_kinds(base: str, category: str, kind: ColumnKind) -> None:
    assert kind_of(base, category) is kind
