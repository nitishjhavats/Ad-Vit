"""HTTP surface.

Covers the endpoints that work without a model key. The assertions worth
noting are the negative ones: that no route can bypass the policy layer, that a
rejection without a reason is refused, and that the compliance response never
claims a clean bill of health it cannot give.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.main import app
from conftest import OUTSIDER, OWNER, auth

DSN = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
)

WORKSPACE = "00000000-0000-4000-8000-000000000050"
FUNDED_ACCOUNT = "1000000000000001"
WRITABLE_ACCOUNT = "1000000000000003"


def _reachable() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception:
        return False


DB_UP = _reachable()
needs_db = pytest.mark.skipif(not DB_UP, reason="local Supabase Postgres is not running")


@pytest.fixture(scope="module")
def client() -> TestClient:
    """Signed in as the workspace owner.

    Every test in this file predates authentication and almost none of them is
    ABOUT it, so the default client carries a valid owner token and the tests
    stay about what they were about. The ones that are about auth build their
    own client, or send their own header, which is the point at which the
    header becomes visible again.
    """
    c = TestClient(app)
    c.headers.update(auth(OWNER))
    return c


@pytest.fixture
def anonymous() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def test_health_publishes_nothing_operational(client):
    """This test used to assert the opposite, and defended it as "the two facts
    an operator needs before trusting the process with an ad account".

    That is true of an operator and false of an anonymous caller, and an
    unauthenticated route cannot tell them apart. `write_allowlist` is the list
    of ad accounts this process may spend money on; the database error string
    carries the DSN and internal hostnames. Neither belongs in a response
    anybody can fetch.

    Operational detail moves behind authentication with the rest of the API.
    Until then it is not published, and this test is what stops it coming back.
    """
    body = client.get("/health").json()
    assert body["status"] in {"ok", "degraded"}, "it must still report liveness"
    for leaked in ("write_allowlist", "meta_driver", "models_configured", "database"):
        assert leaked not in body, f"/health is public and must not publish {leaked}"


# ---------------------------------------------------------------------------
# Compliance
# ---------------------------------------------------------------------------


@needs_db
def test_schedule_j_copy_is_blocked_with_full_provenance(client):
    body = client.post(
        "/api/compliance/check",
        json={
            "primary_text": "Kya aap piles se pareshan hain? Sirf 7 din mein result.",
            "business_type": "ayurveda",
            "has_media": True,
        },
    ).json()

    assert body["verdict"] == "block"
    assert body["layers"] == {"meta": "block", "india": "block"}

    schedule_j = next(f for f in body["findings"] if f["rule_code"] == "IN_DMRA_SCHEDULE_J")
    assert schedule_j["offending_span"] == "piles"
    assert schedule_j["span"] is not None
    assert schedule_j["source_url"].startswith("https://")
    assert schedule_j["as_of"] == "2026-08-01"
    assert schedule_j["needs_legal_verification"] is True
    assert schedule_j["suggested_rewrite"]


@needs_db
def test_clean_copy_is_reported_not_evaluated_never_pass(client):
    """The response must not imply an approval guarantee the OS does not make
    (PRD 4.5)."""
    body = client.post(
        "/api/compliance/check",
        json={
            "primary_text": "An Ayurvedic formulation prepared in the traditional manner.",
            "business_type": "ayurveda",
            "ayush_licence_no": "UP-AYUR-12345",
            "product_classification": "ayurvedic_drug",
        },
    ).json()

    assert body["findings"] == []
    assert body["verdict"] == "not_evaluated"
    assert set(body["stages_skipped"]) >= {7, 8}


@needs_db
def test_stage_coverage_sets_are_disjoint_over_http(client):
    body = client.post(
        "/api/compliance/check",
        json={"primary_text": "Results in 15 days.", "business_type": "ayurveda",
              "has_media": True, "ayush_licence_no": "UP-1",
              "product_classification": "ayurvedic_drug"},
    ).json()

    ev = set(body["stages_evaluated"])
    pa = set(body["stages_partial"])
    sk = set(body["stages_skipped"])
    assert not (ev & pa or ev & sk or pa & sk)


@needs_db
def test_response_surfaces_stale_rules(client):
    """So the caller can say it needs to verify the current rule rather than
    asserting a six-month-old one (PRD 13.5)."""
    body = client.post(
        "/api/compliance/check",
        json={"primary_text": "hello", "business_type": "ayurveda"},
    ).json()

    assert body["ruleset"]["rule_count"] >= 10
    assert isinstance(body["ruleset"]["stale_rules"], list)
    assert any(c.startswith("META_") for c in body["ruleset"]["stale_rules"])


@needs_db
def test_general_pack_does_not_load_schedule_j(client):
    body = client.post(
        "/api/compliance/check",
        json={"primary_text": "We cleared our piles of stock.", "business_type": "general_d2c"},
    ).json()
    assert not any(f["rule_code"] == "IN_DMRA_SCHEDULE_J" for f in body["findings"])


# ---------------------------------------------------------------------------
# Account audit
# ---------------------------------------------------------------------------


def test_audit_flags_the_missing_dataset_as_blocking(client):
    """Measurement readiness is a gate, not a report: the closed loop cannot
    run without a dataset (PRD 12.2, FR-008)."""
    body = client.get(f"/api/audit/account/{FUNDED_ACCOUNT}").json()

    assert body["measurement_ready"] is False
    codes = {f["code"] for f in body["findings"]}
    assert "NO_DATASET" in codes

    blocking = [f for f in body["findings"] if f["severity"] == "blocking"]
    assert [f["code"] for f in blocking] == ["NO_DATASET"]
    assert body["score"] < 50, "a blocking measurement gap must dominate the score"


def test_audit_flags_meta_default_names_and_fragmentation(client):
    body = client.get(f"/api/audit/account/{FUNDED_ACCOUNT}").json()
    codes = {f["code"] for f in body["findings"]}
    assert "META_DEFAULT_NAMES" in codes
    assert "FRAGMENTED_STRUCTURE" in codes


def test_audit_reports_missing_ad_data_as_a_gap_not_a_verdict(client):
    """An unread dimension must not read as a clean one."""
    body = client.get(f"/api/audit/account/{FUNDED_ACCOUNT}").json()
    finding = next(f for f in body["findings"] if f["code"] == "NO_ADS_VISIBLE")
    assert finding["severity"] == "info"
    assert "not a verdict" in finding["detail"]


def test_unknown_account_is_404_not_a_silent_empty_audit(client):
    assert client.get("/api/audit/account/9999999999").status_code == 404


# ---------------------------------------------------------------------------
# Connection health
# ---------------------------------------------------------------------------


@needs_db
def test_connection_health_distinguishes_intent_from_effective_autonomy(client):
    body = client.get(
        f"/api/workspaces/{WORKSPACE}/connections/health"
    ).json()

    automation = body["automation"]
    assert "autonomy_level" in automation
    assert "effective_autonomy" in automation
    assert "capped_by_plan" in automation


@needs_db
def test_connection_health_shows_funded_accounts_as_read_only(client):
    """The seeded posture: the two funded accounts cannot be written to."""
    body = client.get(
        f"/api/workspaces/{WORKSPACE}/connections/health"
    ).json()

    by_id = {c["ad_account_id"]: c for c in body["meta_connections"]}
    assert by_id[FUNDED_ACCOUNT]["write_enabled"] is False
    assert by_id["1000000000000002"]["write_enabled"] is False
    assert by_id[WRITABLE_ACCOUNT]["write_enabled"] is True


@needs_db
def test_unknown_workspace_is_404(client):
    resp = client.get(
        f"/api/workspaces/{uuid.uuid4()}/connections/health"
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


@pytest.fixture
def pending_approval():
    """A decision plus a pending approval, cleaned up afterwards."""
    decision_id = str(uuid.uuid4())
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into t_advit.decisions
              (id, workspace_id, decision_type, chosen_option, reasoning,
               expected_effect_json, horizon_days)
            values (%s, %s, 'budget_change', 'option_a', 'api test', '{}'::jsonb, 7)
            """,
            (decision_id, WORKSPACE),
        )
        cur.execute(
            """
            insert into t_advit.approvals
              (decision_id, workspace_id, risk_class, proposed_json, impact_inr, expires_at)
            values (%s, %s, 'critical', '{"tool": "activate_entity"}'::jsonb, 2500,
                    now() + interval '4 hours')
            returning id::text
            """,
            (decision_id, WORKSPACE),
        )
        approval_id = cur.fetchone()[0]

    yield approval_id

    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("delete from t_advit.decisions where id = %s", (decision_id,))


