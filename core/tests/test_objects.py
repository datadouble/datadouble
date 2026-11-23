# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

from datadouble.catalog import Dependency, ObjectKind, ObjectRef, SourceObject
from datadouble.names import TableName
from datadouble.objects import Candidates, Dialect, infer_dependencies, merge
from fakes import table


def ref(kind: ObjectKind, name: str, namespace: str = "public") -> ObjectRef:
    return ObjectRef(kind=kind, namespace=tuple(namespace.split(".")), name=name)


TOTAL = ref(ObjectKind.FUNCTION, "total")
STAMP = ref(ObjectKind.PROCEDURE, "stamp")
TRIGGER = ref(ObjectKind.TRIGGER, "users")
CANDIDATES = Candidates.of(
    [table("users", ["id"]), table("orders", ["id"]), table("users", ["id"], namespace="audit")],
    [SourceObject(ref=item, fingerprint="") for item in (TOTAL, STAMP, TRIGGER)],
)


def names(text: str, dialect: Dialect, own: ObjectRef = TOTAL) -> set[str]:
    return {str(edge) for edge in infer_dependencies(text, own, CANDIDATES, dialect)}


def test_bodies_name_tables_and_routines() -> None:
    body = """
        -- orders are not counted here
        /* nor are
           orders */
        SELECT count(*) INTO n FROM users u WHERE u.id = 'orders';
        CALL public.stamp();
        RETURN total(n);
    """
    assert names(body, Dialect(quotes='"')) == {
        "table public.users (inferred)",
        "procedure public.stamp (inferred)",
    }


def test_qualified_and_quoted_names() -> None:
    assert names('SELECT * FROM "audit"."users"', Dialect(quotes='"')) == {
        "table audit.users (inferred)"
    }
    assert names("SELECT * FROM [audit].[users] JOIN db.public.orders", Dialect(quotes="[")) == {
        "table audit.users (inferred)",
        "table public.orders (inferred)",
    }
    assert names("SELECT users.id FROM `orders`", Dialect(quotes="`")) == {
        "table public.users (inferred)",
        "table public.orders (inferred)",
    }
    # Unqualified names prefer the object's own namespace.
    elsewhere = ref(ObjectKind.FUNCTION, "f", namespace="audit")
    assert names("SELECT 1 FROM users", Dialect(quotes='"'), elsewhere) == {
        "table audit.users (inferred)"
    }
    assert names(
        "SELECT 1 FROM users", Dialect(quotes='"'), ref(ObjectKind.FUNCTION, "f", "x")
    ) == {
        "table public.users (inferred)",
        "table audit.users (inferred)",
    }


def test_engine_literals_and_comments() -> None:
    mysql = Dialect(quotes="`", backslash_escapes=True, hash_comments=True)
    assert names("SET x = 'it\\'s orders'; # orders\nSELECT 1 FROM users", mysql) == {
        "table public.users (inferred)"
    }
    # Triggers are never named in code, so a table of the same name is what matches.
    assert names("UPDATE users SET id = 1", Dialect(quotes='"'), TRIGGER) == {
        "table public.users (inferred)"
    }


def test_exact_edges_win_over_inferred_ones() -> None:
    users = TableName.of("public", "users")
    exact = (Dependency(table=users), Dependency(table=users))
    inferred = (Dependency(table=users, inferred=True), Dependency(object=STAMP, inferred=True))
    assert merge(exact, inferred) == (
        Dependency(table=users),
        Dependency(object=STAMP, inferred=True),
    )


def test_strings_comments_and_quoted_names_are_read_as_their_engines_write_them() -> None:
    postgres = Dialect(quotes='"', dollar_quotes=True)
    body = """
        EXECUTE $q$SELECT 'orders' FROM orders$q$;
        PERFORM $$ it's orders $$;
        RAISE NOTICE E'it\\'s orders';
        SELECT 1 FROM "users--old" JOIN users ON true;
    """
    assert names(body, postgres) == {"table public.users (inferred)"}
    mysql = Dialect(
        quotes="`", backslash_escapes=True, hash_comments=True, double_quoted_strings=True
    )
    assert names('SET x = "orders"; SELECT 1 FROM `users`', mysql) == {
        "table public.users (inferred)"
    }
    python = Dialect(quotes='"', dollar_quotes=True, hash_comments=True)
    assert names("# orders\nplpy.execute('SELECT 1')\nusers = 1", python) == {
        "table public.users (inferred)"
    }
    unicode = Candidates.of([table("bücher", ["id"])], [])
    edges = infer_dependencies("SELECT * FROM bücher", TOTAL, unicode, postgres)
    assert [str(edge) for edge in edges] == ["table public.bücher (inferred)"]


def test_names_prefer_the_objects_own_database() -> None:
    tables = [
        table("users", ["id"], namespace="shop.public"),
        table("users", ["id"], namespace="billing.public"),
        table("orders", ["id"], namespace="billing.sales"),
    ]
    candidates = Candidates.of(tables, [])
    own = ref(ObjectKind.FUNCTION, "f", namespace="shop.public")
    postgres = Dialect(quotes='"')

    def found(text: str) -> set[str]:
        return {str(edge) for edge in infer_dependencies(text, own, candidates, postgres)}

    assert found("SELECT 1 FROM users") == {"table shop.public.users (inferred)"}
    assert found("SELECT 1 FROM public.users") == {"table shop.public.users (inferred)"}
    assert found("SELECT 1 FROM billing.public.users") == {"table billing.public.users (inferred)"}
    assert found("SELECT 1 FROM sales.orders") == {"table billing.sales.orders (inferred)"}
