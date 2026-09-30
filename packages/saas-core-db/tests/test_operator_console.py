"""20260917000001: three acts only a person at the platform may perform.

The property under test is the one the migration header states: the verdict on
an organisation is not writable by the party it is about, and a compliance
rule's pattern is not writable by anybody with a session - only its as_of, only
by a superadmin, only forwards.

Every test here runs over the real ``advit_tenant`` login role with
``set local role authenticated``, because column grants are what most of this
migration is, and a superuser connection does not have column grants.
"""

from __future__ import annotations

from datetime import date, timedelta

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege

from contextlib import contextmanager

from conftest import ORG_BROADMATE, ORG_RIVAL, OUTSIDER, OWNER, SUPERADMIN, as_tenant


@contextmanager
def refused(cur, exc_type, hint: str | None = None):
    """A refusal inside an open transaction, without poisoning it.

    The statement under test raises; the savepoint is rolled back so the
    ``reset role`` in ``as_tenant``'s finally still runs on a live transaction.
    """
    cur.execute("savepoint probe")
    with pytest.raises(exc_type) as exc:
        yield
    cur.execute("rollback to savepoint probe")
    if hint is not None:
        assert exc.value.diag.message_hint == hint, exc.value.diag.message_hint


# ---------------------------------------------------------------------------
# 1. Organisation status is the operator's
# ---------------------------------------------------------------------------


def test_an_owner_can_no_longer_write_their_own_organisation_status(tenant_conn):
    """The defect: organisations_update_admin let an owner UPDATE status, and
    core.access_mode reads status. A suspended customer could un-suspend
    itself. Now the column is not in the tenant's grant at all, so the
    statement is refused before any policy runs."""
    with as_tenant(tenant_conn, OWNER), tenant_conn.cursor() as cur:
        with refused(cur, InsufficientPrivilege):
            cur.execute("update core.organisations set status = 'active' where id = %s", (ORG_BROADMATE,))


def test_an_owner_still_edits_the_billing_identity(tenant_conn):
    """The GST invoice needs the customer to fill these in themselves; the
    narrowing must not take that away."""
    with as_tenant(tenant_conn, OWNER), tenant_conn.cursor() as cur:
        cur.execute(
            "update core.organisations set legal_name = %s, state_code = '27' where id = %s returning legal_name",
            ("Broadmate Global Pvt Ltd", ORG_BROADMATE),
        )
        assert cur.fetchone()[0] == "Broadmate Global Pvt Ltd"


def test_the_status_function_refuses_everyone_but_a_superadmin(tenant_conn):
    for user in (OWNER, OUTSIDER):
        with as_tenant(tenant_conn, user), tenant_conn.cursor() as cur:
            with refused(cur, InsufficientPrivilege, "not_superadmin"):
                cur.execute("select core.set_organisation_status(%s, 'suspended', 'test')", (ORG_BROADMATE,))


def test_a_superadmin_suspends_with_a_reason_and_it_is_in_the_trail(tenant_conn):
    with as_tenant(tenant_conn, SUPERADMIN), tenant_conn.cursor() as cur:
        cur.execute(
            "select (o).status::text, (o).suspension_reason, (o).suspended_at is not null "
            "from core.set_organisation_status(%s, 'suspended', '  non-payment  ') o",
            (ORG_RIVAL,),
        )
        status, reason, stamped = cur.fetchone()
        assert (status, reason, stamped) == ("suspended", "non-payment", True)

        cur.execute(
            "select actor_id::text, payload_json->>'status' from core.audit_log "
            "where event = 'organisation.suspended' and org_id = %s order by id desc limit 1",
            (ORG_RIVAL,),
        )
        actor, payload_status = cur.fetchone()
        assert actor == SUPERADMIN and payload_status == "suspended"


def test_a_suspension_without_a_reason_is_refused(tenant_conn):
    with as_tenant(tenant_conn, SUPERADMIN), tenant_conn.cursor() as cur:
        with refused(cur, psycopg.errors.CheckViolation, "reason_required"):
            cur.execute("select core.set_organisation_status(%s, 'suspended', '   ')", (ORG_RIVAL,))


def test_reactivation_clears_the_suspension_and_keeps_the_first_activation(tenant_conn):
    with as_tenant(tenant_conn, SUPERADMIN), tenant_conn.cursor() as cur:
        cur.execute("select (o).activated_at from core.set_organisation_status(%s, 'suspended', 'x') o", (ORG_RIVAL,))
        first = cur.fetchone()[0]
        cur.execute(
            "select (o).status::text, (o).suspended_at, (o).suspension_reason, (o).activated_at "
            "from core.set_organisation_status(%s, 'active') o",
            (ORG_RIVAL,),
        )
        status, suspended_at, reason, activated_at = cur.fetchone()
        assert status == "active" and suspended_at is None and reason is None
        assert activated_at == first


