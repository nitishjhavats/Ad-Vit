"""The four tiers, coupons, and an invoice that says what was invoiced.

Three properties run through this file:

  * **A coupon is refused on every doubt.** Expired, exhausted, inactive,
    wrong plan, unknown code, not an owner - each is a named refusal from
    ``core.apply_coupon`` in SQL, not a Python check that a route might skip.
    And the dropdown shows exactly what the redemption would accept, because
    both read the same RLS-filtered rows.

  * **An invoice is a snapshot.** The plan can be renamed, the coupon
    deactivated, the GST rate changed; the invoice still says what was
    invoiced. And an invoice that cannot be issued correctly - no buyer state,
    no seller GSTIN - is a DRAFT with a reason, never a document with the wrong
    tax on it.

  * **The tiers mean something.** A Starter account does not retrieve industry
    patterns, because the PRD sells that at Growth; and it reads the plan on
    every turn, so a downgrade takes effect on the next one.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.billing import invoices as inv
from conftest import (
    ANALYST,
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
ORG_RIVAL = "00000000-0000-4000-8000-000000000011"


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def clean_billing():
    """Coupons, invoices, and any coupon left on a subscription. Superuser,
    because the service role holds no DELETE."""

    def _scrub():
        with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
            cur.execute("update core.subscriptions set coupon_id = null")
            # Payments reference invoices with ON DELETE RESTRICT on purpose;
            # a leftover from an interrupted test_payments run goes first.
            cur.execute("delete from core.payments")
            cur.execute("delete from core.invoices")
            cur.execute("delete from core.coupons")
            conn.commit()

    _scrub()
    yield
    _scrub()


def make_coupon(client, code="DIWALI25", pct=25, plans=("standard",), **extra) -> dict:
    r = client.post(
        "/api/admin/coupons",
        json={"code": code, "name": f"{code} offer", "percent_off": pct, "plan_keys": list(plans), **extra},
        headers=auth(SUPERADMIN),
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# The tiers
# ---------------------------------------------------------------------------


def test_the_four_prd_tiers_are_active_with_the_prd_limits(client):
    """The numbers the PRD's own table gives. Everything else in the seed is
    marked ASSUMED beside it, and this test does not assert those."""
    plans = {p["key"]: p for p in client.get(
        f"/api/workspaces/{BROADMATE_WORKSPACE}/billing/plans", headers=auth(OWNER)
    ).json()}

    for key in ("starter", "growth", "scale", "agency"):
        assert key in plans, f"{key} is not active"

    f = {k: plans[k]["features"] for k in plans}
    assert f["starter"]["max_ad_accounts"] == 1 and f["starter"]["max_autonomy_level"] == 1
    assert f["growth"]["max_ad_accounts"] == 2 and f["growth"]["max_autonomy_level"] == 3
    assert f["scale"]["max_ad_accounts"] == 5 and f["scale"]["max_autonomy_level"] == 4
    assert f["growth"]["feature.experiments"] is True and f["starter"]["feature.experiments"] is False
    assert f["scale"]["feature.competitor_intel"] is True and f["growth"]["feature.competitor_intel"] is False
    assert f["starter"]["feature.industry_intelligence"] is False
    assert f["growth"]["feature.industry_intelligence"] is True


def test_byok_is_on_every_tier_because_the_owner_said_so(client):
    """The PRD puts BYOK at Scale as an option. The owner later decided every
    organisation supplies its own key from day one, and that decision
    supersedes the table."""
    plans = client.get(f"/api/workspaces/{BROADMATE_WORKSPACE}/billing/plans", headers=auth(OWNER)).json()
    assert all(p["features"]["feature.byok"] is True for p in plans)


def test_the_compliance_gate_is_on_every_tier_and_never_an_upsell(client):
    plans = client.get(f"/api/workspaces/{BROADMATE_WORKSPACE}/billing/plans", headers=auth(OWNER)).json()
    assert all(p["features"]["feature.compliance_gate"] is True for p in plans)


@pytest.fixture
def broadmate_on(client):
    """Move Broadmate's subscription to a named plan for one test."""
    moved: list[str] = []

    def _move(plan_key: str):
        with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
            cur.execute("select plan_id::text from core.subscriptions where org_id = %s::uuid", (ORG_BROADMATE,))
            moved.append(cur.fetchone()[0])
            cur.execute(
                """update core.subscriptions set plan_id = (select id from core.plans where key = %s)
                    where org_id = %s::uuid""",
                (plan_key, ORG_BROADMATE),
            )
            conn.commit()

    yield _move

    if moved:
        with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
            cur.execute("update core.subscriptions set plan_id = %s::uuid where org_id = %s::uuid",
                        (moved[0], ORG_BROADMATE))
            conn.commit()


