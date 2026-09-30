"""No campaign gets built until the owner has said where it sends people.

The destination decides the whole funnel, and the strategy prompt asked the
model to raise questions it could not answer from the facts. Nothing checked
that it had. This gate does, in code, after the model: a proposal that would
build a campaign or an ad set with no owner-asserted CTA on file is HELD - no
decision row, nothing at the approval gate, the turn ends ASKED with one argued
question. A proposal with a CTA on file gets it written into the action, so the
approval the owner signs names the destination.

Only ``owner_asserted`` counts. The seed carries an ``inferred`` row - "the ad
account is called 'call ads', so probably calls" - and an inference is exactly
the thing this gate exists to replace with a decision.
"""

from __future__ import annotations

import json

import psycopg
import pytest

from app.orchestrator import cta_gate
from conftest import BROADMATE_WORKSPACE, OUTSIDER, OWNER, auth

SUPERUSER_DSN = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"


def proposal(tool: str, params: dict | None = None) -> dict:
    return {
        "goal": "more qualified leads",
        "assumptions": [],
        "options": [
            {
                "label": "A",
                "what": "do the thing",
                "expected_effect": "leads go up",
                "risk": "low",
                "cost_of_being_wrong": "a day of spend",
                "action": {"tool": tool, "ad_account_id": "1000000000000003",
                           "params": params or {}},
            },
            {"label": "B", "what": "do nothing", "expected_effect": "nothing",
             "risk": "none", "cost_of_being_wrong": "nothing"},
        ],
        "recommended": "A",
        "single_strongest_reason": "because",
        "questions": [],
    }


def asserted(value: str) -> dict:
    return {"dimension": "sales_operation", "key": "primary_cta",
            "value_json": value, "source": "owner_asserted"}


def inferred(value: str) -> dict:
    return {"dimension": "sales_operation", "key": "primary_cta",
            "value_json": value, "source": "inferred"}


FACTS = {
    "products": [{"sku": "PILES-01", "price_inr": 899.0, "margin_rate": 0.62}],
    "economics": {"rto_rate": 0.22},
}


# ---------------------------------------------------------------------------
# The gate as a function
# ---------------------------------------------------------------------------


def test_a_campaign_build_with_no_cta_on_file_is_held():
    result = cta_gate.gate(proposal("create_campaign_draft"), account_context=[], facts=FACTS)
    assert result.held is True
    assert result.question
    assert "where should it send people" in result.question
    assert result.recommendation is not None


def test_the_question_argues_for_a_recommendation_rather_than_asking_a_blank():
    """The CTA model already knows how to weigh economics, capacity and
    category. The owner should be choosing between argued options."""
    result = cta_gate.gate(proposal("create_campaign_draft"), account_context=[], facts=FACTS)
    assert "I would recommend" in result.question
    assert result.recommendation["recommended"]
    assert result.recommendation["rationale"]


def test_an_inferred_cta_does_not_count():
    """The seed's own row: "the ad account is named 'call ads', so probably
    calls." An inference is what this gate exists to replace."""
    result = cta_gate.gate(
        proposal("create_campaign_draft"), account_context=[inferred("call")], facts=FACTS
    )
    assert result.held is True


def test_an_owner_asserted_cta_is_written_into_the_action():
    """So the pipeline builds THAT, and the approval binds to it."""
    result = cta_gate.gate(
        proposal("create_campaign_draft", {"objective": "OUTCOME_LEADS"}),
        account_context=[asserted("whatsapp")],
        facts=FACTS,
    )
    assert result.held is False
    assert result.cta == "click_to_whatsapp"
    action = result.proposal["options"][0]["action"]
    assert action["params"]["cta"] == "click_to_whatsapp"
    assert action["params"]["objective"] == "OUTCOME_LEADS", "other params were dropped"


def test_the_original_proposal_is_not_mutated():
    """Provenance keeps what the model said; the gate returns a copy."""
    original = proposal("create_campaign_draft")
    cta_gate.gate(original, account_context=[asserted("call")], facts=FACTS)
    assert "cta" not in original["options"][0]["action"]["params"]


