"""20260918000002: money moves the subscription, and nothing else does.

Two halves. The first is the payment row's life under the real tenant role:
an owner opens a request for an issued invoice and gets the invoice's total;
a member is refused by name and an outsider sees nothing; a second request
is the same request; a reference submitted in time is recorded and one
submitted late is refused; the operator's approval pays the invoice and
restores the subscription from wherever it had sunk; a rejection needs a
reason; and no session can reach the table with an UPDATE or reach the
review function without being an operator.

The second is the settle walk over a fixture organisation that only this
file knows about: every arrow of the state machine drawn in 20260903000001,
driven by p_now rather than by waiting, narrowed by p_org so the seeded
organisations do not move with it. A draft invoice moves nobody. The tenant
role cannot run the walk. Every step it takes is a row in the trail.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import psycopg
import pytest
from psycopg.errors import CheckViolation, InsufficientPrivilege, UniqueViolation
from psycopg.rows import dict_row

from conftest import (
    DATABASE_URL,
    MEMBER,
    ORG_BROADMATE,
    OUTSIDER,
    OWNER,
    PLAN_STANDARD,
    PRODUCT_MARKETING,
    SUB_BROADMATE,
    SUPERADMIN,
    acting_as,
    as_tenant,
)

MARK = "payments-settlement-test"


@contextmanager
def refused(cur, exc_type, hint: str | None = None):
    """A refusal inside an open transaction, without poisoning it - the same
    helper test_operator_console carries, for the same reason."""
    cur.execute("savepoint probe")
    with pytest.raises(exc_type) as exc:
        yield
    cur.execute("rollback to savepoint probe")
    if hint is not None:
        assert exc.value.diag.message_hint == hint, exc.value.diag.message_hint


def superuser():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


# ---------------------------------------------------------------------------
# Fixtures. The tenant tests run over `advit_tenant`, a separate connection,
# so what they need has to be COMMITTED by the superuser and scrubbed by
# marker afterwards - payments before invoices, because the foreign key is
# RESTRICT on purpose.
# ---------------------------------------------------------------------------


def _scrub():
    with superuser() as conn, conn.cursor() as cur:
        cur.execute("delete from core.payments where invoice_id in "
                    "(select id from core.invoices where coupon_code = %s)", (MARK,))
        cur.execute("delete from core.invoices where coupon_code = %s", (MARK,))
        # The seed's own expression for Broadmate's subscription, not a
        # remembered value: active, this calendar month, nothing pending.
        cur.execute(
            """
            update core.subscriptions
               set status = 'active', grace_ends_at = null, trial_ends_at = null,
                   current_period_start = date_trunc('month', now()),
                   current_period_end   = date_trunc('month', now()) + interval '1 month'
             where id = %s::uuid
            """,
            (SUB_BROADMATE,),
        )
        cur.execute("alter table core.audit_log disable trigger audit_log_no_delete")
        cur.execute("delete from core.audit_log where org_id in "
                    "(select id from core.organisations where slug like %s)", (MARK + "%",))
        cur.execute("alter table core.audit_log enable trigger audit_log_no_delete")
        cur.execute("delete from core.organisations where slug like %s", (MARK + "%",))
        conn.commit()


@pytest.fixture(autouse=True)
def clean():
    _scrub()
    yield
    _scrub()


def _issue_invoice(org: str, subscription: str, *, total: str = "17700.00",
                   status: str = "issued", period_start: date | None = None,
                   due_at: datetime | None = None) -> str:
    """An invoice as raise_invoices would write it, as superuser. A draft has
    no number (invoices_issued_have_number) and no issue or due date."""
    start = period_start or date.today().replace(day=1)
    issued = status != "draft"
    with superuser() as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into core.invoices
              (org_id, subscription_id, number, status, period_start, period_end,
               plan_key, plan_name, list_price_inr, coupon_code, percent_off, discount_inr,
               taxable_inr, gst_rate_percent, sac_code, gst_split, cgst_inr, sgst_inr, igst_inr,
               total_inr, issued_at, due_at)
            values
              (%(org)s::uuid, %(sub)s::uuid,
               case when %(issued)s then 'TST-' || substr(gen_random_uuid()::text, 1, 12) end,
               %(status)s::core.invoice_status, %(start)s, %(start)s + interval '1 month',
               'standard', 'Legacy default', 15000.00, %(mark)s, 0, 0,
               15000.00, 18.00, '998314', 'cgst_sgst', 1350.00, 1350.00, 0,
               %(total)s,
               case when %(issued)s then now() end,
               case when %(issued)s then coalesce(%(due)s, now() + interval '7 days') end)
            returning id::text
            """,
            {"org": org, "sub": subscription, "issued": issued, "status": status,
             "start": start, "mark": MARK, "total": Decimal(total), "due": due_at},
        )
        invoice_id = cur.fetchone()["id"]
        conn.commit()
    return invoice_id