@needs_db
def test_pending_approvals_are_listed_with_their_evidence(client, pending_approval):
    body = client.get(f"/api/workspaces/{WORKSPACE}/approvals").json()
    row = next(r for r in body if r["id"] == pending_approval)

    assert row["risk_class"] == "critical"
    assert row["expired"] is False
    # The proposal must arrive with the reasoning and horizon attached, not as
    # a bare instruction to approve.
    assert row["reasoning"] == "api test"
    assert row["horizon_days"] == 7
    assert float(row["impact_inr"]) == 2500.0


@needs_db
def test_rejection_without_a_reason_is_refused(client, pending_approval):
    """A rejection reason is a training signal and is stored as one
    (FR-014)."""
    resp = client.post(
        f"/api/approvals/{pending_approval}/respond", json={"action": "reject"}
    )
    assert resp.status_code == 422
    assert "reason is required" in resp.json()["detail"]


@needs_db
def test_approval_can_be_granted_once(client, pending_approval):
    first = client.post(
        f"/api/approvals/{pending_approval}/respond", json={"action": "approve"}
    )
    assert first.status_code == 200
    assert first.json()["status"] == "approved"

    # Answering twice must conflict rather than silently re-approve.
    second = client.post(
        f"/api/approvals/{pending_approval}/respond", json={"action": "approve"}
    )
    assert second.status_code == 409