@pytest.mark.parametrize("tool", ["update_budget", "pause_entity", "activate_entity"])
def test_a_change_to_something_that_exists_passes_through(tool):
    """A budget change chooses no destination; the campaign it acts on already
    has one."""
    result = cta_gate.gate(proposal(tool), account_context=[], facts=FACTS)
    assert result.held is False
    assert result.cta is None


def test_an_ad_set_build_needs_a_cta_too():
    result = cta_gate.gate(proposal("create_ad_set_draft"), account_context=[], facts=FACTS)
    assert result.held is True


def test_a_proposal_with_no_recommended_option_passes_through():
    p = proposal("create_campaign_draft")
    p["recommended"] = "Z"
    result = cta_gate.gate(p, account_context=[], facts=FACTS)
    assert result.held is False


def test_every_spelling_an_owner_might_use_resolves():
    for spelling, expected in (
        ("whatsapp", "click_to_whatsapp"), ("WhatsApp", "click_to_whatsapp"),
        ("call", "click_to_call"), ("lead_form", "meta_instant_form"),
        ("website", "landing_page"), ("landing_page", "landing_page"),
    ):
        assert cta_gate.on_file([asserted(spelling)]) == expected, spelling


def test_an_unknown_value_on_file_is_treated_as_absent():
    """A typo in account_context must hold the proposal, not build a campaign
    with a destination nobody chose."""
    assert cta_gate.on_file([asserted("carrier pigeon")]) is None


# ---------------------------------------------------------------------------
# Through the graph
# ---------------------------------------------------------------------------


class CampaignProposer:
    """A strategy model that always proposes building a campaign."""

    def __init__(self):
        self.calls = []

    def complete(self, role, *, system, user, **kw):
        from app.models.router import Completion

        self.calls.append(role)
        return Completion(text="ok", model="stub", role=role, model_class="judgement",
                          tokens_in=1, tokens_out=1, cost_usd=0, cost_inr=0,
                          latency_ms=1, finish_reason="stop")

    def complete_json(self, role, *, system, user, schema, **kw):
        from app.models.router import Completion

        self.calls.append(role)
        body = proposal("create_campaign_draft", {"objective": "OUTCOME_LEADS"})
        if role == "intent":
            body = {"intent": "propose", "confidence": 0.9, "reason": "build"}
        return body, Completion(text=json.dumps(body), model="stub", role=role,
                                model_class="judgement", tokens_in=1, tokens_out=1,
                                cost_usd=0, cost_inr=0, latency_ms=1, finish_reason="stop")


def _run(user: str, workspace: str, message: str) -> dict:
    import uuid

    from app.agents.compliance import ComplianceGate
    from app.orchestrator.graph import Orchestrator, build_graph
    from conftest import acting_as

    orch = Orchestrator(router=CampaignProposer(), gate_factory=lambda bt: ComplianceGate([]))
    graph = build_graph(orch)
    with acting_as(user):
        return graph.invoke({
            "run_id": str(uuid.uuid4()),
            "workspace_id": workspace,
            "thread_id": f"cta-{uuid.uuid4()}",
            "trigger": "user_message",
            "message": message,
        })


@pytest.fixture
def no_cta_on_file():
    """The seed carries an owner_asserted primary_cta = 'call'. Expire it for
    the test so the gate has nothing on file, and restore afterwards."""
    with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """update t_advit.account_context set valid_to = now()
                where workspace_id = %s::uuid and dimension = 'sales_operation'
                  and key = 'primary_cta' and source = 'owner_asserted' and valid_to is null
              returning id::text""",
            (BROADMATE_WORKSPACE,),
        )
        expired = [r[0] for r in cur.fetchall()]
        conn.commit()
    yield
    with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
        if expired:
            cur.execute("update t_advit.account_context set valid_to = null where id = any(%s::uuid[])",
                        (expired,))
        cur.execute(
            """delete from t_advit.account_context
                where workspace_id = %s::uuid and dimension = 'sales_operation'
                  and key = 'primary_cta' and source = 'owner_asserted'
                  and id <> all(%s::uuid[])""",
            (BROADMATE_WORKSPACE, expired or ["00000000-0000-0000-0000-000000000000"]),
        )
        cur.execute("delete from t_advit.held_proposals where run_id in "
                    "(select id from t_advit.runs where thread_id like 'cta-%')")
        cur.execute("delete from t_advit.decisions where workspace_id = %s::uuid and run_id in "
                    "(select id from t_advit.runs where thread_id like 'cta-%%')", (BROADMATE_WORKSPACE,))
        cur.execute("delete from t_advit.runs where thread_id like 'cta-%'")
        conn.commit()


