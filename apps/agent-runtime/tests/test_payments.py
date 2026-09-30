"""Paying an invoice without a gateway: the owner's request, the reference,
the operator's match - and what each route refuses.

What is asserted is the DATABASE'S view after each call, alongside the
response: the payment row's state, the invoice's paid stamp, the
subscription's restore. The routes map the SQL functions' hints to status
codes and decide nothing themselves, so the interesting assertions are the
ones a route could get wrong on its own - 503 BEFORE a row is written when
there is nowhere to send money, 200 rather than 201 for a reload, the
expired row after a late submit, and every operator route invisible to a
tenant.

Broadmate's invoices are inserted as superuser with ``coupon_code = MARK``,
the same marker column test_admin_console scrubs on, and Broadmate's
subscription is restored to the seed's own expression after every test.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app.config import get_settings
from conftest import (
    BROADMATE_WORKSPACE,
    MEMBER,
    OUTSIDER,
    OWNER,
    RIVAL_WORKSPACE,
    SUPERADMIN,
    auth,
)

SUPERUSER_DSN = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
ORG_BROADMATE = "00000000-0000-4000-8000-000000000010"
SUB_BROADMATE = "00000000-0000-4000-8000-000000000040"
MARK = "payments-test"
WS = BROADMATE_WORKSPACE


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


def superuser():
    return psycopg.connect(SUPERUSER_DSN, row_factory=dict_row)


@pytest.fixture(autouse=True)
def restore_everything():
    """Payments before invoices (the foreign key is RESTRICT on purpose), then
    Broadmate's subscription back to what the seed says - by expression, not
    by a remembered value."""

    def _scrub():
        with superuser() as conn, conn.cursor() as cur:
            # The trail these tests write against the seeded organisation:
            # removed with the append-only trigger held open for exactly
            # this statement, as test_admin_onboarding does and for the same
            # reason (a fixture's trail is not a customer's).
            cur.execute("alter table core.audit_log disable trigger audit_log_no_delete")
            cur.execute("delete from core.audit_log where event like 'payment.%%' "
                        "and payload_json->>'invoice_id' in "
                        "(select id::text from core.invoices where coupon_code = %s)", (MARK,))
            cur.execute("alter table core.audit_log enable trigger audit_log_no_delete")
            cur.execute("delete from core.payments where invoice_id in "
                        "(select id from core.invoices where coupon_code = %s)", (MARK,))
            cur.execute("delete from core.invoices where coupon_code = %s", (MARK,))
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
            conn.commit()

    _scrub()
    yield
    _scrub()


@pytest.fixture
def upi_configured(monkeypatch):
    """The runtime knows where money goes: a UPI id and nothing else."""
    s = get_settings()
    monkeypatch.setattr(s, "seller_upi_id", "broadmate@upi")
    monkeypatch.setattr(s, "seller_bank_account_name", "")
    monkeypatch.setattr(s, "seller_bank_account_number", "")
    monkeypatch.setattr(s, "seller_bank_ifsc", "")
    monkeypatch.setattr(s, "seller_bank_name", "")
    monkeypatch.setattr(s, "payment_window_hours", 4)


@pytest.fixture
def nothing_configured(monkeypatch):
    s = get_settings()
    for field in ("seller_upi_id", "seller_bank_account_name", "seller_bank_account_number",
                  "seller_bank_ifsc", "seller_bank_name"):
        monkeypatch.setattr(s, field, "")


def issue_invoice(*, status: str = "issued", total: str = "17700.00") -> str:
    with superuser() as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into core.invoices
              (org_id, subscription_id, number, status, period_start, period_end,
               plan_key, plan_name, list_price_inr, coupon_code, taxable_inr, gst_rate_percent,
               sac_code, gst_split, cgst_inr, sgst_inr, igst_inr, total_inr, issued_at, due_at)
            values
              (%(org)s::uuid, %(sub)s::uuid,
               case when %(issued)s then 'TST-' || substr(gen_random_uuid()::text, 1, 12) end,
               -- A period no job will ever raise an invoice for: the seeded
               -- subscription's live period is what raise_invoices (and the
               -- settle-job test that drives it) would collide with on
               -- invoices_one_per_period.
               %(status)s::core.invoice_status, date '2025-01-01', date '2025-02-01',
               'standard', 'Legacy default', 15000, %(mark)s, 15000, 18, '998314', 'cgst_sgst',
               1350, 1350, 0, %(total)s,
               case when %(issued)s then now() end,
               case when %(issued)s then now() + interval '7 days' end)
            returning id::text
            """,
            {"org": ORG_BROADMATE, "sub": SUB_BROADMATE, "issued": status != "draft",
             "status": status, "mark": MARK, "total": Decimal(total)},
        )
        invoice_id = cur.fetchone()["id"]
        conn.commit()
    return invoice_id


