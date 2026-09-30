"""The reports read model: what it shows, what it refuses to invent, and whose
rows it can see.

The assertions that matter are the negative ones. A report is the surface an
owner reads before deciding whether to scale, so the failures worth guarding
against are the quiet ones: an unknown rendered as a zero, another tenant's
learning rendered as this account's, and a series that disagrees with the
dashboard beside it.

Every row these tests create carries the marker below and is scrubbed by it
before and after, so a crashed run leaves nothing a later run mistakes for
its own.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.main import app
from conftest import BROADMATE_WORKSPACE, OUTSIDER, OWNER, RIVAL_WORKSPACE, auth

DSN = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
)

MARKER = "test_reports marker"

# Far enough back that no sync fixture writes spend there, close enough to sit
# inside the widest window the tests ask for.
UNREPORTED_DAY = date.today() - timedelta(days=45)

DASHBOARD_MONEY_KEYS = (
    "date", "blended_cac_inr", "contribution_margin_inr", "confirm_rate",
    "rto_rate", "delivered_aov_inr", "mer", "computed_at",
)


def _reachable() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _reachable(), reason="local Supabase Postgres is not running")


def _scrub() -> None:
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("delete from t_advit.learnings where statement like %s", (f"%{MARKER}%",))
        # Approvals and outcomes hang off their decision and cascade with it.
        cur.execute("delete from t_advit.decisions where reasoning = %s", (MARKER,))
        cur.execute(
            "delete from t_advit.blended_daily where workspace_id = %s::uuid and date = %s",
            (BROADMATE_WORKSPACE, UNREPORTED_DAY),
        )
        cur.execute("delete from t_advit.business_truth where business_issues = %s", (MARKER,))
        cur.execute("delete from t_advit.held_proposals where reason = %s", (MARKER,))


@pytest.fixture(autouse=True)
def _clean_before_and_after():
    _scrub()
    yield
    _scrub()


@pytest.fixture(scope="module")
def client() -> TestClient:
    c = TestClient(app)
    c.headers.update(auth(OWNER))
    return c


def report(client: TestClient, workspace: str = BROADMATE_WORKSPACE, **params) -> dict:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    resp = client.get(f"/api/workspaces/{workspace}/reports" + (f"?{query}" if query else ""))
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Who may read it
# ---------------------------------------------------------------------------


@needs_db
def test_a_non_member_gets_404_not_an_empty_report():
    """The same answer the rest of the tenant surface gives: a workspace that is
    not yours does not exist, rather than existing and being empty."""
    c = TestClient(app)
    resp = c.get(f"/api/workspaces/{BROADMATE_WORKSPACE}/reports", headers=auth(OUTSIDER))
    assert resp.status_code == 404


@needs_db
def test_an_unauthenticated_request_is_refused():
    assert TestClient(app).get(f"/api/workspaces/{BROADMATE_WORKSPACE}/reports").status_code == 401


# ---------------------------------------------------------------------------
# The period
# ---------------------------------------------------------------------------


@needs_db
def test_the_period_is_clamped_to_between_seven_and_ninety_days(client):
    assert report(client)["period"]["days"] == 30
    assert report(client, days=1)["period"]["days"] == 7
    assert report(client, days=400)["period"]["days"] == 90
    assert report(client, days=45)["period"]["days"] == 45


@needs_db
def test_the_period_bounds_are_dates_the_window_actually_spans(client):
    body = report(client, days=14)
    start = date.fromisoformat(body["period"]["from"])
    end = date.fromisoformat(body["period"]["to"])
    assert (end - start).days == 14
    assert end == date.today()


# ---------------------------------------------------------------------------
# Unknown is not zero
# ---------------------------------------------------------------------------


@needs_db
def test_an_unknown_input_stays_null_and_is_never_reported_as_zero(client):
    """A day the owner reported only delivered orders for: no revenue, no
    spend ingested, no returns count. Every figure derived from those is
    unknown, and the report must say so rather than print a 0 that reads as
    free acquisition or as break-even."""
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into t_advit.business_truth
              (date, workspace_id, delivered_orders, business_issues)
            values (%s, %s::uuid, 30, %s)
            """,
            (UNREPORTED_DAY, BROADMATE_WORKSPACE, MARKER),
        )
        # The economics engine's own answer for that day, so the test reads
        # the row the product would have written rather than one shaped here.
        cur.execute(
            "select t_advit.compute_blended_daily(%s::uuid, %s)",
            (BROADMATE_WORKSPACE, UNREPORTED_DAY),
        )

    body = report(client, days=60)
    row = next(r for r in body["money"] if r["date"] == UNREPORTED_DAY.isoformat())

    assert row["delivered_orders"] == 30
    for metric in ("spend_inr", "delivered_revenue_inr", "blended_cac_inr", "mer",
                   "contribution_margin_inr", "rto_rate", "confirm_rate"):
        assert row[metric] is None, f"{metric} was {row[metric]!r}; an unknown is not a number"
    assert "spend_not_ingested" in row["gaps"]
    assert "rto_not_reported" in row["gaps"]