@pytest.fixture
def invoice() -> str:
    return _issue_invoice(ORG_BROADMATE, SUB_BROADMATE)


def _request(cur, invoice_id: str, hours: int = 4) -> dict:
    cur.execute(
        "select (p).id::text as id, (p).status::text as status, (p).amount_inr, (p).window_ends_at, "
        "(p).org_id::text as org_id from core.request_payment(%s::uuid, %s) p",
        (invoice_id, hours),
    )
    return cur.fetchone()


def _tenant(dsn_conn):
    dsn_conn.row_factory = dict_row
    return dsn_conn


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


def test_an_owner_requests_a_payment_for_an_issued_invoice_and_gets_its_total(tenant_conn, invoice):
    with as_tenant(_tenant(tenant_conn), OWNER), tenant_conn.cursor() as cur:
        row = _request(cur, invoice)
        assert row["status"] == "awaiting_payment"
        assert row["amount_inr"] == Decimal("17700.00")
        assert row["org_id"] == ORG_BROADMATE
        cur.execute("select window_ends_at > now() + interval '3 hours 59 minutes' as open "
                    "from core.payments where id = %s::uuid", (row["id"],))
        assert cur.fetchone()["open"] is True
        cur.execute(
            "select actor_id::text as actor, payload_json->>'payment_id' as payment from core.audit_log "
            "where event = 'payment.requested' and org_id = %s::uuid order by id desc limit 1",
            (ORG_BROADMATE,),
        )
        trail = cur.fetchone()
        assert trail["actor"] == OWNER and trail["payment"] == row["id"]


def test_a_member_is_refused_by_name_and_an_outsider_sees_no_row(tenant_conn, invoice):
    """A member belongs to the organisation and may be told it is not their
    call. An outsider is not told the invoice exists: the function says 'not
    found' and the table shows nothing."""
    with as_tenant(_tenant(tenant_conn), MEMBER), tenant_conn.cursor() as cur:
        with refused(cur, InsufficientPrivilege, "not_allowed"):
            _request(cur, invoice)

    with as_tenant(tenant_conn, OUTSIDER), tenant_conn.cursor() as cur:
        with refused(cur, InsufficientPrivilege, "invoice_unknown"):
            _request(cur, invoice)

    with as_tenant(tenant_conn, OWNER), tenant_conn.cursor() as cur:
        _request(cur, invoice)
    with as_tenant(tenant_conn, OUTSIDER), tenant_conn.cursor() as cur:
        cur.execute("select count(*) as n from core.payments where invoice_id = %s::uuid", (invoice,))
        assert cur.fetchone()["n"] == 0
    with as_tenant(tenant_conn, MEMBER), tenant_conn.cursor() as cur:
        cur.execute("select count(*) as n from core.payments where invoice_id = %s::uuid", (invoice,))
        assert cur.fetchone()["n"] == 0, "a member does not see what the company is billed"


def test_requesting_twice_returns_the_same_awaiting_row(tenant_conn, invoice):
    with as_tenant(_tenant(tenant_conn), OWNER), tenant_conn.cursor() as cur:
        first = _request(cur, invoice)
        second = _request(cur, invoice)
        assert first["id"] == second["id"]
        assert first["window_ends_at"] == second["window_ends_at"], "a reload is not a second deadline"
        cur.execute("select count(*) as n from core.payments where invoice_id = %s::uuid", (invoice,))
        assert cur.fetchone()["n"] == 1


@pytest.mark.parametrize("status", ["draft", "paid", "void"])
def test_only_an_issued_invoice_is_payable(tenant_conn, status):
    invoice = _issue_invoice(ORG_BROADMATE, SUB_BROADMATE, status=status)
    with as_tenant(_tenant(tenant_conn), OWNER), tenant_conn.cursor() as cur:
        with refused(cur, CheckViolation, "invoice_not_payable"):
            _request(cur, invoice)


def test_a_window_of_nothing_is_refused(tenant_conn, invoice):
    with as_tenant(_tenant(tenant_conn), OWNER), tenant_conn.cursor() as cur:
        with refused(cur, CheckViolation, "window_invalid"):
            _request(cur, invoice, hours=0)


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------