def test_a_held_proposal_records_no_decision_and_ends_asked(no_cta_on_file):
    state = _run(OWNER, BROADMATE_WORKSPACE, "Naya campaign banao leads ke liye")
    assert state.get("mode") == "asked", state.get("mode")
    assert state.get("proposal_held") is True
    assert not state.get("decision_id"), "a decision was recorded for a held proposal"
    assert any("where should it send people" in q for q in state.get("questions", []))


def test_the_seeded_owner_choice_lets_the_proposal_through():
    """The seed's owner_asserted 'call' is on file, so the same proposal passes
    with the CTA written into its action."""
    state = _run(OWNER, BROADMATE_WORKSPACE, "Naya campaign banao leads ke liye")
    assert state.get("proposal_held") is False
    assert state.get("cta_gate", {}).get("cta") == "click_to_call"
    assert state["proposal"]["options"][0]["action"]["params"]["cta"] == "click_to_call"
    # clean the decision this run recorded
    with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
        cur.execute("delete from t_advit.held_proposals where run_id in "
                    "(select id from t_advit.runs where thread_id like 'cta-%')")
        cur.execute("delete from t_advit.decisions where run_id in "
                    "(select id from t_advit.runs where thread_id like 'cta-%')")
        cur.execute("delete from t_advit.runs where thread_id like 'cta-%'")
        conn.commit()


# ---------------------------------------------------------------------------
# The route that records the answer
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def test_the_owner_records_a_choice_and_the_gate_sees_it(client, no_cta_on_file):
    r = client.put(
        f"/api/workspaces/{BROADMATE_WORKSPACE}/cta",
        json={"destination": "whatsapp", "reason": "our sales team lives on WhatsApp"},
        headers=auth(OWNER),
    )
    assert r.status_code == 200, r.text
    assert r.json()["destination"] == "click_to_whatsapp"

    state = _run(OWNER, BROADMATE_WORKSPACE, "Naya campaign banao")
    assert state.get("proposal_held") is False
    assert state["cta_gate"]["cta"] == "click_to_whatsapp"


def test_a_new_choice_supersedes_the_old_one_rather_than_overwriting(client, no_cta_on_file):
    client.put(f"/api/workspaces/{BROADMATE_WORKSPACE}/cta", json={"destination": "call"}, headers=auth(OWNER))
    client.put(f"/api/workspaces/{BROADMATE_WORKSPACE}/cta", json={"destination": "whatsapp"}, headers=auth(OWNER))

    with psycopg.connect(SUPERUSER_DSN, row_factory=psycopg.rows.dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """select value_json, valid_to is null as live from t_advit.account_context
                where workspace_id = %s::uuid and dimension = 'sales_operation'
                  and key = 'primary_cta' and source = 'owner_asserted'
                  and asserted_by is not null
                order by valid_from""",
            (BROADMATE_WORKSPACE,),
        )
        rows = cur.fetchall()
    live = [r for r in rows if r["live"]]
    assert len(live) == 1 and live[0]["value_json"] == "click_to_whatsapp"
    assert any(not r["live"] and r["value_json"] == "click_to_call" for r in rows), (
        "the previous decision was overwritten rather than superseded"
    )


def test_a_stranger_cannot_choose_for_another_workspace(client):
    r = client.put(
        f"/api/workspaces/{BROADMATE_WORKSPACE}/cta", json={"destination": "whatsapp"}, headers=auth(OUTSIDER)
    )
    assert r.status_code == 404


def test_a_nonsense_destination_is_refused(client):
    r = client.put(
        f"/api/workspaces/{BROADMATE_WORKSPACE}/cta", json={"destination": "telegram"}, headers=auth(OWNER)
    )
    assert r.status_code == 422