@needs_db
def test_a_total_is_never_a_number_when_its_inputs_are_unknown(client):
    """`sum()` over no rows is NULL and the route must not coalesce it, and a
    quotient of an unknown is unknown. Asserted as a property of whatever the
    period holds, because the rule has to hold for every period."""
    totals = report(client, days=7)["totals"]
    coverage = totals["coverage"]

    if coverage["spend_days"] == 0:
        assert totals["spend_inr"] is None
    if coverage["reported_days"] == 0:
        for key in ("delivered_revenue_inr", "delivered_orders", "rto_rate", "confirm_rate"):
            assert totals[key] is None, f"{key} with nothing reported was {totals[key]!r}"
    if coverage["margin_days"] == 0:
        assert totals["contribution_margin_inr"] is None

    if totals["spend_inr"] is None or totals["delivered_orders"] is None:
        assert totals["blended_cac_inr"] is None
    if totals["spend_inr"] is None or totals["delivered_revenue_inr"] is None:
        assert totals["mer"] is None


@needs_db
def test_week_over_week_has_no_direction_when_a_side_is_unknown(client):
    """A direction needs two numbers. If last week had no ingested spend, the
    change is unknown - not "up from zero"."""
    wow = report(client)["week_over_week"]
    for key in ("spend_inr", "blended_cac_inr", "mer"):
        entry = wow[key]
        if entry["this"] is None or entry["previous"] is None:
            assert entry["direction"] is None
            assert entry["change_pct"] is None
        else:
            assert entry["direction"] in {"up", "down", "flat"}
    assert wow["blended_cac_inr"]["better_when"] == "down"
    assert wow["mer"]["better_when"] == "up"
    assert wow["spend_inr"]["better_when"] is None


# ---------------------------------------------------------------------------
# Agreement with the dashboard
# ---------------------------------------------------------------------------


@needs_db
def test_thirty_days_returns_the_same_money_rows_as_the_dashboard(client):
    """Two surfaces reading the same table with different filters would show
    the owner two different months. The report's series is the dashboard's
    series with the inputs joined on, in the same order."""
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into t_advit.business_truth
              (date, workspace_id, total_orders, confirmed_orders, delivered_orders,
               delivered_revenue_inr, business_issues)
            values (%s, %s::uuid, 40, 27, 20, 61000, %s)
            """,
            (UNREPORTED_DAY, BROADMATE_WORKSPACE, MARKER),
        )
        cur.execute(
            "select t_advit.compute_blended_daily(%s::uuid, %s)",
            (BROADMATE_WORKSPACE, UNREPORTED_DAY),
        )

    dashboard = client.get(f"/api/workspaces/{BROADMATE_WORKSPACE}/dashboard?days=30").json()
    body = report(client, days=30)

    projected = [{k: row[k] for k in DASHBOARD_MONEY_KEYS} for row in body["money"]]
    assert projected == dashboard["money"]

    # And the wider window really is wider: the row seeded 45 days back is in
    # the 60-day series and absent from the 30-day one.
    wide = report(client, days=60)
    assert any(r["date"] == UNREPORTED_DAY.isoformat() for r in wide["money"])
    assert not any(r["date"] == UNREPORTED_DAY.isoformat() for r in body["money"])


# ---------------------------------------------------------------------------
# Learnings
# ---------------------------------------------------------------------------


def _seed_learning(workspace: str, statement: str, *, confidence: float = 0.75) -> str:
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into t_advit.learnings
              (workspace_id, tier, statement, conditions_json, confidence, evidence_n, status)
            values (%s::uuid, 'account', %s,
                    '{"decision_type": "update_budget", "metric": "blended_cac_inr",
                      "direction": "down"}'::jsonb,
                    %s, 3, 'active')
            returning id::text
            """,
            (workspace, statement, confidence),
        )
        return cur.fetchone()[0]


