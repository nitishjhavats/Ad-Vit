"""The entitlement functions must not answer questions about other tenants.

Regression suite for an audit finding: core.entitlement, core.access_mode,
core.can, core.limit_int, core.assert_entitled and core.org_entitlements are all
SECURITY DEFINER, granted to `authenticated`, and took the organisation as a
caller-supplied argument with no membership check.

Any signed-in user could therefore call

    select * from core.org_entitlements('<some other tenant>');

and read that organisation's plan, seat count, ad-account allowance, autonomy
ceiling, token budget, and any override a superadmin had granted them.

Nothing is written, so this is not the audit-forgery class of bug. It is worse
in one specific way: it is silent. A forged row is at least visible afterwards;
an enumerated competitor's plan tier leaves no trace at all.
"""

from __future__ import annotations

import psycopg
import pytest

from conftest import (
    ORG_BROADMATE,
    ORG_RIVAL,
    OUTSIDER,
    OWNER,
    SUPERADMIN,
    rows_as,
    scalar_as,
)

# Every entitlement entry point, with a call that reaches the guard.
PROBES = [
    ("entitlement", "select core.entitlement(%s, 'max_seats')"),
    ("access_mode", "select core.access_mode(%s, t_advit.product_id())::text"),
    ("can", "select core.can(%s, 'feature.experiments')"),
    ("limit_int", "select core.limit_int(%s, 'max_seats')"),
    ("assert_entitled", "select core.assert_entitled(%s, 'feature.experiments')"),
    ("org_entitlements", "select count(*) from core.org_entitlements(%s)"),
]


# ---------------------------------------------------------------------------
# The leak itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,sql", PROBES, ids=[p[0] for p in PROBES])
def test_a_tenant_cannot_probe_another_organisation(conn, name, sql):
    """The finding. An outsider naming Broadmate's org id must be refused."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
        scalar_as(conn, OUTSIDER, sql, (ORG_BROADMATE,))
    conn.rollback()

    assert exc.value.diag.message_hint == "org_not_visible"


@pytest.mark.parametrize("name,sql", PROBES, ids=[p[0] for p in PROBES])
def test_a_member_can_still_ask_about_their_own_organisation(conn, name, sql):
    """The fix must not break the path the product actually uses."""
    scalar_as(conn, OWNER, sql, (ORG_BROADMATE,))


@pytest.mark.parametrize("name,sql", PROBES, ids=[p[0] for p in PROBES])
def test_the_superadmin_may_ask_about_anyone(conn, name, sql):
    scalar_as(conn, SUPERADMIN, sql, (ORG_RIVAL,))


@pytest.mark.parametrize("name,sql", PROBES, ids=[p[0] for p in PROBES])
def test_an_anonymous_caller_is_refused(conn, name, sql):
    """`acting_as(conn, None)` sets role=authenticated with NO subject - which
    the conftest calls an anonymous caller, and which is exactly the shape an
    unauthenticated PostgREST request arrives in.

    The guard originally read "no subject means the backend", and an anonymous
    caller has no subject, so it took the trusted branch. Verified against the
    running database: `set local role anon; select core.entitlement(<any org>,
    'max_seats')` returned a value. The privilege revoke closes it at the door;
    this closes it in the function too.
    """
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
        scalar_as(conn, None, sql, (ORG_BROADMATE,))
    conn.rollback()
    assert exc.value.diag.message_hint == "org_not_visible"


def test_the_backend_is_still_unguarded(conn):
    """A connection with no PostgREST role at all is the service role or a
    direct connection - already inside the trust boundary, and the path the
    agent runtime uses to resolve autonomy for whichever workspace it is
    running. Guarding it would break every scheduled run.

    Note this deliberately does NOT use `acting_as`: that helper always sets
    role=authenticated, which is a tenant, not the backend.
    """
    with conn.cursor() as cur:
        cur.execute("select core.access_mode(%s, t_advit.product_id())::text", (ORG_RIVAL,))
        assert cur.fetchone()[0]
        cur.execute("select core.entitlement(%s, 'max_seats')", (ORG_RIVAL,))
        assert cur.fetchone()[0] is not None


# ---------------------------------------------------------------------------
# What the leak actually exposed
# ---------------------------------------------------------------------------


def test_a_competitors_plan_shape_is_not_enumerable(conn):
    """The commercial harm, stated concretely: org_entitlements returns every
    resolved feature with its provenance, so one call read a rival's whole
    plan - including whether a superadmin had granted them a private override."""
    mine = rows_as(
        conn, OWNER,
        "select feature_key, value_json, source from core.org_entitlements(%s)",
        (ORG_BROADMATE,),
    )
    assert mine, "the legitimate call must still return the caller's own plan"

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        rows_as(
            conn, OWNER,
            "select feature_key, value_json, source from core.org_entitlements(%s)",
            (ORG_RIVAL,),
        )
    conn.rollback()


def test_the_guard_covers_the_delegating_functions_too(conn):
    """can(), limit_int() and assert_entitled() do not check anything
    themselves - they delegate to entitlement() and access_mode(). That is why
    the fix guards those two rather than all six: one place to audit, and no
    way to add a seventh entry point that forgets."""
    for sql in (
        "select core.can(%s, 'feature.experiments')",
        "select core.limit_int(%s, 'max_autonomy_level')",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
            scalar_as(conn, OUTSIDER, sql, (ORG_BROADMATE,))
        conn.rollback()
        assert exc.value.diag.message_hint == "org_not_visible"


def test_a_null_organisation_is_not_an_error(conn):
    """A null org identifies nobody, so there is nothing to leak - and the
    callers rely on it resolving to the product default rather than raising."""
    assert scalar_as(conn, OWNER, "select core.entitlement(null, 'max_seats')") is not None


def test_an_unknown_organisation_does_not_confirm_or_deny(conn):
    """A random uuid must be refused the same way a real foreign org is.

    Otherwise the error message becomes an existence oracle: 'not visible' for a
    real tenant and something else for an invented one tells an attacker which
    uuids are real organisations.
    """
    unknown = "00000000-0000-4000-8000-0000000000ff"
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
        scalar_as(conn, OUTSIDER, "select core.access_mode(%s, t_advit.product_id())::text", (unknown,))
    conn.rollback()
    assert exc.value.diag.message_hint == "org_not_visible"