def payment_row(payment_id: str) -> dict:
    with superuser() as conn, conn.cursor() as cur:
        cur.execute("select * from core.payments where id = %s::uuid", (payment_id,))
        return cur.fetchone()


def request(client, invoice_id: str, who: str = OWNER, workspace: str = WS):
    return client.post(f"/api/workspaces/{workspace}/billing/invoices/{invoice_id}/payment-request",
                       headers=auth(who))


def submit(client, payment_id: str, who: str = OWNER, **body):
    payload = {"method": "upi", "reference": "UTR123456789012", **body}
    return client.post(f"/api/workspaces/{WS}/billing/payments/{payment_id}/submit",
                       json=payload, headers=auth(who))


# ---------------------------------------------------------------------------
# Requesting
# ---------------------------------------------------------------------------


def test_an_owner_requests_a_payment_and_is_told_where_to_send_the_money(client, upi_configured):
    invoice = issue_invoice()
    r = request(client, invoice)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["pay_to"]["upi_id"] == "broadmate@upi"
    assert body["pay_to"]["account_number"] is None, "no bank triple, so no half a bank account"
    assert body["pay_to"]["note"].startswith("Pay the invoice total exactly")
    payment = body["payment"]
    assert payment["status"] == "awaiting_payment"
    assert payment["amount_inr"] == "17700.00", "money is a string of the numeric"
    assert payment["invoice_id"] == invoice and payment["invoice_number"].startswith("TST-")
    assert payment["window_ends_at"] and payment["submitted_at"] is None
    assert payment_row(payment["id"])["requested_by"] == uuid.UUID(OWNER)


def test_a_second_request_inside_the_window_is_the_same_row_with_200(client, upi_configured):
    invoice = issue_invoice()
    first = request(client, invoice)
    second = request(client, invoice)
    assert first.status_code == 201 and second.status_code == 200
    assert first.json()["payment"]["id"] == second.json()["payment"]["id"]


def test_without_bank_details_the_request_is_503_and_nothing_is_written(client, nothing_configured):
    invoice = issue_invoice()
    r = request(client, invoice)
    assert r.status_code == 503
    assert r.json()["detail"].startswith("bank_details_unavailable:")
    with superuser() as conn, conn.cursor() as cur:
        cur.execute("select count(*) as n from core.payments where invoice_id = %s::uuid", (invoice,))
        assert cur.fetchone()["n"] == 0
    assert client.get(f"/api/workspaces/{WS}/billing/payments", headers=auth(OWNER)).json()["pay_to"] is None


def test_a_partial_bank_triple_is_no_bank_account(monkeypatch):
    from app.billing.payments import pay_to

    s = get_settings()
    for field in ("seller_upi_id", "seller_bank_account_name", "seller_bank_account_number", "seller_bank_ifsc"):
        monkeypatch.setattr(s, field, "")
    monkeypatch.setattr(s, "seller_bank_account_name", "Broadmate Global")
    monkeypatch.setattr(s, "seller_bank_account_number", "1234567890")
    assert pay_to() is None, "two of the three is a transfer that bounces"
    monkeypatch.setattr(s, "seller_bank_ifsc", "hdfc0001234")
    monkeypatch.setattr(s, "seller_bank_name", "HDFC Bank")
    details = pay_to()
    assert details["ifsc"] == "HDFC0001234" and details["bank_name"] == "HDFC Bank" and details["upi_id"] is None


def test_a_member_is_refused_with_403_and_an_outsider_with_404(client, upi_configured):
    invoice = issue_invoice()
    r = request(client, invoice, who=MEMBER)
    assert r.status_code == 403 and r.json()["detail"].startswith("not_allowed:")
    r = request(client, invoice, who=OUTSIDER, workspace=RIVAL_WORKSPACE)
    assert r.status_code == 404
    r = request(client, str(uuid.uuid4()))
    assert r.status_code == 404
    with superuser() as conn, conn.cursor() as cur:
        cur.execute("select count(*) as n from core.payments where invoice_id = %s::uuid", (invoice,))
        assert cur.fetchone()["n"] == 0