def test_the_status_the_function_writes_is_the_one_access_mode_reads(tenant_conn):
    """The reason the column left the tenant's grant. Suspended is 'denied'
    to the organisation's own owner, and only the operator can lift it."""
    with as_tenant(tenant_conn, SUPERADMIN), tenant_conn.cursor() as cur:
        cur.execute("select core.set_organisation_status(%s, 'suspended', 'test')", (ORG_BROADMATE,))
    with as_tenant(tenant_conn, OWNER), tenant_conn.cursor() as cur:
        cur.execute("select core.access_mode(%s, (select id from core.products where key = 'advit'))::text",
                    (ORG_BROADMATE,))
        assert cur.fetchone()[0] == "denied"
    with as_tenant(tenant_conn, SUPERADMIN), tenant_conn.cursor() as cur:
        cur.execute("select core.set_organisation_status(%s, 'active')", (ORG_BROADMATE,))
    with as_tenant(tenant_conn, OWNER), tenant_conn.cursor() as cur:
        cur.execute("select core.access_mode(%s, (select id from core.products where key = 'advit'))::text",
                    (ORG_BROADMATE,))
        assert cur.fetchone()[0] == "full"


# ---------------------------------------------------------------------------
# 2. A rule is re-verified by a person - as_of only, forwards only
# ---------------------------------------------------------------------------


def _a_rule(conn) -> tuple[str, date]:
    """Read as the superadmin: the bare NOINHERIT login role can see nothing,
    which test_runtime_roles asserts on purpose."""
    with as_tenant(conn, SUPERADMIN), conn.cursor() as cur:
        cur.execute("select code, as_of from t_advit.policy_rules where jurisdiction = 'meta' order by code limit 1")
        return cur.fetchone()


def test_a_superadmin_moves_as_of_forward_and_it_is_in_the_trail(tenant_conn):
    code, before = _a_rule(tenant_conn)
    today = date.today()
    assert before < today, "the seed's Meta rules are dated in the past by design"

    with as_tenant(tenant_conn, SUPERADMIN), tenant_conn.cursor() as cur:
        cur.execute("update t_advit.policy_rules set as_of = %s where code = %s returning as_of", (today, code))
        assert cur.fetchone()[0] == today
        cur.execute(
            "select actor_id::text, payload_json->>'subject', payload_json->>'previous_as_of' "
            "from core.audit_log where event = 'rule.reverified' order by id desc limit 1"
        )
        actor, subject, previous = cur.fetchone()
        assert actor == SUPERADMIN and subject == code and previous == before.isoformat()


def test_a_tenant_cannot_reverify_a_rule(tenant_conn):
    """RLS on UPDATE: no policy lets an owner through, so zero rows change and
    the rule stays exactly as dated."""
    code, before = _a_rule(tenant_conn)
    with as_tenant(tenant_conn, OWNER), tenant_conn.cursor() as cur:
        cur.execute("update t_advit.policy_rules set as_of = %s where code = %s", (date.today(), code))
        assert cur.rowcount == 0
    assert _a_rule(tenant_conn) == (code, before)


def test_nobody_with_a_session_can_touch_the_pattern(tenant_conn):
    """The Platform Watch contract: detect, never act. A console is a website;
    the column grant names as_of and nothing else, so this is refused by the
    privilege check before RLS is consulted - for the superadmin too."""
    code, _ = _a_rule(tenant_conn)
    for user in (SUPERADMIN, OWNER):
        with as_tenant(tenant_conn, user), tenant_conn.cursor() as cur:
            for column, value in (("pattern", "'.*'"), ("severity", "'warn'"), ("is_active", "false")):
                with refused(cur, InsufficientPrivilege):
                    cur.execute(f"update t_advit.policy_rules set {column} = {value} where code = %s", (code,))


def test_as_of_cannot_move_into_the_future_or_backwards(tenant_conn):
    code, before = _a_rule(tenant_conn)
    with as_tenant(tenant_conn, SUPERADMIN), tenant_conn.cursor() as cur:
        with refused(cur, psycopg.errors.CheckViolation, "as_of_future"):
            cur.execute("update t_advit.policy_rules set as_of = %s where code = %s",
                        (date.today() + timedelta(days=1), code))
        with refused(cur, psycopg.errors.CheckViolation, "as_of_moves_forward"):
            cur.execute("update t_advit.policy_rules set as_of = %s where code = %s",
                        (before - timedelta(days=1), code))


def test_platform_knowledge_follows_the_same_rule(tenant_conn):
    with as_tenant(tenant_conn, SUPERADMIN), tenant_conn.cursor() as cur:
        cur.execute("select id, as_of from t_advit.platform_knowledge order by as_of limit 1")
        row = cur.fetchone()
    if row is None:
        pytest.skip("no platform_knowledge rows seeded")
    kid, before = row
    with as_tenant(tenant_conn, OWNER), tenant_conn.cursor() as cur:
        cur.execute("update t_advit.platform_knowledge set as_of = current_date where id = %s", (kid,))
        assert cur.rowcount == 0
    with as_tenant(tenant_conn, SUPERADMIN), tenant_conn.cursor() as cur:
        cur.execute("update t_advit.platform_knowledge set as_of = current_date where id = %s returning as_of", (kid,))
        assert cur.fetchone()[0] >= before
        cur.execute("select payload_json->>'subject' from core.audit_log where event = 'knowledge.reverified' "
                    "order by id desc limit 1")
        assert cur.fetchone()[0] == str(kid)

