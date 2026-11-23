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
from datadouble_postgres.working import filter_toc, only_toc, placeholder

AS_OF = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
USERS = TableName.of("shop", "public", "users")
POSTS = TableName.of("shop", "public", "posts")
PAYMENT = TableName.of("shop", "public", "payment")
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
        render(ValuesIn((C("id"),), frozenset({(1,)})), TableName.of("shop", "nowhere", "x"))


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
    with pytest.raises(EngineError, match=r"no selection for shop\.public\.posts"):
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


EXTENSIONS = """\
7; 2615 16512 SCHEMA - s postgres
3539; 0 0 COMMENT - SCHEMA s postgres
2; 3079 16384 EXTENSION - hstore\x20
3540; 0 0 COMMENT - EXTENSION hstore\x20
221; 1259 16513 TABLE s t postgres
"""


def test_schemas_and_extensions_split_out() -> None:
    schemas, schema_entries = only_toc(EXTENSIONS, "SCHEMA")
    assert schemas == "7; 2615 16512 SCHEMA - s postgres\n"
    _, extension_entries = only_toc(EXTENSIONS, "EXTENSION")
    assert extension_entries == frozenset({("EXTENSION", "-", "hstore")})
    rest = filter_toc(filter_toc(EXTENSIONS, extension_entries), schema_entries, comments=False)
    assert rest.splitlines() == [
        "3539; 0 0 COMMENT - SCHEMA s postgres",
        "221; 1259 16513 TABLE s t postgres",
    ]


def test_redaction() -> None:
    # Data values are hidden, in every shape listed.
    hidden = {
        'invalid input syntax for type integer: "12a"': (
            'invalid input syntax for type integer: "…"'
        ),
        'invalid input value for enum mood: "glum"': 'invalid input value for enum mood: "…"',
        'value "99999999999" is out of range for type integer': (
            'value "…" is out of range for type integer'
        ),
        'date/time field value out of range: "2026-13-01"': (
            'date/time field value out of range: "…"'
        ),
        'invalid value "zz" for "MM"': 'invalid value "…" for "…"',
        'malformed array literal: "{1,"': 'malformed array literal: "…"',
        'Token "nope" is invalid.': 'Token "…" is invalid.',
        'invalid byte sequence for encoding "UTF8": 0xc3 0x28': (
            'invalid byte sequence for encoding "UTF8": …'
        ),
        'character with byte sequence 0xe2 0x82 in encoding "UTF8" has no equivalent': (
            'character with byte sequence … in encoding "UTF8" has no equivalent'
        ),
        "Key (email)=(a@b.c) already exists.": "Key (email)=(…) already exists.",
        "Key (lower(email::text))=(a@b.c) already exists.": (
            "Key (lower(email::text))=(…) already exists."
        ),
        'Key (owner)=(7) is not present in table "users".': (
            'Key (owner)=(…) is not present in table "users".'
        ),
        "Key (a, b)=(1, x)) conflicts with existing key (a, b)=(2, y).": (
            "Key (a, b)=(…) conflicts with existing key (a, b)=(…)."
        ),
        "Failing row contains (1, null, O'Brien).": "Failing row contains (…).",
        'COPY users, line 3, column email: "a@b.c"': 'COPY users, line 3, column email: "…"',
        'COPY users, line 3: "3\ta@b.c"': 'COPY users, line 3: "…"',
        # A value holding the quote mark is hidden whole.
        'invalid input syntax for type json: "say "hi" now"': (
            'invalid input syntax for type json: "…"'
        ),
    }
    for message, redacted in hidden.items():
        assert redact(message) == redacted
    # Object names are kept.
    for message in (
        'duplicate key value violates unique constraint "users_email_key"',
        'null value in column "name" of relation "parent" violates not-null constraint',
        'insert or update on table "orders" violates foreign key constraint "orders_user_fk"',
        'role "app" does not exist',
        'could not open file "base/16384/2619": No such file or directory',
        'extension "hstore" is not available',
        "value too long for type character varying(5)",
    ):
        assert redact(message) == message
    # A quoted string nothing classifies is hidden, as before, with its introducer.
    assert redact("syntax error at or near 'secret'") == "syntax error at or near '…'"
    assert redact("at or near E'sec ret' and U&'x'") == "at or near E'…' and U&'…'"
    # A value stops at the end of its line, and one running over a line break
    # is still hidden.
    assert redact('value "1" is out of range for type integer\ntable "t" is fine') == (
        'value "…" is out of range for type integer\ntable "t" is fine'
    )
    assert redact("Key (email)=(a@b\n.c) already exists.") == "Key (email)=(…) already exists."
    assert redact('invalid input syntax for type integer: "a\nb" SECRET"') == (
        'invalid input syntax for type integer: "…"'
    )
    # A user-raised message hides every quoted string, names included.
    assert redact('unknown user "a@b.c" in table "users"', raised=True) == (
        'unknown user "…" in table "…"'
    )
    trigger = tool_error(
        "pg_restore",
        1,
        'pg_restore: error: ERROR:  unknown user "a@b.c"\n'
        "CONTEXT:  PL/pgSQL function check_user() line 3 at RAISE\n",
    )
    for context in (
        'CONTEXT:  Traceback (most recent call last):\n  PL/Python function "f", line 2',
        'CONTEXT:  PL/Perl function "f"',
        'CONTEXT:  while executing "error x" in PL/Tcl function "f"',
    ):
        other = tool_error("psql", 3, f'ERROR:  unknown user "a@b.c"\n{context}\n')
        assert 'ERROR:  unknown user "…"' in str(other)
        assert "a@b.c" not in str(other)
    assert (
        str(trigger)
        == 'pg_restore exited with status 1: pg_restore: error: ERROR:  unknown user "…"'
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


def test_runtime_errors_kept() -> None:
    pulled = tool_error(
        "pg_dump",
        125,
        'Unable to find image "postgres:18.7-trixie" locally\n'
        'docker: Error response from daemon: toomanyrequests: "429 Too Many Requests".\n',
    )
    assert str(pulled) == (
        'pg_dump exited with status 125: Unable to find image "postgres:18.7-trixie" locally;'
        ' docker: Error response from daemon: toomanyrequests: "429 Too Many Requests".'
    )
    missing = tool_error("pg_dump", 127, 'exec: "pg_dump": executable file not found in $PATH')
    assert '"pg_dump"' in str(missing)
    assert str(tool_error("pg_dump", 1, 'pg_dump: error: "secret"')) == (
        'pg_dump exited with status 1: pg_dump: error: "…"'
    )


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