def _context(user: str, workspace: str) -> dict:
    from app.agents.compliance import ComplianceGate
    from app.orchestrator.graph import Orchestrator
    from conftest import acting_as

    orch = Orchestrator(router=None, gate_factory=lambda bt: ComplianceGate([]))
    with acting_as(user):
        return orch.assemble_context({"workspace_id": workspace})


def test_a_starter_account_learns_from_itself_not_from_the_industry(broadmate_on):
    """Read from the plan on every turn - core.can, not a cached flag - so a
    downgrade takes effect on the next proposal rather than the next deploy."""
    broadmate_on("starter")
    ctx = _context(OWNER, BROADMATE_WORKSPACE)
    assert ctx["industry_patterns"] == []
    assert "not included in this plan" in ctx["stable_prefix"]

    broadmate_on("growth")
    ctx = _context(OWNER, BROADMATE_WORKSPACE)
    assert "not included in this plan" not in ctx["stable_prefix"]


# ---------------------------------------------------------------------------
# Coupons: the operator's side
# ---------------------------------------------------------------------------


def test_an_operator_creates_a_coupon_and_names_its_plans(client):
    made = make_coupon(client, plans=("standard", "growth"))
    assert made["code"] == "DIWALI25"
    assert made["plan_keys"] == ["growth", "standard"]

    listed = client.get("/api/admin/coupons", headers=auth(SUPERADMIN)).json()
    assert [c["code"] for c in listed] == ["DIWALI25"]
    assert listed[0]["redemptions"] == 0


def test_the_code_is_normalised_so_case_is_not_a_second_coupon(client):
    make_coupon(client, code="diwali25")
    r = client.post(
        "/api/admin/coupons",
        json={"code": "DIWALI25", "name": "again", "percent_off": 10, "plan_keys": ["standard"]},
        headers=auth(SUPERADMIN),
    )
    assert r.status_code == 409


def test_a_coupon_naming_no_real_plan_is_refused(client):
    r = client.post(
        "/api/admin/coupons",
        json={"code": "GHOST", "name": "x", "percent_off": 10, "plan_keys": ["platinum"]},
        headers=auth(SUPERADMIN),
    )
    assert r.status_code == 422


def test_a_tenant_cannot_reach_the_admin_surface_and_is_not_told_it_exists(client):
    """404, not 403. A 403 on /api/admin/coupons tells a tenant the admin
    surface exists at that path."""
    for user in (OWNER, MEMBER, OUTSIDER, ANALYST):
        r = client.post(
            "/api/admin/coupons",
            json={"code": "MINE", "name": "x", "percent_off": 90, "plan_keys": ["standard"]},
            headers=auth(user),
        )
        assert r.status_code == 404, (user, r.status_code)
        assert client.get("/api/admin/coupons", headers=auth(user)).status_code == 404


def test_a_superadmins_status_is_read_from_the_row_not_the_token(client):
    """core.platform_users.is_superadmin: "read from the database on every
    check, never trusted from a JWT claim alone". Flip the row, same token,
    refused."""
    with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
        cur.execute("update core.platform_users set is_superadmin = false where id = %s::uuid", (SUPERADMIN,))
        conn.commit()
    try:
        assert client.get("/api/admin/coupons", headers=auth(SUPERADMIN)).status_code == 404
    finally:
        with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
            cur.execute("update core.platform_users set is_superadmin = true where id = %s::uuid", (SUPERADMIN,))
            conn.commit()


# ---------------------------------------------------------------------------
# Coupons: the tenant's side
# ---------------------------------------------------------------------------


def test_the_dropdown_shows_only_coupons_for_that_plan(client):
    make_coupon(client, code="FORSTD", plans=("standard",))
    make_coupon(client, code="FORGROWTH", plans=("growth",))
    plans = {p["key"]: p for p in client.get(
        f"/api/workspaces/{BROADMATE_WORKSPACE}/billing/plans", headers=auth(OWNER)
    ).json()}
    assert [c["code"] for c in plans["standard"]["coupons"]] == ["FORSTD"]
    assert [c["code"] for c in plans["growth"]["coupons"]] == ["FORGROWTH"]
    assert plans["starter"]["coupons"] == []


