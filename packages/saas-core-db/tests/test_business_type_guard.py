"""The compliance ruleset selector, and who may move it.

t_advit.workspaces.industry_key decides WHICH RULES CONSTRAIN THE TENANT.
The Ayurveda pack loads the Indian statutory layer - DMR Act Schedule J, the
AYUSH licence posture check - and the General D2C pack does not, because
t_advit.policy_rule_industries scopes those rules to 'ayurveda' alone.

The column was t_advit.workspaces.business_type, an enum. It is a foreign key
into t_advit.industries now (20260912000001) so that adding a pack is a row
insert; the guard is the same guard, watching the same decision.

So this is not an ordinary settings column. It is the answer to "which law
applies to me", and the regulated party does not get to fill that in after
onboarding. These tests run as the tenant, the way PostgREST does, because a
test that runs as the table owner proves nothing about a guard keyed on
auth.uid().
"""

from __future__ import annotations

import psycopg
import pytest

from conftest import MEMBER, ORG_BROADMATE, OUTSIDER, OWNER, acting_as, rows_as

WORKSPACE_AYURVEDA = "00000000-0000-4000-8000-000000000050"
WORKSPACE_GENERAL = "00000000-0000-4000-8000-000000000051"


def _audit_count(conn) -> int:
    """Read the trail as the table owner.

    core.audit_log carries its own RLS, and `acting_as(conn, None)` is an
    ANONYMOUS caller, not the backend - it sees nothing, so a test written
    through it would assert against a constant zero.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select count(*) from core.audit_log "
            " where event = 'workspace.industry_changed'"
        )
        return cur.fetchone()[0]


def test_an_org_owner_cannot_move_their_workspace_out_of_the_ayurveda_pack(conn):
    """The reproduction that mattered: switching to general_d2c makes both
    India-layer blocks disappear, so a piles-care advertiser could clear
    Schedule J by editing one enum on their own workspace.

    workspaces_write grants FOR ALL to an org owner or admin, so RLS alone
    permitted it - a column-level rule cannot be expressed as a policy, which is
    why the guard is a trigger.
    """
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
        rows_as(
            conn,
            OWNER,
            "update t_advit.workspaces set industry_key = 'general_d2c' where id = %s",
            (WORKSPACE_AYURVEDA,),
        )
    assert "industry_not_self_service" in str(exc.value)


def test_the_guard_names_the_workspace_and_the_direction_of_the_refused_change(conn):
    """A refusal an owner cannot act on becomes a support ticket that nobody can
    answer. The error carries which workspace and which transition."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
        rows_as(
            conn,
            OWNER,
            "update t_advit.workspaces set industry_key = 'general_d2c' where id = %s",
            (WORKSPACE_AYURVEDA,),
        )
    detail = str(exc.value)
    assert WORKSPACE_AYURVEDA in detail
    assert "ayurveda" in detail and "general_d2c" in detail


def test_a_workspace_may_still_be_reconfigured_in_every_other_respect(conn):
    """The guard is one column wide. Blocking the whole UPDATE would take caps
    and pause state with it, and those ARE self-service."""
    with acting_as(conn, OWNER):
        with conn.cursor() as cur:
            cur.execute(
                "update t_advit.workspaces set daily_cap_inr = 7500 where id = %s",
                (WORKSPACE_AYURVEDA,),
            )
            assert cur.rowcount == 1


def test_picking_a_pack_when_the_workspace_is_created_is_still_self_service(conn):
    """INSERT is untouched. Choosing an industry at onboarding is the step this
    product wants owners to take themselves; changing it afterwards is not."""
    with acting_as(conn, OWNER):
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into t_advit.workspaces
                  (org_id, name, industry_key, daily_cap_inr, monthly_cap_inr)
                values (%s, 'A New Ayurveda Workspace', 'ayurveda', 1000, 30000)
                returning industry_key
                """,
                (ORG_BROADMATE,),
            )
            assert cur.fetchone()[0] == "ayurveda"


def test_an_ordinary_member_cannot_reach_the_column_at_all(conn):
    """Belt and braces: workspaces_write already requires owner or admin, so a
    media buyer's update matches no rows rather than being refused by the
    trigger. Asserted so a future widening of that policy is not silent."""
    with acting_as(conn, MEMBER):
        with conn.cursor() as cur:
            cur.execute(
                "update t_advit.workspaces set daily_cap_inr = 1 where id = %s",
                (WORKSPACE_AYURVEDA,),
            )
            assert cur.rowcount == 0


def test_an_outsider_cannot_see_the_workspace_let_alone_its_pack(conn):
    rows = rows_as(
        conn,
        OUTSIDER,
        "select id from t_advit.workspaces where id = %s",
        (WORKSPACE_AYURVEDA,),
    )
    assert rows == []


def test_the_backend_may_change_the_pack_and_the_change_is_audited(conn):
    """Support must be able to correct a wrong pack, and a change of applicable
    law must be findable afterwards by someone who did not know to look for it.

    A connection with no JWT subject is the backend - the same test
    core.log_audit uses to tell a tenant from the service role.
    """
    before = _audit_count(conn)
    with conn.cursor() as cur:
        cur.execute(
            "update t_advit.workspaces set industry_key = 'general_d2c' where id = %s",
            (WORKSPACE_AYURVEDA,),
        )
        assert cur.rowcount == 1

        cur.execute(
            """
            select payload_json->>'from', payload_json->>'to',
                   actor_type::text, workspace_id::text
              from core.audit_log
             where event = 'workspace.industry_changed'
             order by id desc limit 1
            """
        )
        row = cur.fetchone()
    after = _audit_count(conn)

    assert after == before + 1
    assert row[0] == "ayurveda"
    assert row[1] == "general_d2c"
    assert row[2] == "system"
    assert row[3] == WORKSPACE_AYURVEDA


def test_an_update_that_does_not_touch_the_pack_writes_no_audit_noise(conn):
    """An audit event that fires on every unrelated write is an audit event
    nobody reads."""
    before = _audit_count(conn)
    with conn.cursor() as cur:
        cur.execute(
            "update t_advit.workspaces set is_paused = not is_paused where id = %s",
            (WORKSPACE_AYURVEDA,),
        )
    assert _audit_count(conn) == before


def test_the_guard_function_is_not_executable_by_public(conn):
    """Postgres grants EXECUTE to PUBLIC by default, which is how anon came to
    read entitlements once already."""
    with conn.cursor() as cur:
        cur.execute(
            "select has_function_privilege('public', "
            "'t_advit.guard_workspace_industry()', 'execute')"
        )
        assert cur.fetchone()[0] is False