def test_submitting_within_the_window_records_the_reference(tenant_conn, invoice):
    with as_tenant(_tenant(tenant_conn), OWNER), tenant_conn.cursor() as cur:
        payment = _request(cur, invoice)
        cur.execute(
            "select (p).status::text as status, (p).method, (p).reference, (p).paid_on, "
            "(p).submitted_by::text as by, (p).submitted_at "
            "from core.submit_payment(%s::uuid, 'upi', '  UTR123456789012  ', %s) p",
            (payment["id"], date.today() - timedelta(days=1)),
        )
        row = cur.fetchone()
        assert row["status"] == "submitted"
        assert row["method"] == "upi"
        assert row["reference"] == "UTR123456789012", "trimmed, so a pasted space does not fail the match"
        assert row["by"] == OWNER and row["submitted_at"] is not None
        cur.execute(
            "select payload_json->>'reference' as ref from core.audit_log "
            "where event = 'payment.submitted' and org_id = %s::uuid order by id desc limit 1",
            (ORG_BROADMATE,),
        )
        assert cur.fetchone()["ref"] == "UTR123456789012"


def test_a_reference_that_is_not_a_reference_is_refused(tenant_conn, invoice):
    with as_tenant(_tenant(tenant_conn), OWNER), tenant_conn.cursor() as cur:
        payment = _request(cur, invoice)
        with refused(cur, CheckViolation, "reference_invalid"):
            cur.execute("select core.submit_payment(%s::uuid, 'upi', ' pd ')", (payment["id"],))
        with refused(cur, CheckViolation, "method_unknown"):
            cur.execute("select core.submit_payment(%s::uuid, 'cash', 'UTR123456789012')", (payment["id"],))
        with refused(cur, CheckViolation, "paid_on_future"):
            cur.execute("select core.submit_payment(%s::uuid, 'upi', 'UTR123456789012', %s)",
                        (payment["id"], date.today() + timedelta(days=3)))


def test_submitting_after_the_window_is_refused_and_the_row_is_then_expired(tenant_conn, invoice):
    """The refusal is an exception, and an exception rolls back what the
    function wrote - so the row is closed by core.expire_payment in a call
    the refusal does not undo, which is what the runtime does on
    window_elapsed."""
    with as_tenant(_tenant(tenant_conn), OWNER), tenant_conn.cursor() as cur:
        payment = _request(cur, invoice)
    # Committed, so the superuser can move the deadline on a row that exists
    # outside this connection's transaction. The scrub removes it by marker.
    tenant_conn.commit()
    with superuser() as conn, conn.cursor() as cur:
        cur.execute("update core.payments set window_ends_at = now() - interval '1 minute' where id = %s::uuid",
                    (payment["id"],))
        conn.commit()

    with as_tenant(tenant_conn, OWNER), tenant_conn.cursor() as cur:
        with refused(cur, CheckViolation, "window_elapsed"):
            cur.execute("select core.submit_payment(%s::uuid, 'upi', 'UTR123456789012')", (payment["id"],))
        cur.execute("select (p).status::text as status from core.expire_payment(%s::uuid) p", (payment["id"],))
        assert cur.fetchone()["status"] == "expired"
        with refused(cur, CheckViolation, "payment_not_awaiting"):
            cur.execute("select core.submit_payment(%s::uuid, 'upi', 'UTR123456789012')", (payment["id"],))
        # And the organisation may ask again: the lapsed row no longer holds
        # the one-open-per-invoice slot.
        fresh = _request(cur, invoice)
        assert fresh["id"] != payment["id"] and fresh["status"] == "awaiting_payment"


def test_a_session_cannot_close_a_window_that_is_still_open(tenant_conn, invoice):
    """expire_payment honours a clock argument for the backend only. An owner
    passing a future p_now gets the real clock and an unchanged row."""
    with as_tenant(_tenant(tenant_conn), OWNER), tenant_conn.cursor() as cur:
        payment = _request(cur, invoice)
        cur.execute("select (p).status::text as status from core.expire_payment(%s::uuid, now() + interval '2 days') p",
                    (payment["id"],))
        assert cur.fetchone()["status"] == "awaiting_payment"


def test_a_second_request_while_one_is_submitted_is_refused_payment_open(tenant_conn, invoice):
    with as_tenant(_tenant(tenant_conn), OWNER), tenant_conn.cursor() as cur:
        payment = _request(cur, invoice)
        cur.execute("select core.submit_payment(%s::uuid, 'bank_transfer', 'NEFT0001234567')", (payment["id"],))
        with refused(cur, UniqueViolation, "payment_open"):
            _request(cur, invoice)


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


def _submitted(tenant_conn, invoice: str) -> str:
    with as_tenant(_tenant(tenant_conn), OWNER), tenant_conn.cursor() as cur:
        payment = _request(cur, invoice)
        cur.execute("select core.submit_payment(%s::uuid, 'upi', 'UTR123456789012')", (payment["id"],))
    return payment["id"]