@needs_db
def test_the_approval_signature_comes_from_the_session_and_not_the_payload(
    client, pending_approval
):
    """`responded_by` used to be a caller-supplied field.

    It is the human signature on the row that DISCHARGES an approval - the row
    pipeline step 6 redeems before spending money - so a client could name
    anybody as the person who approved, and the approval trail recorded what the
    client typed rather than who signed.

    The extra field is sent here on purpose: it must be IGNORED, not honoured.
    """
    impostor = "00000000-0000-4000-8000-0000000000ff"
    resp = client.post(
        f"/api/approvals/{pending_approval}/respond",
        json={"action": "approve", "responded_by": impostor},
    )
    assert resp.status_code == 200
    assert resp.json()["responded_by"] == OWNER, (
        "the approval is signed by whoever the caller named"
    )


@needs_db
def test_another_tenant_cannot_answer_this_approval(client, pending_approval):
    """The approval id is a UUID and nothing else. Without a resolver, knowing
    one is enough to authorise somebody else's spending."""
    outsider = TestClient(app)
    outsider.headers.update(auth(OUTSIDER))
    resp = outsider.post(
        f"/api/approvals/{pending_approval}/respond", json={"action": "approve"}
    )
    assert resp.status_code == 404


@needs_db
def test_rejection_with_a_reason_is_recorded(client, pending_approval):
    resp = client.post(
        f"/api/approvals/{pending_approval}/respond",
        json={"action": "reject", "reason": "RTO is climbing; not scaling this week"},
    )
    assert resp.status_code == 200

    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "select status::text, reject_reason from t_advit.approvals where id = %s",
            (pending_approval,),
        )
        status, reason = cur.fetchone()
    assert status == "rejected"
    assert "RTO" in reason


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


@needs_db
def test_rollback_of_unknown_action_is_404(client):
    assert client.post(f"/api/actions/{uuid.uuid4()}/rollback").status_code == 404


@needs_db
def test_dashboard_states_its_own_data_gaps(client):
    """Gaps are marked, never interpolated, so an empty series must be
    explained rather than looking like zero performance."""
    body = client.get(f"/api/workspaces/{WORKSPACE}/dashboard").json()
    assert "money" in body and "platform" in body
    assert "never" in body["note"] and "interpolated" in body["note"]


# ---------------------------------------------------------------------------
# Branding - one source, so the name cannot drift across surfaces
# ---------------------------------------------------------------------------


def test_brand_lockup_is_product_then_company_then_site(client):
    body = client.get("/api/brand").json()
    assert body["product_name"] == "ad-vit"
    assert body["company_name"] == "Broadmate Global"
    assert body["byline"] == "by Broadmate Global"
    assert body["website_url"] == "https://broadmate.org"


def test_brand_html_makes_the_site_a_real_link(client):
    body = client.get("/api/brand", params={"format": "html"}).json()
    html = body["html"]
    assert '<a href="https://broadmate.org"' in html
    assert 'rel="noopener noreferrer"' in html
    # The by-line must be secondary markup, not another heading.
    assert '<p class="brand-byline">by Broadmate Global</p>' in html
    assert "brand-byline" in body["css"]


def test_brand_markdown_links_the_site(client):
    md = client.get("/api/brand", params={"format": "markdown"}).json()["markdown"]
    assert "# ad-vit" in md
    assert "[broadmate.org](https://broadmate.org)" in md
    # The by-line is rendered smaller, not as a second heading.
    assert "<sub>by Broadmate Global" in md


