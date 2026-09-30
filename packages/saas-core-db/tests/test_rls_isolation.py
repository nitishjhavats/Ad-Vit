"""Tenant isolation.

The assertion that matters throughout: a caller reaching for another tenant's
data gets ZERO ROWS, not another tenant's rows and not an error that leaks
existence. Every one of these is a CI gate.
"""

from __future__ import annotations

import psycopg
import pytest

from conftest import (
    MEMBER,
    ORG_BROADMATE,
    ORG_RIVAL,
    OUTSIDER,
    OWNER,
    SUPERADMIN,
    acting_as,
    id_set,
    rows_as,
    scalar_as,
)


# ---------------------------------------------------------------------------
# Organisations
# ---------------------------------------------------------------------------


def test_owner_sees_only_their_organisation(conn):
    ids = id_set(conn, OWNER, "select id from core.organisations")
    assert ids == {ORG_BROADMATE}


def test_outsider_sees_only_their_organisation(conn):
    ids = id_set(conn, OUTSIDER, "select id from core.organisations")
    assert ids == {ORG_RIVAL}


def test_superadmin_sees_every_organisation(conn):
    ids = id_set(conn, SUPERADMIN, "select id from core.organisations")
    assert {ORG_BROADMATE, ORG_RIVAL} <= ids


def test_targeted_cross_tenant_read_returns_zero_rows(conn):
    """Naming another tenant's id explicitly must not defeat the policy."""
    rows = rows_as(
        conn, OWNER, "select id from core.organisations where id = %s", (ORG_RIVAL,)
    )
    assert rows == []


def test_anonymous_caller_sees_nothing(conn):
    assert rows_as(conn, None, "select id from core.organisations") == []


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------


def test_membership_of_other_organisations_is_invisible(conn):
    org_ids = id_set(conn, OWNER, "select org_id from core.organisation_members")
    assert org_ids == {ORG_BROADMATE}


def test_member_cannot_enumerate_rival_members(conn):
    rows = rows_as(
        conn,
        MEMBER,
        "select user_id from core.organisation_members where org_id = %s",
        (ORG_RIVAL,),
    )
    assert rows == []


def test_plain_member_cannot_grant_themselves_admin(conn):
    """org_members_write is restricted to owner/admin, so the UPDATE matches
    no rows rather than escalating."""
    with acting_as(conn, MEMBER):
        with conn.cursor() as cur:
            cur.execute(
                "update core.organisation_members set role = 'admin' "
                "where org_id = %s and user_id = %s",
                (ORG_BROADMATE, MEMBER),
            )
            assert cur.rowcount == 0


def test_outsider_cannot_add_themselves_to_another_organisation(conn):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with acting_as(conn, OUTSIDER):
            with conn.cursor() as cur:
                cur.execute(
                    "insert into core.organisation_members (org_id, user_id, role) "
                    "values (%s, %s, 'owner')",
                    (ORG_BROADMATE, OUTSIDER),
                )


# ---------------------------------------------------------------------------
# Organisation lifecycle is superadmin-only
# ---------------------------------------------------------------------------