def test_approval_pays_the_invoice_and_restores_a_past_due_subscription(tenant_conn, invoice):
    """Started as past_due with a grace deadline, so the restore is proved
    rather than a no-op on an already-active row."""
    payment = _submitted(tenant_conn, invoice)
    with superuser() as conn, conn.cursor() as cur:
        cur.execute("update core.subscriptions set status = 'past_due', grace_ends_at = now() + interval '3 days' "
                    "where id = %s::uuid", (SUB_BROADMATE,))
        conn.commit()

    with as_tenant(tenant_conn, SUPERADMIN), tenant_conn.cursor() as cur:
        cur.execute(
            "select (p).status::text as status, (p).reviewed_by::text as by, (p).reviewed_at, (p).review_note "
            "from core.review_payment(%s::uuid, 'approved', '  matched on the 16th  ') p",
            (payment,),
        )
        row = cur.fetchone()
        assert row["status"] == "approved" and row["by"] == SUPERADMIN and row["reviewed_at"] is not None
        assert row["review_note"] == "matched on the 16th"

        cur.execute("select status::text as status, paid_at from core.invoices where id = %s::uuid", (invoice,))
        inv = cur.fetchone()
        assert inv["status"] == "paid" and inv["paid_at"] is not None

        cur.execute("select status::text as status, grace_ends_at from core.subscriptions where id = %s::uuid",
                    (SUB_BROADMATE,))
        sub = cur.fetchone()
        assert sub["status"] == "active" and sub["grace_ends_at"] is None, "money restores access"

        cur.execute(
            "select actor_type::text as actor_type, actor_id::text as actor, "
            "payload_json->'subscription' as sub from core.audit_log "
            "where event = 'payment.approved' and org_id = %s::uuid order by id desc limit 1",
            (ORG_BROADMATE,),
        )
        trail = cur.fetchone()
        assert trail["actor_type"] == "superadmin" and trail["actor"] == SUPERADMIN
        assert trail["sub"] == {"from": "past_due", "to": "active"}


def test_approval_leaves_an_active_subscription_active(tenant_conn, invoice):
    payment = _submitted(tenant_conn, invoice)
    with as_tenant(tenant_conn, SUPERADMIN), tenant_conn.cursor() as cur:
        cur.execute("select core.review_payment(%s::uuid, 'approved')", (payment,))
        cur.execute("select status::text as status from core.subscriptions where id = %s::uuid", (SUB_BROADMATE,))
        assert cur.fetchone()["status"] == "active"


def test_a_rejection_needs_a_reason_and_then_closes_the_row(tenant_conn, invoice):
    payment = _submitted(tenant_conn, invoice)
    with as_tenant(tenant_conn, SUPERADMIN), tenant_conn.cursor() as cur:
        with refused(cur, CheckViolation, "reason_required"):
            cur.execute("select core.review_payment(%s::uuid, 'rejected', '   ')", (payment,))
        with refused(cur, CheckViolation, "verdict_invalid"):
            cur.execute("select core.review_payment(%s::uuid, 'expired', 'x')", (payment,))
        cur.execute("select (p).status::text as status, (p).review_note "
                    "from core.review_payment(%s::uuid, 'rejected', 'amount short by 100') p", (payment,))
        row = cur.fetchone()
        assert row["status"] == "rejected" and row["review_note"] == "amount short by 100"
        cur.execute("select status::text as status from core.invoices where id = %s::uuid", (invoice,))
        assert cur.fetchone()["status"] == "issued", "a rejection pays nothing"
        with refused(cur, CheckViolation, "payment_not_submitted"):
            cur.execute("select core.review_payment(%s::uuid, 'approved')", (payment,))

    # The organisation may ask again.
    with as_tenant(tenant_conn, OWNER), tenant_conn.cursor() as cur:
        again = _request(cur, invoice)
        assert again["id"] != payment and again["status"] == "awaiting_payment"


def test_a_tenant_cannot_review_and_cannot_update_the_table(tenant_conn, invoice):
    payment = _submitted(tenant_conn, invoice)
    for user in (OWNER, OUTSIDER):
        with as_tenant(tenant_conn, user), tenant_conn.cursor() as cur:
            with refused(cur, InsufficientPrivilege, "not_superadmin"):
                cur.execute("select core.review_payment(%s::uuid, 'approved')", (payment,))
            with refused(cur, InsufficientPrivilege):
                cur.execute("update core.payments set status = 'approved', reviewed_at = now() where id = %s::uuid",
                            (payment,))
            with refused(cur, InsufficientPrivilege):
                cur.execute("insert into core.payments (org_id, invoice_id, subscription_id, amount_inr, status, "
                            "window_ends_at) values (%s::uuid, %s::uuid, %s::uuid, 1, 'awaiting_payment', now())",
                            (ORG_BROADMATE, invoice, SUB_BROADMATE))


