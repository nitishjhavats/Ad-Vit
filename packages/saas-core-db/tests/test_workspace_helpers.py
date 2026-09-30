"""The workspace helpers answer about your workspace, and refuse about anyone's.

``t_advit.workspace_org(p_workspace)`` was ``select w.org_id from
t_advit.workspaces w where w.id = p_workspace`` — SECURITY DEFINER, granted to
``authenticated``, and with a WHERE clause naming only the caller's own argument
and never the caller. ``workspaces_select`` was not bypassed; it was never
consulted. The whole guard was the input.

Reproduced as the rival tenant's owner: in the same transaction in which a
``select`` on that workspace row returned nothing, ``workspace_org`` on the same
id returned Broadmate's organisation uuid.

What leaked was identity metadata rather than tenant data, and the third item is
the one that makes it matter:

  1. workspace uuid → owning organisation uuid, for every workspace.
  2. a workspace-EXISTENCE oracle — a real id returned a uuid, a fabricated one
     returned NULL. ``app/auth/scope.py`` gives up a useful status code to avoid
     exactly this ("404, never 403"), and this handed the answer back at the side
     door.
  3. workspace uuids travel. They are path segments in
     ``/api/workspaces/{workspace_id}/…``, so they appear in URLs, screenshots
     and support tickets — and this was the join key from a leaked URL to a named
     tenant.

``effective_autonomy`` is a second channel for the same secret rather than a
second secret: its value was already protected by ``core.access_mode`` raising,
but CASE stops at the first matching WHEN, and ``when w.is_paused then 0`` came
first — a one-bit oracle on a foreign workspace's pause state that never reached
the guard.
"""

from __future__ import annotations

import pytest

from conftest import (
    ANALYST,
    ORG_BROADMATE,
    OUTSIDER,
    OWNER,
    SUPERADMIN,
    as_tenant,
    rows_as,
    scalar_as,
)

BROADMATE_WORKSPACE = "00000000-0000-4000-8000-000000000050"
RIVAL_WORKSPACE = "00000000-0000-4000-8000-000000000051"
NO_SUCH_WORKSPACE = "00000000-0000-4000-8000-000000009999"


# ---------------------------------------------------------------------------
# These tests run over `tenant_conn` - the real `advit_tenant` credential - and
# NOT over the `conn` fixture that the rest of this package uses. That is not a
# stylistic choice and it must not be "simplified" back.
#
# `rows_as(conn, user, ...)` connects as `postgres` and wears `authenticated`
# with the caller's claims. That works for every other test here, because an RLS
# policy keys on `auth.uid()` and does not care which login role is underneath.
#
# This guard keys on `session_user`, deliberately - a login role does not move
# with `set role`, which is exactly what makes it unforgeable by a request. And
# `postgres` holds rolbypassrls, so on that connection the guard's second arm
# fires and the caller is trusted. Written over `conn`, every test below passes
# for the wrong reason or fails for the wrong one: the first draft of this file
# came up red with `core.access_mode` raising, because the row was never filtered
# at all.
#
# The lesson, since this is the second time in this repository that a test proved
# something about `postgres` rather than about the boundary: when the guard names
# the CREDENTIAL, the test has to hold the credential.
# ---------------------------------------------------------------------------


def _scalar(tenant_conn, user, sql, params):
    with as_tenant(tenant_conn, user) as connection:
        with connection.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return row[0] if row else None


def org_of(tenant_conn, user, workspace):
    return _scalar(
        tenant_conn, user, "select t_advit.workspace_org(%s::uuid)::text", (workspace,)
    )


def autonomy_of(tenant_conn, user, workspace):
    return _scalar(
        tenant_conn, user, "select t_advit.effective_autonomy(%s::uuid)", (workspace,)
    )


# ---------------------------------------------------------------------------
# The leak
# ---------------------------------------------------------------------------


def test_a_stranger_cannot_resolve_a_workspace_to_its_organisation(tenant_conn):
    """The finding. Before: the rival tenant's owner got Broadmate's org uuid."""
    assert org_of(tenant_conn, OUTSIDER, BROADMATE_WORKSPACE) is None


def test_an_org_member_without_a_workspace_grant_is_refused_too(tenant_conn):
    """Deliberately TIGHTER than ``workspaces_select``, which uses the wider
    ``core.is_org_member``.

    ``app/auth/scope.py`` already 404s this account, on the grounds that
    ``is_workspace_member`` is the product's boundary. Aligning the helper with
    the API rather than with the table policy is the point; leaving them
    disagreeing is how a resolver ends up trusting the looser one.
    """
    assert org_of(tenant_conn, ANALYST, BROADMATE_WORKSPACE) is None


def test_the_existence_oracle_is_closed(tenant_conn):
    """A real foreign workspace and a fabricated id must be indistinguishable.

    The refusal is an ABSENT ROW rather than an exception, precisely so these two
    answers are the same answer. Raising would have moved the oracle rather than
    closing it — "not visible to you" and "no such workspace" are different
    sentences.
    """
    real = org_of(tenant_conn, OUTSIDER, BROADMATE_WORKSPACE)
    fabricated = org_of(tenant_conn, OUTSIDER, NO_SUCH_WORKSPACE)
    assert real is None and fabricated is None


