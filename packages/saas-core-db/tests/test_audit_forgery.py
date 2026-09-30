"""core.log_audit tenant binding.

Regression suite for an audit finding: the original function was
SECURITY DEFINER, granted to `authenticated`, and took p_org, p_workspace,
p_actor and p_actor_type as caller-supplied arguments with no membership check.

Any signed-in user could therefore write an arbitrary row into another
organisation's trail. The damage was worse than an ordinary cross-tenant write
because core.audit_log is append-only by trigger: a forged row could not be
deleted by anyone, including the operator. The integrity property was exactly
inverted - history could not be corrected, only fabricated.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from conftest import (
    ORG_BROADMATE,
    ORG_RIVAL,
    OUTSIDER,
    OWNER,
    SUPERADMIN,
    acting_as,
    rows_as,
    scalar_as,
)


@pytest.fixture
def event_name() -> str:
    """A unique event per test.

    core.audit_log is append-only by design, and `acting_as` commits its
    transaction block, so a row written inside one cannot be cleaned up
    afterwards. Counting by a fixed event name would therefore accumulate
    across runs and pass or fail depending on history.
    """
    return f"test.{uuid.uuid4().hex[:12]}"


def log_audit_as(conn, user, **kwargs) -> None:
    """Call core.log_audit as a tenant, with whatever arguments they choose."""
    args = {
        "p_scope": "organisation",
        "p_event": "test.forged",
        "p_org": None,
        "p_workspace": None,
        "p_actor_type": "user",
        "p_actor": None,
        **kwargs,
    }
    with acting_as(conn, user):
        with conn.cursor() as cur:
            cur.execute(
                """
                select core.log_audit(
                  %(p_scope)s::core.audit_scope, %(p_event)s,
                  p_org        => %(p_org)s::uuid,
                  p_workspace  => %(p_workspace)s::uuid,
                  p_actor_type => %(p_actor_type)s::core.actor_type,
                  p_actor      => %(p_actor)s::uuid
                )
                """,
                args,
            )


def forged_rows(conn, event: str) -> list:
    return rows_as(
        conn, SUPERADMIN, "select id from core.audit_log where event = %s", (event,)
    )


# ---------------------------------------------------------------------------
# The forgery itself
# ---------------------------------------------------------------------------


def test_a_tenant_cannot_write_into_another_organisations_trail(conn, event_name):
    """The finding. An outsider naming Broadmate's org id must be refused."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
        log_audit_as(conn, OUTSIDER, p_org=ORG_BROADMATE, p_event=event_name)
    conn.rollback()

    assert exc.value.diag.message_hint == "audit_tenant_forbidden"
    assert forged_rows(conn, event_name) == []


def test_a_tenant_can_still_write_their_own_trail(conn, event_name):
    """The fix must not break the legitimate path."""
    log_audit_as(conn, OWNER, p_org=ORG_BROADMATE, p_event=event_name)
    assert len(forged_rows(conn, event_name)) == 1


def test_the_actor_is_taken_from_the_session_not_the_argument(conn, event_name):
    """A caller must not be able to attribute their action to someone else -
    otherwise the trail answers 'who did this' with whatever they typed."""
    log_audit_as(
        conn, OWNER, p_org=ORG_BROADMATE, p_event=event_name, p_actor=OUTSIDER
    )
    actor = scalar_as(
        conn, SUPERADMIN,
        "select actor_id::text from core.audit_log where event = %s", (event_name,),
    )
    assert actor == OWNER, "the argument was trusted over the session"


def test_a_tenant_cannot_attribute_their_action_to_an_agent(conn, event_name):
    """Agent, automation and system attribution is reserved for the service
    role. Otherwise a tenant can launder their own action as the AI's."""
    log_audit_as(
        conn, OWNER, p_org=ORG_BROADMATE, p_event=event_name, p_actor_type="agent"
    )
    actor_type = scalar_as(
        conn, SUPERADMIN,
        "select actor_type::text from core.audit_log where event = %s", (event_name,),
    )
    assert actor_type == "user"


def test_an_operator_is_described_as_one_and_still_cannot_pose_as_an_agent(conn, event_name):
    """20260917000001: the type is DERIVED from core.platform_users, never
    taken from the argument. A superadmin asking to be logged as 'agent' or as
    'user' is logged as superadmin either way, because that is what the row
    says they are."""
    for asked in ("agent", "user", "superadmin"):
        log_audit_as(
            conn, SUPERADMIN, p_org=ORG_BROADMATE, p_event=event_name, p_actor_type=asked
        )
    types = {
        r[0]
        for r in rows_as(
            conn, SUPERADMIN,
            "select actor_type::text from core.audit_log where event = %s", (event_name,),
        )
    }
    assert types == {"superadmin"}


def test_a_tenant_cannot_write_a_platform_scoped_row(conn, event_name):
    """Platform scope is the operator's log, not a tenant's."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
        log_audit_as(conn, OWNER, p_scope="platform", p_event=event_name)
    conn.rollback()
    assert exc.value.diag.message_hint == "audit_scope_forbidden"


def test_a_workspace_row_must_name_its_organisation(conn, event_name):
    """Without the org, the tenant check has nothing to check against - which
    is how a workspace-scoped row could sidestep it entirely."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
        log_audit_as(
            conn, OWNER, p_scope="workspace", p_org=None,
            p_workspace="00000000-0000-4000-8000-000000000050",
            p_event=event_name,
        )
    conn.rollback()
    assert exc.value.diag.message_hint == "audit_org_required"


def test_the_superadmin_may_still_write_anywhere(conn, event_name):
    log_audit_as(conn, SUPERADMIN, p_scope="platform", p_event=event_name)
    assert len(forged_rows(conn, event_name)) == 1


# ---------------------------------------------------------------------------
# Append-only, including the path a row-level trigger cannot see
# ---------------------------------------------------------------------------


def test_truncate_is_refused(conn):
    """A row-level trigger does not fire for TRUNCATE, so the append-only
    guarantee needed a statement-level trigger as well."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
        with conn.cursor() as cur:
            cur.execute("truncate core.audit_log")
    conn.rollback()


@pytest.mark.parametrize(
    "statement",
    [
        "update core.audit_log set event = 'x' where id = %s",
        "delete from core.audit_log where id = %s",
    ],
)
def test_update_and_delete_remain_refused(conn, statement):
    """The original guarantee must survive the fix.

    Parameterised rather than looped: recovering from the first failure needs a
    rollback, which also discards the row the second statement would target -
    so the second would match zero rows and the trigger would never fire.
    """
    with conn.cursor() as cur:
        cur.execute(
            "insert into core.audit_log (scope, event, actor_type) "
            "values ('platform', 'test.still_immutable', 'system') returning id"
        )
        audit_id = cur.fetchone()[0]

    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
        with conn.cursor() as cur:
            cur.execute(statement, (audit_id,))
    conn.rollback()