# ---------------------------------------------------------------------------
# The settle walk, over an organisation only this file knows about
# ---------------------------------------------------------------------------

T0 = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)


def _organisation(conn, *, status: str, **sub) -> tuple[str, str]:
    """A marker organisation with one live subscription on the standard plan
    (monthly, 7 grace days). Written on the superuser transaction the test
    holds, so it is rolled back with everything else."""
    with conn.cursor() as cur:
        cur.execute(
            "insert into core.organisations (name, slug, status, state_code, created_by, activated_at) "
            "values (%s, %s, 'active', '09', %s::uuid, now()) returning id::text",
            (MARK, f"{MARK}-{uuid.uuid4().hex[:8]}", SUPERADMIN),
        )
        org = cur.fetchone()["id"]
        columns = {
            "trial_ends_at": None,
            "current_period_start": T0,
            "current_period_end": T0 + timedelta(days=30),
            "grace_ends_at": None,
        }
        columns.update(sub)
        cur.execute(
            """
            insert into core.subscriptions
              (org_id, product_id, plan_id, status, trial_ends_at,
               current_period_start, current_period_end, grace_ends_at)
            values (%s::uuid, %s::uuid, %s::uuid, %s::core.subscription_status, %s, %s, %s, %s)
            returning id::text
            """,
            (org, PRODUCT_MARKETING, PLAN_STANDARD, status, columns["trial_ends_at"],
             columns["current_period_start"], columns["current_period_end"], columns["grace_ends_at"]),
        )
        return org, cur.fetchone()["id"]


def _settle(conn, org: str, now: datetime) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("select from_status, to_status, reason from core.settle_subscriptions(%s, %s::uuid)", (now, org))
        return cur.fetchall()