def test_health_carries_the_identity(client):
    body = client.get("/health").json()
    assert body["product"] == "ad-vit"
    assert body["by"] == "Broadmate Global"
    assert body["website"] == "https://broadmate.org"


def test_api_metadata_carries_the_company(client):
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "ad-vit"
    assert schema["info"]["contact"]["name"] == "Broadmate Global"
    # Pydantic normalises the URL with a trailing slash; the assertion is
    # about identity, not URL normalisation.
    assert schema["info"]["contact"]["url"].rstrip("/") == "https://broadmate.org"


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------


@needs_db
def test_chat_returns_the_facts_behind_the_answer(client):
    """A proposal the owner cannot interrogate is one they should not approve,
    so the run returns its facts and the records they came from."""
    body = client.post(
        f"/api/workspaces/{WORKSPACE}/chat",
        json={"message": "kal ka CPL kyun badha?"},
    ).json()

    assert body["run_id"]
    assert "facts" in body
    assert isinstance(body["retrieved_record_ids"], list)
    assert "cost" in body and "inr" in body["cost"]


@needs_db
def test_chat_blocks_a_schedule_j_creative_and_spends_nothing(client):
    body = client.post(
        f"/api/workspaces/{WORKSPACE}/chat",
        json={
            "message": "Ye chala do",
            "creative": {"primary_text": "Piles ka permanent ilaj, guaranteed result"},
        },
    ).json()

    assert body["compliance"]["verdict"] == "block"
    assert body["mode"] == "halted"
    # The whole point of gating before the paid agents.
    assert body["cost"]["inr"] == 0
    assert "dmr_act_schedule_j" in body["narration"]


@needs_db
def test_chat_does_not_treat_a_budget_question_as_ad_copy(client):
    body = client.post(
        f"/api/workspaces/{WORKSPACE}/chat",
        json={"message": "Budget 5000 se 20000 kar do"},
    ).json()
    assert body["compliance"]["verdict"] == "not_applicable"


@needs_db
def test_chat_reports_an_unknown_workspace_rather_than_inventing_context(client):
    resp = client.post(
        f"/api/workspaces/{uuid.uuid4()}/chat",
        json={"message": "hello"},
    )
    # The refusal now comes from authorized_workspace, before a run starts -
    # and 404 rather than 403 on purpose, because a 403 would confirm the
    # workspace exists and rebuild the enumeration oracle 20260910000001 closed.
    assert resp.status_code == 404


@needs_db
def test_chat_refuses_a_caller_supplied_ayush_licence_rather_than_ignoring_it(client):
    """The field was the only input to a BLOCK rule, so posting a made-up
    licence number cleared a statutory block while the workspace's own product
    row held NULL. It now comes from t_advit.catalog_products.

    Refused loudly rather than dropped quietly. /api/chat has no
    authentication, so a client that keeps sending a licence number and keeps
    getting 200s would go on believing it controls the verdict.
    """
    resp = client.post(
        f"/api/workspaces/{WORKSPACE}/chat",
        json={
            "message": "Ye chala do",
            "creative": {"primary_text": "A traditional preparation"},
            "ayush_licence_no": "FAKE-NOT-A-LICENCE",
        },
    )
    assert resp.status_code == 422
    assert "catalog_products" in resp.json()["detail"]
    assert "creative.product_sku" in resp.json()["detail"]


@needs_db
def test_chat_reports_which_product_and_which_pack_governed_the_verdict(client):
    """Why did it say that must be answerable from the response. The pack
    decides whether the Indian statutory layer loaded at all, and the product
    decides whose licence was read."""
    body = client.post(
        f"/api/workspaces/{WORKSPACE}/chat",
        json={
            "message": "Ye chala do",
            "creative": {"primary_text": "A traditional preparation",
                         "product_sku": "PC-001"},
        },
    ).json()

    assert body["compliance"]["business_type"] == "ayurveda"
    assert body["compliance"]["licence_posture"]["source"] == "catalogue"
    assert body["compliance"]["licence_posture"]["sku"] == "PC-001"


def test_chat_requires_a_message(client):
    resp = client.post(f"/api/workspaces/{WORKSPACE}/chat", json={"message": ""})
    assert resp.status_code == 422