def test_a_tenant_sees_the_offer_not_the_operators_books(client):
    """The dropdown carries code, name, percent and expiry. Not the redemption
    count, not the cap, not who created it."""
    make_coupon(client, max_redemptions=5)
    plans = {p["key"]: p for p in client.get(
        f"/api/workspaces/{BROADMATE_WORKSPACE}/billing/plans", headers=auth(OWNER)
    ).json()}
    offer = plans["standard"]["coupons"][0]
    assert set(offer) == {"code", "name", "percent_off", "valid_to"}


def test_applying_a_coupon_brings_the_price_down_by_that_much(client):
    make_coupon(client, pct=25)
    r = client.post(
        f"/api/workspaces/{BROADMATE_WORKSPACE}/billing/coupon",
        json={"code": "diwali25"}, headers=auth(OWNER),
    )
    assert r.status_code == 200, r.text
    sub = r.json()
    assert sub["coupon_code"] == "DIWALI25"
    assert Decimal(str(sub["list_price_inr"])) == Decimal("15000.00")
    assert Decimal(str(sub["discount_inr"])) == Decimal("3750.00")
    assert Decimal(str(sub["price_inr"])) == Decimal("11250.00")


def test_applying_the_same_coupon_twice_is_one_redemption(client):
    make_coupon(client, max_redemptions=1)
    for _ in range(2):
        r = client.post(f"/api/workspaces/{BROADMATE_WORKSPACE}/billing/coupon",
                        json={"code": "DIWALI25"}, headers=auth(OWNER))
        assert r.status_code == 200, r.text
    listed = client.get("/api/admin/coupons", headers=auth(SUPERADMIN)).json()
    assert listed[0]["redemptions"] == 1


@pytest.mark.parametrize(
    "setup,code,hint",
    [
        ({}, "NOSUCH", "coupon_unknown"),
        ({"plans": ("growth",)}, "DIWALI25", "coupon_wrong_plan"),
        ({"valid_to": "2020-01-01T00:00:00Z", "valid_from": "2019-01-01T00:00:00Z"}, "DIWALI25", "coupon_expired"),
        ({"valid_from": "2099-01-01T00:00:00Z"}, "DIWALI25", "coupon_expired"),
    ],
)
def test_every_doubt_is_a_named_refusal(client, setup, code, hint):
    """From core.apply_coupon in SQL, with a hint, not from a Python check a
    route might skip."""
    if hint != "coupon_unknown":
        make_coupon(client, **setup)
    r = client.post(f"/api/workspaces/{BROADMATE_WORKSPACE}/billing/coupon",
                    json={"code": code}, headers=auth(OWNER))
    assert r.status_code == 422, r.text
    assert r.json()["detail"].startswith(hint)


def test_an_exhausted_coupon_is_refused_for_the_next_organisation(client):
    make_coupon(client, max_redemptions=1)
    assert client.post(f"/api/workspaces/{BROADMATE_WORKSPACE}/billing/coupon",
                       json={"code": "DIWALI25"}, headers=auth(OWNER)).status_code == 200
    r = client.post(f"/api/workspaces/{RIVAL_WORKSPACE}/billing/coupon",
                    json={"code": "DIWALI25"}, headers=auth(OUTSIDER))
    assert r.status_code == 422
    assert r.json()["detail"].startswith("coupon_exhausted")


def test_a_deactivated_coupon_stops_working_but_stays_on_the_books(client):
    made = make_coupon(client)
    client.patch(f"/api/admin/coupons/{made['id']}", json={"is_active": False}, headers=auth(SUPERADMIN))
    r = client.post(f"/api/workspaces/{BROADMATE_WORKSPACE}/billing/coupon",
                    json={"code": "DIWALI25"}, headers=auth(OWNER))
    assert r.status_code == 422 and r.json()["detail"].startswith("coupon_inactive")
    # gone from the tenant's dropdown, still in the operator's list
    plans = {p["key"]: p for p in client.get(
        f"/api/workspaces/{BROADMATE_WORKSPACE}/billing/plans", headers=auth(OWNER)).json()}
    assert plans["standard"]["coupons"] == []
    assert len(client.get("/api/admin/coupons", headers=auth(SUPERADMIN)).json()) == 1


def test_only_an_owner_or_admin_spends_the_organisations_money(client):
    make_coupon(client)
    r = client.post(f"/api/workspaces/{BROADMATE_WORKSPACE}/billing/coupon",
                    json={"code": "DIWALI25"}, headers=auth(MEMBER))
    assert r.status_code == 403
    assert r.json()["detail"].startswith("not_billing_admin")