def test_the_paused_state_of_a_foreign_workspace_does_not_leak(tenant_conn):
    """CASE evaluates its WHEN conditions in order and stops. ``when w.is_paused
    then 0`` came before the arm that calls ``core.access_mode`` — the only thing
    guarding this function — so a paused foreign workspace answered 0 and never
    reached the guard.

    Pausing Broadmate inside this transaction must not change what the rival
    tenant can observe.
    """
    before = autonomy_of(tenant_conn, OUTSIDER, BROADMATE_WORKSPACE)

    # Paused as the OWNER, who may write it; the question is what the rival can
    # observe afterwards.
    with as_tenant(tenant_conn, OWNER) as connection:
        with connection.cursor() as cur:
            cur.execute(
                "update t_advit.workspaces set is_paused = true where id = %s",
                (BROADMATE_WORKSPACE,),
            )

    after = autonomy_of(tenant_conn, OUTSIDER, BROADMATE_WORKSPACE)
    tenant_conn.rollback()

    assert before is None and after is None, (
        f"a foreign workspace's pause state is observable: {before!r} -> {after!r}"
    )


def test_the_refusal_does_not_name_the_organisation_in_an_error(tenant_conn):
    """``core.assert_org_visible`` refuses with ``'organisation % is not visible
    to you'``, which interpolates the org uuid — printing exactly the mapping
    this function was handing over directly.

    The guard is a WHERE conjunct rather than a CASE in the target list, so for a
    stranger the row is filtered before the select list is evaluated and
    ``core.access_mode`` is never called. No row, no exception, no uuid.
    """
    with as_tenant(tenant_conn, OUTSIDER) as connection:
        with connection.cursor() as cur:
            cur.execute(
                "select t_advit.effective_autonomy(%s::uuid)", (BROADMATE_WORKSPACE,)
            )
            assert cur.fetchone()[0] is None


# ---------------------------------------------------------------------------
# ...and the callers that must keep working
# ---------------------------------------------------------------------------


def test_a_member_still_resolves_its_own_workspace(tenant_conn):
    assert org_of(tenant_conn, OWNER, BROADMATE_WORKSPACE) == ORG_BROADMATE
    assert autonomy_of(tenant_conn, OWNER, BROADMATE_WORKSPACE) is not None


def test_a_superadmin_still_resolves_any_workspace(tenant_conn):
    """The support console. ``is_workspace_member`` admits a superadmin through
    ``core.has_org_role``, so no separate arm is needed — asserted rather than
    assumed, because if that ever changed the console would go dark quietly."""
    assert org_of(tenant_conn, SUPERADMIN, BROADMATE_WORKSPACE) == ORG_BROADMATE
    assert org_of(tenant_conn, SUPERADMIN, RIVAL_WORKSPACE) is not None


def test_the_backend_still_resolves_every_workspace(service_conn):
    """``advit_service`` INHERITs ``advit_backend`` and holds a database
    credential no browser has. It is the first arm of the allowlist."""
    with service_conn.cursor() as cur:
        cur.execute(
            "select t_advit.workspace_org(%s::uuid)::text,"
            "       t_advit.effective_autonomy(%s::uuid)",
            (BROADMATE_WORKSPACE, BROADMATE_WORKSPACE),
        )
        org, autonomy = cur.fetchone()
    assert org == ORG_BROADMATE
    assert autonomy is not None


def test_the_policies_that_name_workspace_org_still_work(tenant_conn):
    """The trap this fix deliberately avoids, pinned.

    ``workspace_org`` is named in the USING and WITH CHECK of
    ``workspace_members_write`` and ``meta_connections_write``. An RLS predicate
    runs with the QUERYING user's privileges, so revoking EXECUTE — the obvious
    fix — takes the function away from the policies too. Measured when tried:
    both tables failed with "permission denied for function workspace_org", the
    same way revoking ``has_org_role`` broke every tenant read in
    20260911000008.
    """
    with as_tenant(tenant_conn, OWNER) as conn:
        with conn.cursor() as cur:
            for table in ("workspace_members", "meta_connections"):
                cur.execute(f"select count(*) from t_advit.{table}")
                cur.fetchone()


# ---------------------------------------------------------------------------
# The shape of the guard, not just its answers
# ---------------------------------------------------------------------------


def test_the_trusted_set_is_an_allowlist_on_things_a_caller_cannot_set(conn):
    """The first draft of this fix enumerated the UNTRUSTED set —
    ``current_setting('role') in ('anon','authenticated')`` — and permitted
    everything else. That is a deny-list guarding a permit, on a GUC whose whole
    purpose is to be set per request: ``service_role`` was not on it, and neither
    is any role a later migration adds.

    This asserts the matrix instead of the phrasing. ``authenticator`` is the one
    that matters most: it is PostgREST's own login role, and it is what
    ``session_user`` would be if the API were ever fronted that way.
    """
    rows = rows_as(
        conn,
        SUPERADMIN,
        """
        select r.rolname,
               (pg_has_role(r.rolname, 'advit_backend', 'USAGE')
                or r.rolbypassrls or r.rolsuper) as trusted
          from pg_roles r
         where r.rolname in ('anon', 'authenticated', 'authenticator',
                             'advit_tenant', 'advit_service', 'advit_jobs')
         order by r.rolname
        """,
        (),
    )
    trusted = {name: value for name, value in rows}

    for role in ("anon", "authenticated", "authenticator", "advit_tenant"):
        assert trusted.get(role) is False, f"{role} is being treated as the backend"
    for role in ("advit_service", "advit_jobs"):
        assert trusted.get(role) is True, f"{role} is not being treated as the backend"


def test_session_user_does_not_move_with_set_role(tenant_conn):
    """The property the first arm rests on. If ``session_user`` followed
    ``set role``, a tenant could reach the trusted branch with a GUC it sets
    itself — which is precisely what made the deny-list version wrong."""
    with as_tenant(tenant_conn, OWNER) as conn:
        with conn.cursor() as cur:
            cur.execute("select current_user::text, session_user::text")
            current, session = cur.fetchone()
    assert current == "authenticated"
    assert session == "advit_tenant"