def test_owner_cannot_create_an_organisation(conn):
    """Only the superadmin creates organisations (platform brief)."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with acting_as(conn, OWNER):
            with conn.cursor() as cur:
                cur.execute(
                    "insert into core.organisations (name, slug) values ('Sneaky', 'sneaky')"
                )


def test_owner_cannot_delete_their_organisation(conn):
    with acting_as(conn, OWNER):
        with conn.cursor() as cur:
            cur.execute("delete from core.organisations where id = %s", (ORG_BROADMATE,))
            assert cur.rowcount == 0


# ---------------------------------------------------------------------------
# Subscriptions and entitlement overrides: tenant reads, superadmin writes
# ---------------------------------------------------------------------------


def test_subscription_of_another_tenant_is_invisible(conn):
    rows = rows_as(
        conn, OWNER, "select id from core.subscriptions where org_id = %s", (ORG_RIVAL,)
    )
    assert rows == []


def test_owner_cannot_upgrade_their_own_plan(conn):
    """Self-service plan changes would let a tenant grant themselves autonomy
    and token budget. Plan changes are a superadmin action."""
    with acting_as(conn, OWNER):
        with conn.cursor() as cur:
            cur.execute(
                "update core.subscriptions set status = 'active' where org_id = %s",
                (ORG_BROADMATE,),
            )
            assert cur.rowcount == 0


def test_owner_cannot_grant_themselves_an_entitlement_override(conn):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with acting_as(conn, OWNER):
            with conn.cursor() as cur:
                cur.execute(
                    "insert into core.entitlement_overrides "
                    "(org_id, feature_key, value_json, reason) "
                    "values (%s, 'max_autonomy_level', '4'::jsonb, 'self-granted')",
                    (ORG_BROADMATE,),
                )


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def test_platform_scoped_audit_is_hidden_from_tenants(conn):
    with acting_as(conn, SUPERADMIN):
        with conn.cursor() as cur:
            cur.execute(
                "select core.log_audit('platform', 'test.platform_only', "
                "p_actor_type => 'superadmin')"
            )

    scopes = {
        r[0] for r in rows_as(conn, OWNER, "select distinct scope from core.audit_log")
    }
    assert "platform" not in scopes


def test_tenant_cannot_read_another_tenants_audit_trail(conn):
    with acting_as(conn, SUPERADMIN):
        with conn.cursor() as cur:
            cur.execute(
                "select core.log_audit('organisation', 'test.rival_event', p_org => %s)",
                (ORG_RIVAL,),
            )

    rows = rows_as(
        conn,
        OWNER,
        "select id from core.audit_log where event = 'test.rival_event'",
    )
    assert rows == []


def test_audit_log_rejects_update_even_for_the_table_owner(conn):
    """Append-only is enforced by trigger, so privileged code cannot quietly
    rewrite history either."""
    with conn.cursor() as cur:
        cur.execute(
            "insert into core.audit_log (scope, event, actor_type) "
            "values ('platform', 'test.immutable', 'system') returning id"
        )
        audit_id = cur.fetchone()[0]

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with conn.cursor() as cur:
            cur.execute(
                "update core.audit_log set event = 'test.tampered' where id = %s",
                (audit_id,),
            )
    conn.rollback()


def test_audit_log_rejects_delete_even_for_the_table_owner(conn):
    with conn.cursor() as cur:
        cur.execute(
            "insert into core.audit_log (scope, event, actor_type) "
            "values ('platform', 'test.immutable_delete', 'system') returning id"
        )
        audit_id = cur.fetchone()[0]

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with conn.cursor() as cur:
            cur.execute("delete from core.audit_log where id = %s", (audit_id,))
    conn.rollback()


# ---------------------------------------------------------------------------
# Helper functions must not become an oracle
# ---------------------------------------------------------------------------


def test_membership_helper_does_not_leak_other_tenancies(conn):
    assert scalar_as(conn, OWNER, "select core.is_org_member(%s)", (ORG_RIVAL,)) is False
    assert scalar_as(conn, OWNER, "select core.is_org_member(%s)", (ORG_BROADMATE,)) is True


def test_superadmin_flag_is_read_from_the_database(conn):
    """A forged 'is_superadmin' JWT claim must not matter - the flag is a
    database column, consulted on every check."""
    assert scalar_as(conn, OWNER, "select core.is_superadmin()") is False
    assert scalar_as(conn, SUPERADMIN, "select core.is_superadmin()") is True


def test_user_cannot_promote_themselves_to_superadmin(conn):
    """platform_users_update_self decides WHICH ROW you may touch, not which
    columns. Column-level grants are what stop a user writing their own
    privilege flag; without them this policy would permit escalation."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with acting_as(conn, OWNER):
            with conn.cursor() as cur:
                cur.execute(
                    "update core.platform_users set is_superadmin = true where id = %s",
                    (OWNER,),
                )
    conn.rollback()
    assert scalar_as(conn, OWNER, "select core.is_superadmin()") is False


def test_user_can_still_update_their_own_profile(conn):
    """The column guard must not break legitimate self-service."""
    with acting_as(conn, OWNER):
        with conn.cursor() as cur:
            cur.execute(
                "update core.platform_users set full_name = %s where id = %s",
                ("Demo Owner", OWNER),
            )
            assert cur.rowcount == 1