# ---------------------------------------------------------------------------
# GST
# ---------------------------------------------------------------------------


def test_intra_state_is_cgst_plus_sgst_and_the_halves_add_back():
    """Half-up rounding of each half can lose a paisa against the total; the
    second half is the remainder, not a second rounding."""
    tax = inv.gst(Decimal("11250.00"), rate_percent=Decimal("18"), seller_state="09", buyer_state="09")
    assert tax.split == "cgst_sgst"
    assert tax.cgst + tax.sgst == Decimal("2025.00")
    assert tax.igst == 0


def test_inter_state_is_igst_at_the_full_rate():
    tax = inv.gst(Decimal("11250.00"), rate_percent=Decimal("18"), seller_state="09", buyer_state="27")
    assert tax.split == "igst"
    assert tax.igst == Decimal("2025.00")
    assert tax.cgst == 0 and tax.sgst == 0


def test_an_odd_paisa_lands_on_one_half_not_in_the_bin():
    tax = inv.gst(Decimal("100.03"), rate_percent=Decimal("18"), seller_state="09", buyer_state="09")
    assert tax.cgst + tax.sgst == inv.money(Decimal("100.03") * Decimal("0.18"))


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------


@pytest.fixture
def seller_configured():
    from app.config import get_settings

    s = get_settings()
    before = (s.seller_gstin, s.seller_legal_name, s.seller_state_code)
    s.seller_gstin, s.seller_legal_name, s.seller_state_code = "09AAACB1234C1ZV", "Broadmate Global", "09"
    try:
        yield
    finally:
        s.seller_gstin, s.seller_legal_name, s.seller_state_code = before


@pytest.fixture
def broadmate_state():
    """Blank Broadmate's state code for a test, restoring the SEEDED value.

    Read, not remembered: an earlier version restored a hard-coded 27 while
    the seed says 09, which corrupted the fixture for every run after the
    first and made a wrong assertion pass.
    """
    with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
        cur.execute("select state_code from core.organisations where id = %s::uuid", (ORG_BROADMATE,))
        original = cur.fetchone()[0]

    def _set(value):
        with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
            cur.execute("update core.organisations set state_code = %s where id = %s::uuid", (value, ORG_BROADMATE))
            conn.commit()

    yield _set
    _set(original)


def _invoices(org: str) -> list[dict]:
    with psycopg.connect(SUPERUSER_DSN, row_factory=psycopg.rows.dict_row) as conn, conn.cursor() as cur:
        cur.execute("select * from core.invoices where org_id = %s::uuid order by period_start", (org,))
        return cur.fetchall()


def test_an_active_subscription_is_invoiced_once_per_period(seller_configured):
    first = inv.raise_invoices(today=date(2026, 9, 16))
    second = inv.raise_invoices(today=date(2026, 9, 16))
    assert len(first["issued"]) >= 1
    assert second["considered"] == 0, "the same period was invoiced twice"


def test_a_trial_is_not_invoiced(seller_configured):
    """Rival is trialing in the seed."""
    inv.raise_invoices(today=date(2026, 9, 16))
    assert _invoices(ORG_RIVAL) == []


def test_the_invoice_snapshots_everything_it_names(client, seller_configured):
    make_coupon(client, pct=25)
    client.post(f"/api/workspaces/{BROADMATE_WORKSPACE}/billing/coupon",
                json={"code": "DIWALI25"}, headers=auth(OWNER))
    inv.raise_invoices(today=date(2026, 9, 16))
    row = _invoices(ORG_BROADMATE)[0]

    assert row["status"] == "issued"
    assert row["number"] and row["number"].startswith("BMG-2026-")
    assert row["plan_name"] == "Legacy default"
    assert row["coupon_code"] == "DIWALI25" and row["percent_off"] == Decimal("25.00")
    assert row["list_price_inr"] == Decimal("15000.00")
    assert row["discount_inr"] == Decimal("3750.00")
    assert row["taxable_inr"] == Decimal("11250.00")
    assert row["gst_rate_percent"] == Decimal("18.00")
    # Broadmate is seeded in state 09 - the SAME state as the seller - so this is
    # an intra-state supply: CGST + SGST, half each. The first draft of this
    # test asserted IGST from memory of the wrong seed row, and its own restore
    # step then wrote 27 back, which made the assertion pass on every run but
    # the first. The seed is the ground truth; a test reads it, it does not
    # remember it.
    assert row["gst_split"] == "cgst_sgst"
    assert row["cgst_inr"] == Decimal("1012.50") and row["sgst_inr"] == Decimal("1012.50")
    assert row["igst_inr"] == Decimal("0.00")
    assert row["total_inr"] == Decimal("13275.00")
    assert row["seller_gstin"] == "09AAACB1234C1ZV"
    assert row["buyer_state_code"] == "09"