def _subscription(conn, sub: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("select status::text as status, trial_ends_at, current_period_start, current_period_end, "
                    "grace_ends_at from core.subscriptions where id = %s::uuid", (sub,))
        return cur.fetchone()


def _settled_events(conn, org: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("select actor_type::text as actor_type, actor_id, payload_json as payload from core.audit_log "
                    "where event = 'subscription.settled' and org_id = %s::uuid order by id", (org,))
        return cur.fetchall()


@pytest.fixture
def db(conn):
    conn.row_factory = dict_row
    return conn


def test_a_trial_that_has_ended_becomes_pending_payment_with_the_plans_period(db):
    trial_end = T0 + timedelta(days=14)
    org, sub = _organisation(db, status="trialing", trial_ends_at=trial_end,
                             current_period_start=T0, current_period_end=trial_end)
    assert _settle(db, org, trial_end - timedelta(hours=1)) == [], "not before the trial ends"
    moved = _settle(db, org, trial_end)
    assert moved == [{"from_status": "trialing", "to_status": "pending_payment", "reason": "trial ended"}]
    after = _subscription(db, sub)
    assert after["status"] == "pending_payment"
    assert after["current_period_start"] == trial_end
    assert after["current_period_end"] == datetime(2026, 10, 15, tzinfo=timezone.utc), "monthly: one calendar month"
    assert _settle(db, org, trial_end) == [], "one transition per call, and nothing is due yet"


def test_an_active_period_that_has_ended_rolls_forward_and_becomes_pending_payment(db):
    end = datetime(2026, 10, 1, tzinfo=timezone.utc)
    org, sub = _organisation(db, status="active", current_period_start=T0, current_period_end=end)
    assert _settle(db, org, end - timedelta(seconds=1)) == []
    assert _settle(db, org, end) == [{"from_status": "active", "to_status": "pending_payment", "reason": "period ended"}]
    after = _subscription(db, sub)
    assert after["current_period_start"] == end
    assert after["current_period_end"] == datetime(2026, 11, 1, tzinfo=timezone.utc)


def test_a_yearly_plan_rolls_by_a_year(db):
    with db.cursor() as cur:
        cur.execute("update core.plans set billing_period = 'yearly' where id = %s::uuid", (PLAN_STANDARD,))
    end = datetime(2026, 10, 1, tzinfo=timezone.utc)
    org, sub = _organisation(db, status="active", current_period_end=end)
    _settle(db, org, end)
    assert _subscription(db, sub)["current_period_end"] == datetime(2027, 10, 1, tzinfo=timezone.utc)


def test_pending_payment_with_an_issued_invoice_past_due_becomes_past_due_with_grace_from_the_due_date(db):
    org, sub = _organisation(db, status="pending_payment")
    due = T0 + timedelta(days=7)
    with db.cursor() as cur:
        cur.execute(
            """
            insert into core.invoices
              (org_id, subscription_id, number, status, period_start, period_end, plan_key, plan_name,
               list_price_inr, coupon_code, taxable_inr, gst_rate_percent, sac_code, gst_split,
               cgst_inr, sgst_inr, total_inr, issued_at, due_at)
            values (%s::uuid, %s::uuid, 'TST-' || substr(gen_random_uuid()::text, 1, 12), 'issued',
                    %s, %s, 'standard', 'Legacy default', 15000, %s, 15000, 18, '998314', 'cgst_sgst',
                    1350, 1350, 17700, %s, %s)
            """,
            (org, sub, T0.date(), (T0 + timedelta(days=30)).date(), MARK, T0, due),
        )
    assert _settle(db, org, due - timedelta(minutes=1)) == [], "not before the due date"
    assert _settle(db, org, due) == [{"from_status": "pending_payment", "to_status": "past_due", "reason": "invoice overdue"}]
    after = _subscription(db, sub)
    assert after["status"] == "past_due"
    assert after["grace_ends_at"] == due + timedelta(days=7), "grace_days on the standard plan is 7"


def test_a_draft_invoice_never_makes_anyone_past_due(db):
    """A draft is a bill the platform could not send. Full access stays."""
    org, sub = _organisation(db, status="pending_payment")
    with db.cursor() as cur:
        cur.execute(
            """
            insert into core.invoices
              (org_id, subscription_id, number, status, period_start, period_end, plan_key, plan_name,
               list_price_inr, coupon_code, taxable_inr, gst_rate_percent, sac_code, gst_split, total_inr)
            values (%s::uuid, %s::uuid, null, 'draft', %s, %s, 'standard', 'Legacy default',
                    15000, %s, 15000, 18, '998314', 'igst', 15000)
            """,
            (org, sub, T0.date(), (T0 + timedelta(days=30)).date(), MARK),
        )
    assert _settle(db, org, T0 + timedelta(days=60)) == []
    assert _subscription(db, sub)["status"] == "pending_payment"
    with acting_as(db, SUPERADMIN), db.cursor() as cur:
        cur.execute("select core.access_mode(%s::uuid, %s::uuid)::text as mode", (org, PRODUCT_MARKETING))
        assert cur.fetchone()["mode"] == "full"


def test_past_due_past_its_grace_becomes_expired_and_access_narrows_at_each_step(db):
    grace_end = T0 + timedelta(days=14)
    org, sub = _organisation(db, status="past_due", grace_ends_at=grace_end)
    with acting_as(db, SUPERADMIN), db.cursor() as cur:
        cur.execute("select core.access_mode(%s::uuid, %s::uuid)::text as mode", (org, PRODUCT_MARKETING))
        assert cur.fetchone()["mode"] == "read_only"

    assert _settle(db, org, grace_end - timedelta(hours=1)) == []
    assert _settle(db, org, grace_end) == [{"from_status": "past_due", "to_status": "expired", "reason": "grace ended"}]
    assert _subscription(db, sub)["status"] == "expired"
    with acting_as(db, SUPERADMIN), db.cursor() as cur:
        cur.execute("select core.access_mode(%s::uuid, %s::uuid)::text as mode", (org, PRODUCT_MARKETING))
        assert cur.fetchone()["mode"] == "denied"
    assert _settle(db, org, grace_end + timedelta(days=365)) == [], "expired has nowhere further to go"


def test_a_trial_with_no_end_date_ends_when_its_period_does(db):
    """The guard with nothing to compare against: PATCH .../subscription can
    set trialing without stamping trial_ends_at. A null deadline is a missing
    one, not a longer trial - the period end is the deadline the row carries."""
    end = T0 + timedelta(days=14)
    org, sub = _organisation(db, status="trialing", trial_ends_at=None,
                             current_period_start=T0, current_period_end=end)
    assert _settle(db, org, end - timedelta(hours=1)) == []
    assert _settle(db, org, end) == [{"from_status": "trialing", "to_status": "pending_payment", "reason": "trial ended"}]
    after = _subscription(db, sub)
    assert after["current_period_start"] == end
    assert after["current_period_end"] == datetime(2026, 10, 15, tzinfo=timezone.utc), "monthly, from the period end"


def test_past_due_with_no_grace_clock_starts_one_rather_than_staying_read_only_forever(db):
    """An operator set past_due by hand and stamped no grace_ends_at. Read-only
    forever is not what past_due means; the walk starts the clock - from the
    overdue invoice's due date when there is one - and reports that it did."""
    org, sub = _organisation(db, status="past_due", grace_ends_at=None)
    at = T0 + timedelta(days=20)
    assert _settle(db, org, at) == [{"from_status": "past_due", "to_status": "past_due", "reason": "grace clock started"}]
    after = _subscription(db, sub)
    assert after["status"] == "past_due"
    assert after["grace_ends_at"] == at + timedelta(days=7), "standard plan: seven grace days from the walk"
    # And from there the ordinary arrow: past its grace, expired.
    assert _settle(db, org, at + timedelta(days=7)) == [
        {"from_status": "past_due", "to_status": "expired", "reason": "grace ended"}]


def test_a_period_boundary_is_the_ist_day_to_both_the_walk_and_the_invoice(db):
    """core.period_date: a boundary at 22:00 UTC is the NEXT day in IST, and
    that day is what an invoice's period_start carries. The walk's past_due
    lookup and raise_invoices' dedupe both go through the function, so a
    non-midnight boundary is one day to both rather than two."""
    with db.cursor() as cur:
        cur.execute("select core.period_date('2026-09-30 22:00:00+00'::timestamptz) as d, "
                    "core.period_date('2026-09-30 18:29:59+00'::timestamptz) as e")
        row = cur.fetchone()
    assert str(row["d"]) == "2026-10-01"
    assert str(row["e"]) == "2026-09-30"
    # A past-due invoice is found by the walk when its period_start is the
    # IST day of a boundary that is not midnight.
    start = datetime(2026, 9, 30, 22, 0, tzinfo=timezone.utc)   # 03:30 IST on 1 Oct
    org, sub = _organisation(db, status="pending_payment",
                             current_period_start=start, current_period_end=start + timedelta(days=30))
    with db.cursor() as cur:
        cur.execute(
            """
            insert into core.invoices
              (org_id, subscription_id, number, status, period_start, period_end, plan_key, plan_name,
               list_price_inr, coupon_code, taxable_inr, gst_rate_percent, sac_code, gst_split, total_inr,
               issued_at, due_at)
            values (%s::uuid, %s::uuid, 'TST-' || substr(gen_random_uuid()::text, 1, 12), 'issued',
                    core.period_date(%s::timestamptz), core.period_date(%s::timestamptz),
                    'standard', 'Legacy default', 15000, %s, 15000, 18, '998314', 'igst', 15000, %s, %s)
            """,
            (org, sub, start, start + timedelta(days=30), MARK, start, start + timedelta(days=7)),
        )
    assert _settle(db, org, start + timedelta(days=7)) == [
        {"from_status": "pending_payment", "to_status": "past_due", "reason": "invoice overdue"}]


def test_a_submitted_payment_under_review_holds_the_walk(db):
    """The reference may already be on the bank statement. The operator's lag
    must not narrow the customer's access."""
    grace_end = T0 + timedelta(days=14)
    org, sub = _organisation(db, status="past_due", grace_ends_at=grace_end)
    with db.cursor() as cur:
        cur.execute(
            """
            insert into core.invoices
              (org_id, subscription_id, number, status, period_start, period_end, plan_key, plan_name,
               list_price_inr, coupon_code, taxable_inr, gst_rate_percent, sac_code, gst_split, total_inr,
               issued_at, due_at)
            values (%s::uuid, %s::uuid, 'TST-' || substr(gen_random_uuid()::text, 1, 12), 'issued', %s, %s,
                    'standard', 'Legacy default', 15000, %s, 15000, 18, '998314', 'igst', 15000, %s, %s)
            returning id
            """,
            (org, sub, T0.date(), (T0 + timedelta(days=30)).date(), MARK, T0, T0 + timedelta(days=7)),
        )
        invoice = cur.fetchone()["id"]
        cur.execute(
            "insert into core.payments (org_id, invoice_id, subscription_id, amount_inr, status, method, reference, "
            "window_ends_at, submitted_at) values (%s::uuid, %s::uuid, %s::uuid, 15000, 'submitted', 'upi', "
            "'UTR123456789012', %s, %s)",
            (org, invoice, sub, T0 + timedelta(hours=4), T0 + timedelta(hours=1)),
        )
    assert _settle(db, org, grace_end + timedelta(days=1)) == []
    assert _subscription(db, sub)["status"] == "past_due"


def test_the_walk_closes_lapsed_payment_windows_and_leaves_open_ones(db):
    org, sub = _organisation(db, status="pending_payment")
    with db.cursor() as cur:
        cur.execute(
            """
            insert into core.invoices
              (org_id, subscription_id, number, status, period_start, period_end, plan_key, plan_name,
               list_price_inr, coupon_code, taxable_inr, gst_rate_percent, sac_code, gst_split, total_inr,
               issued_at, due_at)
            values (%s::uuid, %s::uuid, 'TST-' || substr(gen_random_uuid()::text, 1, 12), 'issued', %s, %s,
                    'standard', 'Legacy default', 15000, %s, 15000, 18, '998314', 'igst', 15000, %s, %s)
            returning id
            """,
            (org, sub, T0.date(), (T0 + timedelta(days=30)).date(), MARK, T0, T0 + timedelta(days=7)),
        )
        invoice = cur.fetchone()["id"]
        cur.execute(
            "insert into core.payments (org_id, invoice_id, subscription_id, amount_inr, status, window_ends_at) "
            "values (%s::uuid, %s::uuid, %s::uuid, 15000, 'awaiting_payment', %s) returning id",
            (org, invoice, sub, T0 + timedelta(hours=4)),
        )
        payment = cur.fetchone()["id"]

    _settle(db, org, T0 + timedelta(hours=3))
    with db.cursor() as cur:
        cur.execute("select status::text as status from core.payments where id = %s", (payment,))
        assert cur.fetchone()["status"] == "awaiting_payment", "the window is still open at three hours"

    _settle(db, org, T0 + timedelta(hours=4))
    with db.cursor() as cur:
        cur.execute("select status::text as status from core.payments where id = %s", (payment,))
        assert cur.fetchone()["status"] == "expired"
        cur.execute("select actor_type::text as actor_type, actor_id from core.audit_log "
                    "where event = 'payment.expired' and org_id = %s::uuid", (org,))
        rows = cur.fetchall()
        assert rows == [{"actor_type": "system", "actor_id": None}]


def test_the_seeded_organisations_do_not_move_when_the_walk_is_narrowed(db):
    org, _ = _organisation(db, status="active", current_period_start=T0 - timedelta(days=30), current_period_end=T0)
    far = datetime(2030, 1, 1, tzinfo=timezone.utc)
    moved = _settle(db, org, far)
    assert len(moved) == 1
    with db.cursor() as cur:
        cur.execute("select status::text as status from core.subscriptions where id = %s::uuid", (SUB_BROADMATE,))
        assert cur.fetchone()["status"] == "active"


def test_every_transition_is_a_subscription_settled_row_by_the_system(db):
    trial_end = T0 + timedelta(days=14)
    org, sub = _organisation(db, status="trialing", trial_ends_at=trial_end,
                             current_period_start=T0, current_period_end=trial_end)
    _settle(db, org, trial_end)
    # Walk the rest of the machine by hand-issued invoices and the clock.
    with db.cursor() as cur:
        cur.execute(
            """
            insert into core.invoices
              (org_id, subscription_id, number, status, period_start, period_end, plan_key, plan_name,
               list_price_inr, coupon_code, taxable_inr, gst_rate_percent, sac_code, gst_split, total_inr,
               issued_at, due_at)
            values (%s::uuid, %s::uuid, 'TST-' || substr(gen_random_uuid()::text, 1, 12), 'issued', %s, %s,
                    'standard', 'Legacy default', 15000, %s, 15000, 18, '998314', 'igst', 15000, %s, %s)
            """,
            (org, sub, trial_end.date(), (trial_end + timedelta(days=30)).date(), MARK,
             trial_end, trial_end + timedelta(days=7)),
        )
    _settle(db, org, trial_end + timedelta(days=7))
    _settle(db, org, trial_end + timedelta(days=14))

    events = _settled_events(db, org)
    assert [(e["payload"]["from"], e["payload"]["to"]) for e in events] == [
        ("trialing", "pending_payment"),
        ("pending_payment", "past_due"),
        ("past_due", "expired"),
    ]
    assert all(e["actor_type"] == "system" and e["actor_id"] is None for e in events)
    assert all(e["payload"]["reason"] for e in events)


def test_the_tenant_role_cannot_run_the_walk(tenant_conn):
    for user in (OWNER, SUPERADMIN):
        with as_tenant(tenant_conn, user), tenant_conn.cursor() as cur:
            with refused(cur, InsufficientPrivilege):
                cur.execute("select * from core.settle_subscriptions()")


def test_the_walk_refuses_without_a_clock(db):
    with db.cursor() as cur:
        with refused(cur, CheckViolation, "now_required"):
            cur.execute("select * from core.settle_subscriptions(null)")
