"""The operator's console: every route is invisible to a tenant, and the two
acts it performs land exactly where 20260917000001 said they would.

Two properties:

  * **A tenant cannot see that /api/admin exists.** Every route, every method,
    404 - the same shape as a foreign workspace. Asserted over the router's own
    route table rather than a hand-kept list, so a route added tomorrow is
    covered the moment it is registered.

  * **Nothing here decides.** Suspension goes through the SQL function that
    carries the check; an override goes through the value guard and the stamp
    trigger; a re-verification writes the one column a session may write and
    the trigger refuses the future. The tests assert the DATABASE'S view after
    each call - the audit row, the resolved entitlement, the rule's date - not
    the route's return value alone.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient

from conftest import (
    BROADMATE_WORKSPACE,
    OUTSIDER,
    OWNER,
    RIVAL_WORKSPACE,
    SUPERADMIN,
    auth,
)

SUPERUSER_DSN = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
ORG_BROADMATE = "00000000-0000-4000-8000-000000000010"
ORG_RIVAL = "00000000-0000-4000-8000-000000000011"
MARK = "admin-console-test"


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


def superuser():
    return psycopg.connect(SUPERUSER_DSN)


@pytest.fixture(autouse=True)
def restore_everything():
    """Every mutation this file makes, undone before and after - by marker
    where a row is created, by value where a row is changed. Superuser, because
    the service role holds no DELETE and the reverify trigger refuses to move
    a date backwards for anybody."""

    # The SEED's values, not "active": Rival is seeded `trialing`, and a scrub
    # that wrote `active` broke test_a_trial_is_not_invoiced in another file -
    # the same defect as the hard-coded state code in test_billing, met again.
    SEED_SUBSCRIPTIONS = {ORG_BROADMATE: "active", ORG_RIVAL: "trialing"}

    def _scrub():
        with superuser() as conn, conn.cursor() as cur:
            cur.execute("update core.organisations set status = 'active', suspended_at = null, "
                        "suspension_reason = null where id in (%s::uuid, %s::uuid)", (ORG_BROADMATE, ORG_RIVAL))
            for org, status in SEED_SUBSCRIPTIONS.items():
                cur.execute("update core.subscriptions set status = %s::core.subscription_status, "
                            "plan_id = (select id from core.plans where key = 'standard') "
                            "where org_id = %s::uuid and cancelled_at is null", (status, org))
            cur.execute("delete from core.entitlement_overrides where reason like %s", (MARK + "%",))
            cur.execute("delete from t_advit.watch_findings where detail_json->>'marker' = %s", (MARK,))
            cur.execute("delete from core.invoices where coupon_code = %s", (MARK,))
            # Rules the tests re-verified: the seed dates them 2026-03-01.
            cur.execute("alter table t_advit.policy_rules disable trigger policy_rules_reverify")
            cur.execute("update t_advit.policy_rules set as_of = '2026-03-01' "
                        "where jurisdiction = 'meta' and as_of <> '2026-03-01'")
            cur.execute("alter table t_advit.policy_rules enable trigger policy_rules_reverify")
            conn.commit()

    _scrub()
    yield
    _scrub()


# ---------------------------------------------------------------------------
# Invisible to a tenant
# ---------------------------------------------------------------------------


def _admin_routes() -> list[tuple[str, str]]:
    from app.routes_admin import router

    out = []
    for route in router.routes:
        path = route.path.replace("{org_id}", ORG_BROADMATE)
        path = path.replace("{finding_id}", str(uuid.uuid4())).replace("{knowledge_id}", str(uuid.uuid4()))
        path = path.replace("{code}", "META_X").replace("{feature_key}", "max_ad_accounts")
        for method in route.methods:
            out.append((method, path))
    return sorted(out)


@pytest.mark.parametrize("method,path", _admin_routes())
def test_every_admin_route_is_404_for_a_tenant_owner(client, method, path):
    """Not 403. A 403 says "this exists and you may not"; a 404 says nothing.
    Every route on the admin router, whatever the body, whatever the method."""
    r = client.request(method, path, headers=auth(OWNER), json={})
    assert r.status_code == 404, f"{method} {path} -> {r.status_code}: {r.text[:200]}"


def test_me_names_the_operator(client):
    r = client.get("/api/admin/me", headers=auth(SUPERADMIN))
    assert r.status_code == 200
    assert r.json()["user_id"] == SUPERADMIN
    assert r.json()["email"]


def test_the_overview_counts_what_needs_a_person(client):
    with superuser() as conn, conn.cursor() as cur:
        cur.execute(
            "insert into t_advit.watch_findings (kind, subject, severity, detail_json) "
            "values ('rule_stale', %s, 'urgent', %s::jsonb)",
            (f"{MARK}-overview", f'{{"marker": "{MARK}"}}'),
        )
        conn.commit()
    body = client.get("/api/admin/overview", headers=auth(SUPERADMIN)).json()
    assert body["organisations_by_status"].get("active", 0) >= 2
    assert body["open_findings_by_severity"].get("urgent", 0) >= 1
    for key in ("draft_invoices", "unpaid_invoices", "live_coupons", "subscriptions_needing_attention"):
        assert isinstance(body[key], int)


# ---------------------------------------------------------------------------
# Organisations
# ---------------------------------------------------------------------------


def test_the_organisation_list_carries_plan_and_readiness(client):
    orgs = {o["id"]: o for o in client.get("/api/admin/organisations", headers=auth(SUPERADMIN)).json()}
    assert ORG_BROADMATE in orgs and ORG_RIVAL in orgs
    b = orgs[ORG_BROADMATE]
    assert b["plan_key"] == "standard" and b["subscription_status"] == "active"
    assert b["workspaces"] >= 1 and b["members"] >= 2
    assert b["billing_ready"] is False, "the seed carries a state code but no GSTIN"


def test_the_organisation_detail_resolves_entitlements_with_their_source(client):
    body = client.get(f"/api/admin/organisations/{ORG_BROADMATE}", headers=auth(SUPERADMIN)).json()
    assert {m["user_id"] for m in body["members"]} >= {OWNER}
    assert any(w["id"] == BROADMATE_WORKSPACE for w in body["workspaces"])
    by_key = {e["feature_key"]: e for e in body["entitlements"]}
    assert by_key["max_autonomy_level"]["source"] == "plan"
    assert by_key["max_autonomy_level"]["value"] == 3
    assert by_key["max_autonomy_level"]["value_type"] == "integer"


def test_an_unknown_organisation_is_404(client):
    r = client.get(f"/api/admin/organisations/{uuid.uuid4()}", headers=auth(SUPERADMIN))
    assert r.status_code == 404


def test_suspending_needs_a_reason_and_lands_in_the_trail(client):
    r = client.patch(f"/api/admin/organisations/{ORG_RIVAL}/status",
                     json={"status": "suspended"}, headers=auth(SUPERADMIN))
    assert r.status_code == 422 and "reason_required" in r.text

    r = client.patch(f"/api/admin/organisations/{ORG_RIVAL}/status",
                     json={"status": "suspended", "reason": "chargeback"}, headers=auth(SUPERADMIN))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "suspended" and r.json()["suspension_reason"] == "chargeback"

    # The database's view, not the route's: access_mode is what the tenant's
    # every request reads, and the trail names the operator.
    with superuser() as conn, conn.cursor() as cur:
        cur.execute("select status::text, suspension_reason from core.organisations where id = %s::uuid", (ORG_RIVAL,))
        assert cur.fetchone() == ("suspended", "chargeback")
        cur.execute("select actor_id::text from core.audit_log where event = 'organisation.suspended' "
                    "and org_id = %s::uuid order by id desc limit 1", (ORG_RIVAL,))
        assert cur.fetchone()[0] == SUPERADMIN

    trail = client.get(f"/api/admin/organisations/{ORG_RIVAL}/audit?event_prefix=organisation.",
                       headers=auth(SUPERADMIN)).json()
    assert trail[0]["event"] == "organisation.suspended" and trail[0]["actor_id"] == SUPERADMIN

    r = client.patch(f"/api/admin/organisations/{ORG_RIVAL}/status",
                     json={"status": "active"}, headers=auth(SUPERADMIN))
    assert r.status_code == 200 and r.json()["suspended_at"] is None


def test_the_owner_of_a_suspended_organisation_is_denied_and_cannot_lift_it(client):
    """The finding the migration closed: the verdict was writable by the
    party it was about. Now the owner's own PATCH is a 404 (no admin surface
    for them), and their PostgREST-shaped UPDATE is a permission error."""
    client.patch(f"/api/admin/organisations/{ORG_RIVAL}/status",
                 json={"status": "suspended", "reason": "test"}, headers=auth(SUPERADMIN))
    with superuser() as conn, conn.cursor() as cur:
        cur.execute("select set_config('request.jwt.claims', %s, true)", ('{"role":"authenticated","sub":"%s"}' % OUTSIDER,))
        cur.execute("set local role authenticated")
        cur.execute("select core.access_mode(%s::uuid, t_advit.product_id())::text", (ORG_RIVAL,))
        assert cur.fetchone()[0] == "denied"
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("update core.organisations set status = 'active' where id = %s::uuid", (ORG_RIVAL,))
        conn.rollback()


def test_a_plan_change_is_read_by_the_tenant_on_the_next_request(client):
    r = client.patch(f"/api/admin/organisations/{ORG_BROADMATE}/subscription",
                     json={"plan_key": "growth"}, headers=auth(SUPERADMIN))
    assert r.status_code == 200, r.text
    assert r.json()["plan_key"] == "growth"
    sub = client.get(f"/api/workspaces/{BROADMATE_WORKSPACE}/billing/subscription", headers=auth(OWNER)).json()
    assert sub["plan_key"] == "growth"


def test_narrowing_a_subscription_needs_a_reason(client):
    r = client.patch(f"/api/admin/organisations/{ORG_BROADMATE}/subscription",
                     json={"status": "grace"}, headers=auth(SUPERADMIN))
    assert r.status_code == 422 and "reason_required" in r.text
    r = client.patch(f"/api/admin/organisations/{ORG_BROADMATE}/subscription",
                     json={"status": "grace", "reason": "invoice 30 days overdue"}, headers=auth(SUPERADMIN))
    assert r.status_code == 200 and r.json()["status"] == "grace"
    r = client.patch(f"/api/admin/organisations/{ORG_BROADMATE}/subscription",
                     json={"plan_key": "platinum"}, headers=auth(SUPERADMIN))
    assert r.status_code == 422
    r = client.patch(f"/api/admin/organisations/{ORG_BROADMATE}/subscription", json={}, headers=auth(SUPERADMIN))
    assert r.status_code == 422


def test_an_override_is_typed_stamped_and_reversible(client):
    url = f"/api/admin/organisations/{ORG_BROADMATE}/entitlements/max_ad_accounts"

    def granted() -> int:
        # The trail is append-only and survives the scrub, so count the delta.
        with superuser() as conn, conn.cursor() as cur:
            cur.execute("select count(*) from core.audit_log where event = 'entitlement.granted' "
                        "and org_id = %s::uuid and payload_json->>'reason' = %s", (ORG_BROADMATE, f"{MARK} pilot"))
            return cur.fetchone()[0]

    before = granted()
    r = client.put(url, json={"value": "five", "reason": f"{MARK} wrong type"}, headers=auth(SUPERADMIN))
    assert r.status_code == 422 and "value_type_mismatch" in r.text
    assert granted() == before, "a refused override leaves no trail claiming it was granted"

    r = client.put(url, json={"value": 5, "reason": f"{MARK} pilot"}, headers=auth(SUPERADMIN))
    assert r.status_code == 200, r.text
    assert r.json()["set_by"] == SUPERADMIN, "set_by comes from the session, not the payload"

    detail = client.get(f"/api/admin/organisations/{ORG_BROADMATE}", headers=auth(SUPERADMIN)).json()
    e = {x["feature_key"]: x for x in detail["entitlements"]}["max_ad_accounts"]
    assert e["source"] == "override" and e["value"] == 5 and e["reason"] == f"{MARK} pilot"

    assert granted() == before + 1

    r = client.delete(url, headers=auth(SUPERADMIN))
    assert r.status_code == 200 and r.json()["previous"] == 5
    detail = client.get(f"/api/admin/organisations/{ORG_BROADMATE}", headers=auth(SUPERADMIN)).json()
    assert {x["feature_key"]: x for x in detail["entitlements"]}["max_ad_accounts"]["source"] == "plan"

    r = client.delete(url, headers=auth(SUPERADMIN))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Platform Watch: the inbox and the act
# ---------------------------------------------------------------------------


def _open_finding(kind: str, subject: str, severity: str = "review") -> str:
    with superuser() as conn, conn.cursor() as cur:
        cur.execute(
            "insert into t_advit.watch_findings (kind, subject, severity, source_url, detail_json) "
            "values (%s, %s, %s, 'https://example.invalid', %s::jsonb) returning id::text",
            (kind, subject, severity, f'{{"marker": "{MARK}"}}'),
        )
        fid = cur.fetchone()[0]
        conn.commit()
    return fid


def test_the_inbox_lists_open_findings_urgent_first_and_acknowledging_closes_one(client):
    low = _open_finding("fetch_failed", f"{MARK}-low", "info")
    high = _open_finding("rule_stale", f"{MARK}-high", "urgent")

    inbox = client.get("/api/admin/watch/findings", headers=auth(SUPERADMIN)).json()
    ids = [f["id"] for f in inbox]
    assert ids.index(high) < ids.index(low)
    assert all(f["acknowledged_at"] is None for f in inbox)

    r = client.post(f"/api/admin/watch/findings/{high}/acknowledge", headers=auth(SUPERADMIN))
    assert r.status_code == 200 and r.json()["acknowledged_at"]
    assert high not in {f["id"] for f in client.get("/api/admin/watch/findings", headers=auth(SUPERADMIN)).json()}
    everything = client.get("/api/admin/watch/findings?include_acknowledged=true", headers=auth(SUPERADMIN)).json()
    acked = next(f for f in everything if f["id"] == high)
    assert acked["acknowledged_by"], "the acknowledger is named, by email"

    r = client.post(f"/api/admin/watch/findings/{high}/acknowledge", headers=auth(SUPERADMIN))
    assert r.status_code == 404, "acknowledging is one act; a second time there is nothing open"


def _a_stale_meta_rule() -> str:
    with superuser() as conn, conn.cursor() as cur:
        cur.execute("select code from t_advit.policy_rules where jurisdiction = 'meta' and severity = 'block' "
                    "order by code limit 1")
        return cur.fetchone()[0]


def test_the_rules_page_agrees_with_the_nightly_job_about_what_is_stale(client):
    body = client.get("/api/admin/rules", headers=auth(SUPERADMIN)).json()
    meta = [r for r in body["rules"] if r["jurisdiction"] == "meta"]
    assert meta and all(r["stale"] for r in meta), "the seed's Meta rules are 2026-03-01: past a 90-day window"
    assert all(r["window_days"] == 90 for r in meta)
    assert all("pattern" not in r for r in body["rules"]), "the pattern is not the console's to see or edit"


def test_reverifying_a_rule_moves_as_of_to_today_and_closes_its_finding(client):
    code = _a_stale_meta_rule()
    fid = _open_finding("rule_stale", code, "urgent")

    r = client.post(f"/api/admin/rules/{code}/reverify", json={}, headers=auth(SUPERADMIN))
    assert r.status_code == 200, r.text
    assert r.json()["finding_closed"] is True
    # IST today, not UTC today: the trigger and the route agree on the clock.
    assert date.fromisoformat(r.json()["as_of"]) >= date.today() - timedelta(days=1)

    with superuser() as conn, conn.cursor() as cur:
        cur.execute("select as_of from t_advit.policy_rules where code = %s", (code,))
        assert cur.fetchone()[0] > date(2026, 3, 1)
        cur.execute("select acknowledged_by::text from t_advit.watch_findings where id = %s::uuid", (fid,))
        assert cur.fetchone()[0] == SUPERADMIN
        cur.execute("select count(*) from core.audit_log where event = 'rule.reverified' "
                    "and payload_json->>'subject' = %s and actor_id = %s::uuid", (code, SUPERADMIN))
        assert cur.fetchone()[0] >= 1

    rules = {x["code"]: x for x in client.get("/api/admin/rules", headers=auth(SUPERADMIN)).json()["rules"]}
    assert rules[code]["stale"] is False


def test_a_reverification_cannot_be_dated_in_the_future_or_backwards(client):
    code = _a_stale_meta_rule()
    tomorrow = (date.today() + timedelta(days=2)).isoformat()
    r = client.post(f"/api/admin/rules/{code}/reverify", json={"as_of": tomorrow}, headers=auth(SUPERADMIN))
    assert r.status_code == 422 and "as_of_future" in r.text
    r = client.post(f"/api/admin/rules/{code}/reverify", json={"as_of": "2026-01-01"}, headers=auth(SUPERADMIN))
    assert r.status_code == 422 and "as_of_moves_forward" in r.text
    r = client.post("/api/admin/rules/NO_SUCH_RULE/reverify", json={}, headers=auth(SUPERADMIN))
    assert r.status_code == 404


def test_reverifying_does_not_reach_the_pattern(client):
    """Structural: the route's SQL names as_of and nothing else, and the column
    grant would refuse anything else. Checked by reading the route's source for
    the SET clause rather than by trying every column."""
    import inspect

    from app import routes_admin

    src = inspect.getsource(routes_admin._reverify)
    assert "set as_of" in src and "pattern" not in src and "severity" not in src


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------


def test_drafts_come_first_and_say_why_they_are_drafts(client):
    with superuser() as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into core.invoices (org_id, subscription_id, status, period_start, period_end,
                                       plan_key, plan_name, list_price_inr, coupon_code, taxable_inr,
                                       gst_rate_percent, sac_code, gst_split, total_inr,
                                       buyer_state_code, seller_gstin)
            values (%s::uuid, (select id from core.subscriptions where org_id = %s::uuid and cancelled_at is null),
                    'draft', '2031-01-01', '2031-02-01', 'standard', 'Standard', 100, %s, 100,
                    18, '998314', 'igst', 100, null, '09AAAAA0000A1Z5')
            """,
            (ORG_RIVAL, ORG_RIVAL, MARK),
        )
        conn.commit()

    rows = client.get("/api/admin/invoices", headers=auth(SUPERADMIN)).json()
    mine = next(r for r in rows if r["coupon_code"] == MARK)
    assert rows.index(mine) == 0 or all(r["status"] == "draft" for r in rows[: rows.index(mine)])
    assert mine["draft_reason"] == "buyer state code missing on the organisation"
    assert mine["org_name"]

    only_drafts = client.get("/api/admin/invoices?status=draft", headers=auth(SUPERADMIN)).json()
    assert all(r["status"] == "draft" for r in only_drafts)


def test_a_tenant_owner_sees_only_their_own_invoices_still(client):
    """The admin route did not widen the tenant's read: the tenant route is
    unchanged and RLS is what answers it."""
    r = client.get(f"/api/workspaces/{RIVAL_WORKSPACE}/billing/invoices", headers=auth(OUTSIDER))
    assert r.status_code == 200
    assert all(i.get("org_id", ORG_RIVAL) == ORG_RIVAL for i in r.json())


def test_the_catalogue_shows_every_plan_including_inactive_ones(client):
    plans = {p["key"]: p for p in client.get("/api/admin/plans", headers=auth(SUPERADMIN)).json()}
    assert {"starter", "growth", "scale", "agency", "standard"} <= set(plans)
    assert plans["standard"]["subscriptions"] >= 2
    assert plans["growth"]["features"]["max_ad_accounts"] == 2