def test_a_buyer_in_another_state_is_charged_igst(seller_configured, broadmate_state):
    """The other split, exercised end to end rather than only in gst()."""
    broadmate_state("27")
    inv.raise_invoices(today=date(2026, 9, 16))
    row = _invoices(ORG_BROADMATE)[0]
    assert row["gst_split"] == "igst"
    assert row["igst_inr"] == Decimal("2700.00")
    assert row["cgst_inr"] == 0 and row["sgst_inr"] == 0
    assert row["buyer_state_code"] == "27"


def test_renaming_the_plan_afterwards_does_not_rewrite_the_invoice(seller_configured):
    inv.raise_invoices(today=date(2026, 9, 16))
    with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
        cur.execute("update core.plans set name = 'Renamed' where key = 'standard'")
        conn.commit()
    try:
        assert _invoices(ORG_BROADMATE)[0]["plan_name"] == "Legacy default"
    finally:
        with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
            cur.execute("update core.plans set name = 'Legacy default' where key = 'standard'")
            conn.commit()


def test_no_buyer_state_means_a_draft_with_a_reason_not_a_wrong_tax(seller_configured, broadmate_state):
    broadmate_state(None)
    result = inv.raise_invoices(today=date(2026, 9, 16))
    drafts = [d for d in result["drafts"] if d["org_id"] == ORG_BROADMATE]
    assert drafts and "buyer state" in drafts[0]["reason"]
    row = _invoices(ORG_BROADMATE)[0]
    assert row["status"] == "draft" and row["number"] is None


def test_no_seller_registration_means_nothing_is_issued():
    """The default configuration. Every due subscription becomes a draft and
    the reason is in the job detail, rather than a run that quietly did
    nothing or an invoice with no seller on it."""
    from app.config import get_settings

    assert not get_settings().seller_gstin, "this test assumes the default (unconfigured) seller"
    result = inv.raise_invoices(today=date(2026, 9, 16))
    assert result["issued"] == []
    assert all("seller" in d["reason"] for d in result["drafts"])


def test_a_draft_never_burns_an_invoice_number(seller_configured, broadmate_state):
    """GST wants the series gapless. The number is taken at ISSUE."""
    broadmate_state(None)
    with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
        cur.execute("select last_value from core.invoice_numbers")
        before = cur.fetchone()[0]
    inv.raise_invoices(today=date(2026, 9, 16))
    with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
        cur.execute("select last_value from core.invoice_numbers")
        assert cur.fetchone()[0] == before


def test_owners_see_their_invoices_and_media_buyers_do_not(client, seller_configured):
    inv.raise_invoices(today=date(2026, 9, 16))
    owner = client.get(f"/api/workspaces/{BROADMATE_WORKSPACE}/billing/invoices", headers=auth(OWNER)).json()
    buyer = client.get(f"/api/workspaces/{BROADMATE_WORKSPACE}/billing/invoices", headers=auth(MEMBER)).json()
    assert len(owner) == 1
    assert buyer == [], "a media buyer can read what the company is billed"


def test_a_stranger_cannot_read_another_organisations_invoices(client, seller_configured):
    inv.raise_invoices(today=date(2026, 9, 16))
    r = client.get(f"/api/workspaces/{BROADMATE_WORKSPACE}/billing/invoices", headers=auth(OUTSIDER))
    assert r.status_code == 404


def test_a_tenant_cannot_write_an_invoice():
    """`authenticated` holds SELECT and nothing else."""
    import json

    from conftest import TENANT_DSN, claims_for

    with psycopg.connect(TENANT_DSN) as conn:
        conn.autocommit = False
        conn.execute("select 1")
        with conn.cursor() as cur:
            cur.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps(claims_for(OWNER)),))
            cur.execute("set local role authenticated")
            cur.execute("savepoint p")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("update core.invoices set status = 'paid'")
            cur.execute("rollback to savepoint p")
        conn.rollback()


_ = (uuid, timedelta)