@pytest.mark.parametrize("status", ["draft", "paid", "void"])
def test_only_an_issued_invoice_is_payable(client, upi_configured, status):
    invoice = issue_invoice(status=status)
    r = request(client, invoice)
    assert r.status_code == 422 and r.json()["detail"].startswith("invoice_not_payable:")


# ---------------------------------------------------------------------------
# Submitting
# ---------------------------------------------------------------------------


def test_submitting_within_the_window_records_the_reference_and_the_invoice_list_shows_it(client, upi_configured):
    invoice = issue_invoice()
    payment = request(client, invoice).json()["payment"]
    r = submit(client, payment["id"], reference="  UTR123456789012 ", paid_on=str(date.today() - timedelta(days=1)))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "submitted" and body["method"] == "upi"
    assert body["reference"] == "UTR123456789012" and body["submitted_at"]

    invoices = client.get(f"/api/workspaces/{WS}/billing/invoices", headers=auth(OWNER)).json()
    mine = next(i for i in invoices if i["id"] == invoice)
    assert mine["payment"]["id"] == payment["id"] and mine["payment"]["status"] == "submitted"

    listed = client.get(f"/api/workspaces/{WS}/billing/payments", headers=auth(OWNER)).json()
    assert listed["pay_to"]["upi_id"] == "broadmate@upi"
    assert listed["payments"][0]["id"] == payment["id"]

    r = submit(client, payment["id"])
    assert r.status_code == 422 and r.json()["detail"].startswith("payment_not_awaiting:")


def test_a_reference_is_validated_before_the_database_sees_it(client, upi_configured):
    invoice = issue_invoice()
    payment = request(client, invoice).json()["payment"]
    assert submit(client, payment["id"], reference=" pd ").status_code == 422
    assert submit(client, payment["id"], method="cash").status_code == 422
    assert payment_row(payment["id"])["status"] == "awaiting_payment"


def test_a_late_submit_is_refused_and_the_row_is_expired(client, upi_configured):
    invoice = issue_invoice()
    payment = request(client, invoice).json()["payment"]
    with superuser() as conn, conn.cursor() as cur:
        cur.execute("update core.payments set window_ends_at = now() - interval '1 minute' where id = %s::uuid",
                    (payment["id"],))
        conn.commit()
    r = submit(client, payment["id"])
    assert r.status_code == 422 and r.json()["detail"].startswith("window_elapsed:")
    assert payment_row(payment["id"])["status"] == "expired"
    # And a new request opens a fresh window rather than returning the dead one.
    again = request(client, invoice)
    assert again.status_code == 201 and again.json()["payment"]["id"] != payment["id"]


def test_a_member_cannot_see_a_payment_and_an_outsider_cannot_submit_one(client, upi_configured):
    invoice = issue_invoice()
    payment = request(client, invoice).json()["payment"]
    assert client.get(f"/api/workspaces/{WS}/billing/payments", headers=auth(MEMBER)).json()["payments"] == []
    assert submit(client, payment["id"], who=MEMBER).status_code == 403
    r = client.post(f"/api/workspaces/{RIVAL_WORKSPACE}/billing/payments/{payment['id']}/submit",
                    json={"method": "upi", "reference": "UTR123456789012"}, headers=auth(OUTSIDER))
    assert r.status_code == 404
    assert payment_row(payment["id"])["status"] == "awaiting_payment"


# ---------------------------------------------------------------------------
# The operator
# ---------------------------------------------------------------------------


def submitted_payment(client) -> tuple[str, str]:
    invoice = issue_invoice()
    payment = request(client, invoice).json()["payment"]
    assert submit(client, payment["id"]).status_code == 200
    return invoice, payment["id"]


def test_the_operator_sees_the_submission_with_who_it_is_from(client, upi_configured):
    invoice, payment_id = submitted_payment(client)
    inbox = client.get("/api/admin/payments", headers=auth(SUPERADMIN)).json()
    mine = next(p for p in inbox if p["id"] == payment_id)
    assert mine["status"] == "submitted"
    assert mine["org_name"] == "Broadmate Global" and mine["org_slug"] == "broadmate-global"
    assert mine["org_id"] == ORG_BROADMATE
    assert mine["submitted_by_email"] == "owner@advit.local"
    assert mine["reference"] == "UTR123456789012" and mine["amount_inr"] == "17700.00"
    assert all(p["status"] == "submitted" for p in inbox), "the default inbox is what needs a person"
    assert client.get("/api/admin/overview", headers=auth(SUPERADMIN)).json()["payments_submitted"] >= 1