@needs_db
def test_a_provider_failure_degrades_rather_than_discarding_the_run(client, monkeypatch):
    """Confirmed live when OpenRouter returned 402: the whole turn became a 404
    and threw away the compliance verdict, the computed facts and the data gaps
    - all of which were produced deterministically before any model ran.

    Quality degrades, availability does not (PRD 14.6)."""
    from app.models.router import AllModelsFailed, ModelRouter

    def exploding(self, role, **kwargs):
        raise AllModelsFailed(role, [("stub", "HTTP 402: out of credits")])

    # Patched on the class so it reaches whichever router instance the graph
    # happens to hold, without reaching into LangGraph's node internals.
    monkeypatch.setattr(ModelRouter, "complete_json", exploding)
    monkeypatch.setattr(ModelRouter, "complete", exploding)

    body = client.post(
        f"/api/workspaces/{WORKSPACE}/chat",
        json={"message": "Budget 5000 se 20000 kar do"},
    ).json()

    assert body["degraded"] is True
    assert "402" in body["degraded_reason"]
    # The deterministic half survives, which is the whole point.
    assert body["compliance"]["verdict"] == "not_applicable"
    assert body["facts"]
    assert body["narration"], "a degraded run must still say something"

# ---------------------------------------------------------------------------
# The daily business-truth intake
#
# The product's heartbeat (PRD 12.1), and it had no test at all.
# ---------------------------------------------------------------------------

DAILY_TRUTH_DATE = "2026-09-01"


def _truth_row(date=DAILY_TRUTH_DATE):
    import psycopg

    from app.config import get_settings

    with psycopg.connect(get_settings().database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            select total_orders, confirmed_orders, cancelled_orders, revenue_inr,
                   delivered_orders, delivered_revenue_inr, sales_feedback
              from t_advit.business_truth
             where workspace_id = %s and date = %s::date
            """,
            (WORKSPACE, date),
        )
        return cur.fetchone()


def _clear_truth(date=DAILY_TRUTH_DATE):
    import psycopg

    from app.config import get_settings

    with psycopg.connect(get_settings().database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "delete from t_advit.business_truth where workspace_id = %s and date = %s::date",
            (WORKSPACE, date),
        )
        conn.commit()


def test_a_later_partial_report_does_not_erase_the_earlier_one(client):
    """A day is reported in more than one message by design.

    The owner sends orders and revenue at 20:30; delivered orders arrive days
    later against that same back-dated date, because COD delivery lags. The
    upsert wrote `excluded.x` for every column, so the second message
    overwrote everything it did not carry with NULL - a delivery update that
    knew only delivered_orders destroyed 42 orders, 28 confirmed, Rs 61,000 and
    the sales team's notes.

    Losing them was not even the worst part. NULL is this schema's marker for
    "not reported", so the day then looked like a gap the owner had never
    filled in, and every figure derived from it quietly lost its denominator.
    """
    _clear_truth()
    try:
        evening = client.post(
            f"/api/workspaces/{WORKSPACE}/daily-truth",
            json={
                "date": DAILY_TRUTH_DATE,
                "message": "42 order aaye, 28 confirm, 9 cancel, 61000 revenue",
                "sales_feedback": "callers asked about delivery time",
            },
        )
        assert evening.status_code == 200, evening.text
        assert evening.json()["accepted"] is True

        before = _truth_row()
        assert before[0] == 42 and before[1] == 28

        # Days later. Only the delivered figures are known.
        late = client.post(
            f"/api/workspaces/{WORKSPACE}/daily-truth",
            json={
                "date": DAILY_TRUTH_DATE,
                "values": {"delivered_orders": 24},
            },
        )
        assert late.status_code == 200, late.text

        after = _truth_row()
        assert after[4] == 24, "the late delivery figure must land"
        assert after[0] == 42, "the evening's order count must survive it"
        assert after[1] == 28
        assert after[2] == 9
        assert float(after[3]) == 61000.0
        assert after[6] == "callers asked about delivery time", (
            "free text is the hardest thing to re-enter and the easiest to lose"
        )
    finally:
        _clear_truth()


def test_a_correction_still_overwrites(client):
    """Preserving unsent fields must not freeze the sent ones. Correcting a
    number is sending a different one, and that has to keep working."""
    _clear_truth()
    try:
        client.post(
            f"/api/workspaces/{WORKSPACE}/daily-truth",
            json={
                "date": DAILY_TRUTH_DATE,
                "message": "42 order aaye, 28 confirm, 9 cancel, 61000 revenue",
            },
        )
        client.post(
            f"/api/workspaces/{WORKSPACE}/daily-truth",
            json={
                "date": DAILY_TRUTH_DATE,
                "values": {"total_orders": 40, "confirmed_orders": 27},
            },
        )
        row = _truth_row()
        assert row[0] == 40 and row[1] == 27, "a correction must land"
        assert row[2] == 9, "and must not disturb what it did not mention"
    finally:
        _clear_truth()


# ---------------------------------------------------------------------------
# The rollback window
#
# rollback_expires_at was SELECTed and never compared to now(), so the limit the
# store writes (now() + 24 hours) and the schema calls rollback's "visible time
# limit" existed in the column and nowhere else.
# ---------------------------------------------------------------------------


@pytest.fixture
def rollback_action():
    """Build an executed action carrying a rollback handle, and take it away again.

    The cleanup is not tidiness. `workspace_policy` computes committed daily
    spend from `t_advit.actions.after_state_json->>'daily_budget_inr'` on today's
    verified, un-rolled-back rows - so an action fixture that commits and stays
    is indistinguishable from a real activation, and the next cap test in the
    session finds the ceiling already part-spent. Leaving these behind broke
    test_the_daily_cap_accumulates_across_sequential_activations, two files
    away, in a way that looked like a cap bug.
    """
    made: list[tuple[str, str]] = []

    def build(expires_sql: str) -> str:
        with psycopg.connect(DSN) as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into t_advit.decisions
                  (workspace_id, decision_type, reasoning, expected_effect_json,
                   horizon_days, confidence)
                values (%s, 'budget_change', 'rollback window fixture', '{}'::jsonb, 7, 0.5)
                returning id::text
                """,
                (WORKSPACE,),
            )
            decision_id = cur.fetchone()[0]

            cur.execute(
                f"""
                insert into t_advit.actions
                  (decision_id, workspace_id, action_type, risk_class,
                   meta_request_json, idempotency_key, verified, executed_at,
                   after_state_json, rollback_handle, rollback_expires_at)
                values (%s, %s, 'update_budget', 'high', '{{}}'::jsonb, %s, true, now(),
                        '{{"id": "1", "daily_budget_inr": 2000, "status": "ACTIVE"}}'::jsonb,
                        '{{"kind": "restore_budget", "ad_account_id": "{WRITABLE_ACCOUNT}",
                           "entity_id": "1", "daily_budget_inr": 1000}}'::jsonb,
                        {expires_sql})
                returning id::text
                """,
                (decision_id, WORKSPACE, f"idem-rollback-{uuid.uuid4()}"),
            )
            action_id = cur.fetchone()[0]
            conn.commit()
        made.append((action_id, decision_id))
        return action_id

    yield build

    # Anything the pipeline wrote during the test goes too - the in-window case
    # actually executes, so it leaves an action row of its own.
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        for action_id, decision_id in made:
            cur.execute(
                "delete from t_advit.actions where decision_id = %s", (decision_id,)
            )
            cur.execute("delete from t_advit.outcomes where decision_id = %s", (decision_id,))
            cur.execute("delete from t_advit.decisions where id = %s", (decision_id,))
        conn.commit()


