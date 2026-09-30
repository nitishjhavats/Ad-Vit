"""The product schema is `t_advit`, and nothing still says `marketing`.

The rename was done as a rewrite of the migrations rather than an
``ALTER SCHEMA ... RENAME``, for a reason worth keeping visible: renaming moves
the objects, because they reference the namespace by OID, but **function bodies
and their ``search_path`` settings are stored as plain text and are not
rewritten**. Six functions would have kept pointing at a schema that no longer
existed and broken at call time rather than at rename time — which is the worst
possible moment to discover it, because it is the moment somebody is spending
money.

So these are not tests of the rename that happened once. They are the guard on
the property it established, and they catch the two ways it can silently come
back: a new migration written against the old name, and a function body that
still carries it.
"""

from __future__ import annotations

import pytest

from conftest import rows_as, SUPERADMIN

PRODUCT_SCHEMA = "t_advit"
OLD_SCHEMA = "marketing"


def test_the_old_schema_does_not_exist(conn):
    rows = rows_as(
        conn, SUPERADMIN,
        "select nspname from pg_namespace where nspname = %s", (OLD_SCHEMA,),
    )
    assert rows == [], (
        f"schema {OLD_SCHEMA!r} exists again. A migration written against the old "
        "name creates it silently rather than failing, and the product then has "
        "two schemas that each look right on their own."
    )


def test_the_product_schema_exists_and_holds_the_product(conn):
    """The counterpart, so this file fails loudly if the schema is renamed again
    without it being updated, rather than passing vacuously."""
    tables = rows_as(
        conn, SUPERADMIN,
        """
        select count(*) from pg_class c
          join pg_namespace n on n.oid = c.relnamespace
         where n.nspname = %s and c.relkind = 'r'
        """,
        (PRODUCT_SCHEMA,),
    )
    assert tables[0][0] > 20, f"{PRODUCT_SCHEMA} looks empty; did the rename half-apply?"


def test_no_function_body_still_names_the_old_schema(conn):
    """The failure mode an ALTER SCHEMA RENAME would have left behind.

    ``prosrc`` is plain text. A function whose body says ``marketing.workspaces``
    compiles fine, is listed fine, and raises ``3F000`` the first time it is
    called — which for ``compute_blended_daily`` is inside a scheduled job, and
    for ``effective_autonomy`` is inside the autonomy check that gates spend.
    """
    offenders = rows_as(
        conn, SUPERADMIN,
        """
        select n.nspname || '.' || p.proname
          from pg_proc p
          join pg_namespace n on n.oid = p.pronamespace
         where n.nspname in ('core', %s)
           and p.prosrc like %s
         order by 1
        """,
        (PRODUCT_SCHEMA, f"%{OLD_SCHEMA}.%"),
    )
    assert offenders == [], (
        "these function bodies still reference the old schema and will raise "
        "3F000 the first time they are called:\n  "
        + "\n  ".join(fn for (fn,) in offenders)
    )


def test_no_function_search_path_still_names_the_old_schema(conn):
    """The other half, and the easier one to miss. ``proconfig`` carries
    ``set search_path = ...`` as text too, so a SECURITY DEFINER function can
    resolve unqualified names against a schema that is gone."""
    offenders = rows_as(
        conn, SUPERADMIN,
        """
        select n.nspname || '.' || p.proname, array_to_string(p.proconfig, ', ')
          from pg_proc p
          join pg_namespace n on n.oid = p.pronamespace
         where n.nspname in ('core', %s)
           and array_to_string(p.proconfig, ',') like %s
         order by 1
        """,
        (PRODUCT_SCHEMA, f"%{OLD_SCHEMA}%"),
    )
    assert offenders == [], (
        "these functions still search the old schema:\n  "
        + "\n  ".join(f"{fn}: {cfg}" for fn, cfg in offenders)
    )


@pytest.mark.parametrize("key,name", [("advit", "ad-vit")])
def test_the_product_identity_matches_the_brand(conn, key, name):
    """`core.products.key` carries a check requiring `^[a-z][a-z0-9_]{2,49}$`,
    so the brand name itself is not a legal key - "ad-vit" has a hyphen. The
    key is `advit` and the display name carries the brand, and this pins both
    so they cannot drift apart."""
    rows = rows_as(conn, SUPERADMIN, "select key, name from core.products", ())
    assert (key, name) in rows, f"expected product ({key}, {name}); found {rows}"