@needs_db
def test_a_learning_inserted_for_rival_is_not_returned_for_broadmate(client):
    """The anonymisation boundary from PRD 5.1, at the report. Account-tier
    memory is one workspace's knowledge of itself; the query filters it and
    `learnings_select` refuses it, and this asserts the outcome of both."""
    rival = _seed_learning(RIVAL_WORKSPACE, f"On this account ({MARKER}, rival) budget moved CAC down.")
    own = _seed_learning(BROADMATE_WORKSPACE, f"On this account ({MARKER}, own) budget moved CAC down.")

    ids = {l["id"] for l in report(client)["learnings"]}
    assert own in ids
    assert rival not in ids


@needs_db
def test_a_learning_arrives_as_a_sentence_with_its_confidence_and_evidence(client):
    """What the Report tab renders: the statement as written by the promotion
    job, the smoothed confidence, how many outcomes it rests on, its status and
    when it last moved - never a paraphrase."""
    statement = f"On this account ({MARKER}) update budget moved Blended CAC down in 2 of 3 measured attempts."
    learning_id = _seed_learning(BROADMATE_WORKSPACE, statement, confidence=0.6)

    row = next(l for l in report(client)["learnings"] if l["id"] == learning_id)
    assert row["statement"] == statement
    assert float(row["confidence"]) == pytest.approx(0.6)
    assert row["evidence_n"] == 3
    assert row["status"] == "active"
    assert row["tier"] == "account"
    assert row["updated"] is not None


@needs_db
def test_the_shared_tier_gate_is_reported_so_the_page_can_say_why_rows_are_absent(client):
    body = report(client)
    assert isinstance(body["entitlements"]["industry_intelligence"], bool)
    if not body["entitlements"]["industry_intelligence"]:
        assert all(l["tier"] == "account" for l in body["learnings"])


# ---------------------------------------------------------------------------
# Outcomes and suggestions
# ---------------------------------------------------------------------------