def test_approval_pays_the_invoice_and_restores_the_subscription(client, upi_configured):
    invoice, payment_id = submitted_payment(client)
    with superuser() as conn, conn.cursor() as cur:
        cur.execute("update core.subscriptions set status = 'past_due', grace_ends_at = now() + interval '2 days' "
                    "where id = %s::uuid", (SUB_BROADMATE,))
        conn.commit()
    r = client.post(f"/api/admin/payments/{payment_id}/review",
                    json={"verdict": "approved", "note": "matched"}, headers=auth(SUPERADMIN))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["payment"]["status"] == "approved" and body["payment"]["reviewed_at"]
    assert body["invoice_status"] == "paid" and body["subscription_status"] == "active"

    sub = client.get(f"/api/workspaces/{WS}/billing/subscription", headers=auth(OWNER)).json()
    assert sub["status"] == "active" and sub["grace_ends_at"] is None and sub["access_mode"] == "full"
    invoices = client.get(f"/api/workspaces/{WS}/billing/invoices", headers=auth(OWNER)).json()
    mine = next(i for i in invoices if i["id"] == invoice)
    assert mine["status"] == "paid" and mine["paid_at"] and mine["payment"]["status"] == "approved"

    r = client.post(f"/api/admin/payments/{payment_id}/review",
                    json={"verdict": "approved"}, headers=auth(SUPERADMIN))
    assert r.status_code == 409 and r.json()["detail"].startswith("payment_not_submitted:")
    assert client.get("/api/admin/payments?status=all", headers=auth(SUPERADMIN)).json()[0]["id"] == payment_id


def test_a_rejection_without_a_note_is_422_and_with_one_closes_the_row(client, upi_configured):
    invoice, payment_id = submitted_payment(client)
    r = client.post(f"/api/admin/payments/{payment_id}/review", json={"verdict": "rejected"},
                    headers=auth(SUPERADMIN))
    assert r.status_code == 422 and r.json()["detail"].startswith("reason_required:")
    assert payment_row(payment_id)["status"] == "submitted"

    r = client.post(f"/api/admin/payments/{payment_id}/review",
                    json={"verdict": "rejected", "note": "no such credit on the statement"}, headers=auth(SUPERADMIN))
    assert r.status_code == 200
    assert r.json()["payment"]["review_note"] == "no such credit on the statement"
    assert r.json()["invoice_status"] == "issued" and r.json()["subscription_status"] == "active"
    assert request(client, invoice).status_code == 201, "the organisation may request again"


def test_an_unknown_payment_is_404_for_the_operator(client):
    r = client.post(f"/api/admin/payments/{uuid.uuid4()}/review", json={"verdict": "approved"},
                    headers=auth(SUPERADMIN))
    assert r.status_code == 404


def test_a_tenant_cannot_reach_the_operator_routes(client, upi_configured):
    _, payment_id = submitted_payment(client)
    assert client.get("/api/admin/payments", headers=auth(OWNER)).status_code == 404
    r = client.post(f"/api/admin/payments/{payment_id}/review", json={"verdict": "approved"}, headers=auth(OWNER))
    assert r.status_code == 404
    assert payment_row(payment_id)["status"] == "submitted"


# ---------------------------------------------------------------------------
# The subscription says what the guards will say
# ---------------------------------------------------------------------------


def test_the_subscription_carries_access_mode_and_the_grace_deadline(client):
    sub = client.get(f"/api/workspaces/{WS}/billing/subscription", headers=auth(OWNER)).json()
    assert sub["access_mode"] == "full" and sub["grace_ends_at"] is None
    with superuser() as conn, conn.cursor() as cur:
        cur.execute("update core.subscriptions set status = 'past_due', grace_ends_at = now() + interval '5 days' "
                    "where id = %s::uuid", (SUB_BROADMATE,))
        conn.commit()
    sub = client.get(f"/api/workspaces/{WS}/billing/subscription", headers=auth(OWNER)).json()
    assert sub["access_mode"] == "read_only" and sub["grace_ends_at"] is not None