@needs_db
def test_a_rollback_past_its_window_is_refused(client, rollback_action):
    """The finding. The route guarded on rolled_back_at and on the presence of a
    handle, and never on the clock."""
    action_id = rollback_action("now() - interval '1 hour'")
    resp = client.post(f"/api/actions/{action_id}/rollback")

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "window" in detail and "closed" in detail
    # The reason matters as much as the refusal: undo is not free after a day,
    # because spend has accrued and later changes may sit on top of this one.
    assert "spend has accrued" in detail


@needs_db
def test_a_rollback_handle_with_no_expiry_is_refused_not_permitted(client, rollback_action):
    """A handle with no expiry is a row written before the store set one, or by
    a path that skipped it. There is nothing to compare against, and the
    alternative reading - "no expiry means it never expires" - would make the
    least-known row the most permissive one."""
    action_id = rollback_action("null")
    resp = client.post(f"/api/actions/{action_id}/rollback")

    assert resp.status_code == 409
    assert "no expiry" in resp.json()["detail"]


@needs_db
def test_a_rollback_inside_its_window_is_still_allowed(client, rollback_action):
    """The counterpart. A guard that refuses everything is not a guard, and
    would pass both tests above."""
    action_id = rollback_action("now() + interval '6 hours'")
    resp = client.post(f"/api/actions/{action_id}/rollback")

    assert resp.status_code == 200, resp.text
    assert resp.json()["decision"] in {"executed", "denied"}
    # Whatever the pipeline decides, the route must not have refused it on the
    # clock - that is the property under test.
    assert "window" not in resp.text