@pytest.fixture
def decision_with_outcome_and_approval():
    """A decision that was measured AND has a proposal still waiting. Both hang
    off the decision row, which the autouse scrub deletes by its marker."""
    decision_id = str(uuid.uuid4())
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into t_advit.decisions
              (id, workspace_id, decision_type, chosen_option, reasoning,
               expected_effect_json, horizon_days)
            values (%s, %s::uuid, 'budget_change', 'option_a', %s,
                    '{"prediction": {"metric": "blended_cac_inr", "direction": "down"}}'::jsonb, 7)
            """,
            (decision_id, BROADMATE_WORKSPACE, MARKER),
        )
        cur.execute(
            """
            insert into t_advit.outcomes
              (decision_id, workspace_id, horizon_days, verdict, vs_expected, notes)
            values (%s, %s::uuid, 7, 'met',
                    '{"verdict": "met", "metric": "blended_cac_inr",
                      "metric_label": "Blended CAC", "predicted_direction": "down",
                      "before": {"value": 412.5, "days_with_data": 7},
                      "after": {"value": 380.0, "days_with_data": 7},
                      "delta": -32.5}'::jsonb,
                    'Blended CAC moved from 412.5 to 380, as predicted; no target was stated')
            """,
            (decision_id, BROADMATE_WORKSPACE),
        )
        cur.execute(
            """
            insert into t_advit.approvals
              (decision_id, workspace_id, risk_class, proposed_json, impact_inr, expires_at)
            values (%s, %s::uuid, 'high', '{"tool": "update_budget"}'::jsonb, 5000,
                    now() + interval '4 hours')
            returning id::text
            """,
            (decision_id, BROADMATE_WORKSPACE),
        )
        approval_id = cur.fetchone()[0]
    return {"decision_id": decision_id, "approval_id": approval_id}


@needs_db
def test_a_measured_outcome_is_returned_with_its_verdict_and_both_readings(
    client, decision_with_outcome_and_approval
):
    row = next(
        o for o in report(client)["outcomes"]
        if o["decision_id"] == decision_with_outcome_and_approval["decision_id"]
    )
    assert row["decision_type"] == "budget_change"
    assert row["verdict"] == "met"
    assert row["metric"] == "blended_cac_inr"
    assert row["predicted_direction"] == "down"
    assert row["before_value"] == pytest.approx(412.5)
    assert row["after_value"] == pytest.approx(380.0)
    assert row["horizon_days"] == 7


@needs_db
def test_suggestions_are_the_pending_approvals_the_inbox_shows(
    client, decision_with_outcome_and_approval
):
    """One query, two surfaces. The suggestions tab must list exactly what
    GET .../approvals?status=pending lists, or the owner approves from a page
    that disagrees with the inbox."""
    inbox = client.get(f"/api/workspaces/{BROADMATE_WORKSPACE}/approvals?status=pending").json()
    suggestions = report(client)["suggestions"]

    assert suggestions["pending_approvals"] == inbox
    assert any(
        a["id"] == decision_with_outcome_and_approval["approval_id"]
        for a in suggestions["pending_approvals"]
    )


@needs_db
def test_a_held_proposal_is_read_from_its_row_and_absence_is_a_checked_false(client):
    """A proposal the CTA gate holds is a t_advit.held_proposals row now, so
    the route can answer for it. With nothing open the answer is `held:
    false` with every key present - a check that was made and came back
    empty, which is what the old `held: null` could not claim. With a row
    open, the question, the proposal and the recommendation come back from
    it. (test_held_proposals.py covers who may see it and how it resolves.)"""
    from app.orchestrator import held
    from app.orchestrator.cta_gate import GateResult
    from app.db.pools import service_conn

    gate = report(client)["suggestions"]["cta_gate"]
    assert gate["held"] is False
    assert gate["proposal"] is None and gate["question"] is None
    assert set(gate) == {"held", "id", "run_id", "proposal", "question", "reason",
                         "recommendation", "held_at"}

    with service_conn() as conn, conn.cursor() as cur:
        held_id = held.hold(
            cur,
            workspace_id=BROADMATE_WORKSPACE,
            run_id=None,
            gated=GateResult(
                held=True,
                reason=MARKER,
                question=f"Where should it send people? {MARKER}",
                recommendation={"recommended": "click_to_call"},
                proposal={"goal": MARKER, "options": [], "recommended": "A"},
            ),
        )
        conn.commit()
    try:
        gate = report(client)["suggestions"]["cta_gate"]
        assert gate["held"] is True and gate["id"] == held_id
        assert gate["proposal"]["goal"] == MARKER
        assert gate["recommendation"] == {"recommended": "click_to_call"}
        assert MARKER in gate["question"] and gate["held_at"]
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("delete from t_advit.held_proposals where reason = %s", (MARKER,))


# ---------------------------------------------------------------------------
# The arithmetic the first review caught: a rate over unpaired days, a window
# clipped by the period, and a per-order figure summed as if it were a total.
# ---------------------------------------------------------------------------

# Rival's workspace, which the seed leaves with no spend and no truth at all -
# Broadmate's carries seven days of fixture spend, and a total asserted against
# it would be a test of the seed rather than of the arithmetic.
AD_ACCOUNT = "999000111222333"   # a connection this fixture creates for Rival
ARITH_WS = RIVAL_WORKSPACE
ARITH_DAYS = [date.today() - timedelta(days=n) for n in (3, 4, 10)]


@pytest.fixture(scope="module")
def rival() -> TestClient:
    c = TestClient(app)
    c.headers.update(auth(OUTSIDER))
    return c


@pytest.fixture
def paired_and_unpaired_days():
    """Two days of spend, one of them with delivered orders reported; a third
    day of spend in the PREVIOUS week only. Scrubbed by the same marker."""
    def _wipe(cur):
        for d in ARITH_DAYS:
            for table in ("metrics_daily", "blended_daily", "business_truth"):
                cur.execute(f"delete from t_advit.{table} where workspace_id = %s::uuid and date = %s",
                            (ARITH_WS, d))
        cur.execute("delete from t_advit.meta_connections where workspace_id = %s::uuid and ad_account_id = %s",
                    (ARITH_WS, AD_ACCOUNT))

    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        _wipe(cur)
        cur.execute(
            """
            insert into t_advit.meta_connections
              (workspace_id, business_id, ad_account_id, health, currency, write_enabled, health_detail)
            values (%s::uuid, 'test-business', %s, 'unknown', 'INR', false, '{}'::jsonb)
            """,
            (ARITH_WS, AD_ACCOUNT),
        )
        spend = {ARITH_DAYS[0]: 1000, ARITH_DAYS[1]: 3000, ARITH_DAYS[2]: 500}
        for d, inr in spend.items():
            cur.execute(
                """
                insert into t_advit.metrics_daily
                  (date, workspace_id, level, entity_id, ad_account_id, spend_inr, source)
                values (%s, %s::uuid, 'account', %s, %s, %s, 'fixture')
                """,
                (d, ARITH_WS, AD_ACCOUNT, AD_ACCOUNT, inr),
            )
        # Orders reported on day 0 only: 10 delivered at 2,000 revenue.
        cur.execute(
            """
            insert into t_advit.business_truth
              (date, workspace_id, delivered_orders, delivered_revenue_inr, business_issues)
            values (%s, %s::uuid, 10, 2000, %s)
            """,
            (ARITH_DAYS[0], ARITH_WS, MARKER),
        )
        for d in ARITH_DAYS[:2]:
            cur.execute("select t_advit.compute_blended_daily(%s::uuid, %s)", (ARITH_WS, d))
    yield
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        _wipe(cur)


def test_cac_and_mer_rest_on_days_where_both_sides_were_reported(rival, paired_and_unpaired_days):
    """Spend on two days, orders on one. A CAC of (1000+3000)/10 = 400 would be
    the answer of a report that lost an evening's truth and called it
    acquisition cost; the honest CAC is 1000/10 = 100 over the one paired day,
    and the coverage says it rests on one day."""
    body = report(rival, ARITH_WS, days=7)
    totals = body["totals"]
    assert totals["spend_inr"] == 4000.0, "the raw period spend keeps its own, wider, coverage"
    assert totals["blended_cac_inr"] == 100.0
    assert totals["mer"] == 2.0
    assert totals["coverage"]["cac_days"] == 1
    assert totals["coverage"]["mer_days"] == 1
    assert totals["coverage"]["spend_days"] == 2


def test_the_previous_week_is_summed_on_its_own_dates_even_for_a_seven_day_report(
    rival, paired_and_unpaired_days
):
    """At days=7 the previous week lies entirely outside the period. The first
    draft filtered by period first and compared this week against a single
    day, then printed a direction."""
    body = report(rival, ARITH_WS, days=7)
    wow = body["week_over_week"]
    assert wow["windows"]["previous_days_with_data"] == 1
    assert wow["spend_inr"]["previous"] == 500.0
    assert wow["spend_inr"]["this"] == 4000.0


def test_the_period_margin_is_orders_times_per_order_margin_not_a_sum_of_per_order_values(
    rival, paired_and_unpaired_days
):
    """blended_daily.contribution_margin_inr is PER DELIVERED ORDER. The
    period figure is that times the day's delivered orders; the plain sum was
    a rupee figure that was neither."""
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "select contribution_margin_inr from t_advit.blended_daily "
            "where workspace_id = %s::uuid and date = %s",
            (ARITH_WS, ARITH_DAYS[0]),
        )
        per_order = cur.fetchone()[0]
    body = report(rival, ARITH_WS, days=7)
    if per_order is None:
        assert body["totals"]["contribution_margin_inr"] is None
        assert body["totals"]["coverage"]["margin_days"] == 0
    else:
        assert body["totals"]["contribution_margin_inr"] == pytest.approx(float(per_order) * 10)
        assert body["totals"]["coverage"]["margin_days"] == 1
