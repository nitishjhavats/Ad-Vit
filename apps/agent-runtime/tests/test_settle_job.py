"""The settle walk as a platform job: it runs, it reports, and it is
scheduled before the invoices are raised.

The walk itself - every arrow of the state machine - is proved in
packages/saas-core-db/tests/test_payments_and_settlement.py against the SQL
function. What this file adds is the runtime's half: the handler calls the
function on the service connection and hands back what it returned in the
shape the job record stores; a fixture organisation moved through it shows
up in that report and nowhere else; the job is registered at 00:15 IST,
before raise_invoices at 00:30, in the shape test_platform_watch asserts for
its own job.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg.rows import dict_row

from app.billing import settle
from app.jobs import runner

SUPERUSER_DSN = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
SUPERADMIN = "00000000-0000-4000-8000-000000000001"
PRODUCT = "00000000-0000-4000-8000-000000000020"
PLAN_STANDARD = "00000000-0000-4000-8000-000000000030"
MARK = "settle-job-test"


def superuser():
    return psycopg.connect(SUPERUSER_DSN, row_factory=dict_row)


@pytest.fixture(autouse=True)
def scrub_marked_organisations():
    """The fixture organisation and its trail, removed before and after. The
    audit rows go first with the append-only trigger held open for exactly
    that statement, as test_admin_onboarding does and for the same reason."""

    def _scrub():
        with superuser() as conn, conn.cursor() as cur:
            cur.execute("alter table core.audit_log disable trigger audit_log_no_delete")
            cur.execute("delete from core.audit_log where org_id in "
                        "(select id from core.organisations where slug like %s)", (MARK + "%",))
            cur.execute("alter table core.audit_log enable trigger audit_log_no_delete")
            cur.execute("delete from core.organisations where slug like %s", (MARK + "%",))
            conn.commit()

    _scrub()
    yield
    _scrub()


def organisation_with_an_ended_trial() -> tuple[str, str]:
    trial_end = datetime(2026, 9, 1, tzinfo=timezone.utc)
    with superuser() as conn, conn.cursor() as cur:
        cur.execute(
            "insert into core.organisations (name, slug, status, state_code, created_by, activated_at) "
            "values (%s, %s, 'active', '09', %s::uuid, now()) returning id::text",
            (MARK, f"{MARK}-{uuid.uuid4().hex[:8]}", SUPERADMIN),
        )
        org = cur.fetchone()["id"]
        cur.execute(
            "insert into core.subscriptions (org_id, product_id, plan_id, status, trial_ends_at, "
            "current_period_start, current_period_end) values (%s::uuid, %s::uuid, %s::uuid, 'trialing', %s, %s, %s) "
            "returning id::text",
            (org, PRODUCT, PLAN_STANDARD, trial_end, trial_end - timedelta(days=14), trial_end),
        )
        sub = cur.fetchone()["id"]
        conn.commit()
    return org, sub


def test_the_job_reports_what_moved_and_how_many():
    org, sub = organisation_with_an_ended_trial()
    report = settle.settle_subscriptions(now=datetime(2026, 9, 2, tzinfo=timezone.utc), org_id=org)
    assert report["count"] == 1
    assert report["transitions"] == [{
        "subscription_id": sub, "org_id": org,
        "from_status": "trialing", "to_status": "pending_payment", "reason": "trial ended",
    }]
    with superuser() as conn, conn.cursor() as cur:
        cur.execute("select status::text as status from core.subscriptions where id = %s::uuid", (sub,))
        assert cur.fetchone()["status"] == "pending_payment"
        cur.execute("select actor_type::text as actor_type from core.audit_log "
                    "where event = 'subscription.settled' and org_id = %s::uuid", (org,))
        assert [r["actor_type"] for r in cur.fetchall()] == ["system"]

    again = settle.settle_subscriptions(now=datetime(2026, 9, 2, tzinfo=timezone.utc), org_id=org)
    assert again == {"transitions": [], "count": 0}, "one transition per subscription per call"


def test_a_trial_that_ends_mid_morning_is_invoiced_once_and_the_dedupe_holds_the_next_night(monkeypatch):
    """The interaction this file owns. The walk sets current_period_start to
    the trial's end - an instant with a time of day - and raise_invoices
    writes a DATE. Compared in the session's UTC, a 10:13 boundary never
    equalled the date the invoice carried, so the next night's INSERT hit
    invoices_one_per_period and aborted the whole run. core.period_date is
    the one definition both sides use now: one invoice, then nothing due."""
    from app.billing import invoices
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "seller_gstin", "09AAACB1234C1ZV")
    monkeypatch.setattr(s, "seller_legal_name", "Broadmate Global")
    monkeypatch.setattr(s, "seller_state_code", "09")

    trial_end = datetime(2026, 9, 1, 10, 13, tzinfo=timezone.utc)   # 15:43 IST on 1 Sep
    with superuser() as conn, conn.cursor() as cur:
        cur.execute(
            "insert into core.organisations (name, slug, status, state_code, created_by, activated_at) "
            "values (%s, %s, 'active', '09', %s::uuid, now()) returning id::text",
            (MARK, f"{MARK}-{uuid.uuid4().hex[:8]}", SUPERADMIN),
        )
        org = cur.fetchone()["id"]
        cur.execute(
            "insert into core.subscriptions (org_id, product_id, plan_id, status, trial_ends_at, "
            "current_period_start, current_period_end) values (%s::uuid, %s::uuid, %s::uuid, 'trialing', %s, %s, %s) "
            "returning id::text",
            (org, PRODUCT, PLAN_STANDARD, trial_end, trial_end - timedelta(days=14), trial_end),
        )
        sub = cur.fetchone()["id"]
        conn.commit()

    first = second = {"issued": [], "drafts": []}
    try:
        settle.settle_subscriptions(now=trial_end, org_id=org)
        first = invoices.raise_invoices(today=trial_end.date())
        mine = [i for i in first["issued"] if i["org_id"] == org]
        assert len(mine) == 1, first
        second = invoices.raise_invoices(today=trial_end.date() + timedelta(days=1))
        assert [i for i in second["issued"] + second["drafts"] if i["org_id"] == org] == [],             "the period was already invoiced; the dedupe must see the same day the INSERT wrote"
        with superuser() as conn, conn.cursor() as cur:
            cur.execute("select period_start, period_end from core.invoices where subscription_id = %s::uuid", (sub,))
            rows = cur.fetchall()
        assert len(rows) == 1
        assert str(rows[0]["period_start"]) == "2026-09-01" and str(rows[0]["period_end"]) == "2026-10-01"
    finally:
        # raise_invoices has no organisation filter: whatever else was due
        # tonight (the seeded subscriptions, another test's fixture) was
        # invoiced too, and those rows are this test's to remove.
        raised = [i["invoice_id"] for run in (first, second) for i in run["issued"] + run["drafts"]]
        with superuser() as conn, conn.cursor() as cur:
            cur.execute("delete from core.payments where org_id = %s::uuid or invoice_id = any(%s::uuid[])",
                        (org, raised))
            cur.execute("delete from core.invoices where org_id = %s::uuid or id = any(%s::uuid[])",
                        (org, raised))
            conn.commit()


def test_without_a_clock_the_job_uses_the_databases_and_returns_a_count():
    """The scheduled call passes nothing. The SQL coalesces the missing clock
    to now() rather than handing the function a NULL it would refuse - and the
    walk over the seeded organisations, which are inside their periods, moves
    nobody."""
    org, _ = organisation_with_an_ended_trial()
    report = settle.settle_subscriptions(org_id=org)
    assert report["count"] == 1 and isinstance(report["transitions"], list)


def test_the_settle_job_is_registered_at_0015_ist_before_raise_invoices():
    names = [j.name for j in runner.PLATFORM_JOBS]
    assert "settle_subscriptions" in names and "raise_invoices" in names
    assert names.index("settle_subscriptions") < names.index("raise_invoices")

    job = next(j for j in runner.PLATFORM_JOBS if j.name == "settle_subscriptions")
    invoices = next(j for j in runner.PLATFORM_JOBS if j.name == "raise_invoices")
    assert (job.local_hour, job.local_minute) == (0, 15)
    assert (job.local_hour, job.local_minute) < (invoices.local_hour, invoices.local_minute), (
        "a period rolled by the walk must be invoiced the same night"
    )
    assert job.timezone == "Asia/Kolkata" and job.description
    assert job.handler is settle.settle_subscriptions


def test_the_settle_job_takes_no_workspace_and_no_positional_input():
    """The same property test_platform_watch asserts: there is nothing a
    caller could hand it."""
    job = next(j for j in runner.PLATFORM_JOBS if j.name == "settle_subscriptions")
    params = inspect.signature(job.handler).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())
    assert "workspace" not in " ".join(params)
