"""A question the owner has not answered is a fact about the account.

The CTA gate holds a proposal that would build a campaign while no destination
is on file. That hold used to live in one run's state and one chat response,
and the reports route could only say ``held: null, "not persisted"``. It is a
row now - t_advit.held_proposals, written by app/orchestrator/held.py - and
these tests are about the row: that the strategy node writes exactly one, that
the newest supersedes, that the owner's answer closes it and says so, and that
the tenant surface can read it but never write it.

The direct tests drive ``held.hold`` / ``held.resolve`` on the backend
connection with a GateResult built by hand; one graph-level test proves the
strategy node calls them. Every row these tests create carries the marker in
its reason or belongs to a run whose thread_id starts with ``held-``, and is
scrubbed by that before and after.
"""

from __future__ import annotations

import json
import uuid

import psycopg
import psycopg.rows
import pytest
from fastapi.testclient import TestClient

from app.db.pools import service_conn, tenant_tx
from app.main import app
from app.orchestrator import held
from app.orchestrator.cta_gate import GateResult
from conftest import (
    BROADMATE_WORKSPACE,
    MEMBER,
    OUTSIDER,
    OWNER,
    RIVAL_WORKSPACE,
    auth,
    claims_for,
)

SUPERUSER_DSN = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"

MARKER = "test_held_proposals marker"


def _scrub() -> None:
    with psycopg.connect(SUPERUSER_DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("delete from t_advit.held_proposals where reason = %s", (MARKER,))
        cur.execute(
            "delete from t_advit.held_proposals where run_id in "
            "(select id from t_advit.runs where thread_id like 'held-%')"
        )
        cur.execute(
            "delete from t_advit.decisions where run_id in "
            "(select id from t_advit.runs where thread_id like 'held-%')"
        )
        cur.execute("delete from t_advit.runs where thread_id like 'held-%'")


@pytest.fixture(autouse=True)
def _clean_before_and_after():
    _scrub()
    yield
    _scrub()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def gated(goal: str = "more qualified leads") -> GateResult:
    return GateResult(
        held=True,
        reason=MARKER,
        question=f"Before this campaign is built: where should it send people? ({goal})",
        recommendation={"recommended": "click_to_call", "rationale": ["cheap to test"]},
        proposal={
            "goal": goal,
            "assumptions": [],
            "options": [
                {"label": "A", "what": "build it",
                 "action": {"tool": "create_campaign_draft", "params": {}}},
            ],
            "recommended": "A",
            "single_strongest_reason": "because",
            "questions": [],
        },
    )


def rows(workspace: str = BROADMATE_WORKSPACE) -> list[dict]:
    with psycopg.connect(SUPERUSER_DSN, row_factory=psycopg.rows.dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """select id::text, run_id::text as run_id, resolved_at, resolved_by,
                      proposal_json ->> 'goal' as goal
                 from t_advit.held_proposals
                where workspace_id = %s::uuid and reason = %s
                order by held_at""",
            (workspace, MARKER),
        )
        return cur.fetchall()


def hold(workspace: str = BROADMATE_WORKSPACE, goal: str = "more qualified leads") -> str:
    with service_conn() as conn, conn.cursor() as cur:
        held_id = held.hold(cur, workspace_id=workspace, run_id=None, gated=gated(goal))
        conn.commit()
    return held_id


# ---------------------------------------------------------------------------
# The row
# ---------------------------------------------------------------------------


def test_a_hold_writes_exactly_one_open_row():
    held_id = hold()
    open_rows = [r for r in rows() if r["resolved_at"] is None]
    assert [r["id"] for r in open_rows] == [held_id]


def test_a_second_hold_supersedes_the_first_so_the_newest_question_is_the_live_one():
    first = hold(goal="first ask")
    second = hold(goal="second ask")

    by_id = {r["id"]: r for r in rows()}
    assert by_id[first]["resolved_by"] == "superseded"
    assert by_id[first]["resolved_at"] is not None
    assert by_id[second]["resolved_at"] is None

    with tenant_tx(claims_for(OWNER)) as cur:
        live = held.open_for(cur, BROADMATE_WORKSPACE)
    assert live["id"] == second and live["proposal"]["goal"] == "second ask"


def test_the_database_refuses_a_second_open_row_even_if_the_writer_forgot_to_supersede():
    """The partial unique index is the guarantee; held.hold resolving first is
    what makes it a design rather than a race."""
    hold()
    with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                """insert into t_advit.held_proposals
                     (workspace_id, proposal_json, question, reason)
                   values (%s::uuid, '{}'::jsonb, 'again?', %s)""",
                (BROADMATE_WORKSPACE, MARKER),
            )


def test_a_result_the_gate_did_not_hold_is_refused():
    passed = GateResult(held=False, reason="the recommended option builds nothing new",
                        proposal={"goal": "x"})
    with service_conn() as conn, conn.cursor() as cur:
        with pytest.raises(ValueError):
            held.hold(cur, workspace_id=BROADMATE_WORKSPACE, run_id=None, gated=passed)
    assert rows() == []


def test_a_hold_with_no_question_is_refused():
    blank = GateResult(held=True, reason=MARKER, question=None, proposal={"goal": "x"})
    with service_conn() as conn, conn.cursor() as cur:
        with pytest.raises(ValueError):
            held.hold(cur, workspace_id=BROADMATE_WORKSPACE, run_id=None, gated=blank)


def test_a_resolution_the_vocabulary_does_not_name_is_refused():
    hold()
    with service_conn() as conn, conn.cursor() as cur:
        with pytest.raises(ValueError):
            held.resolve(cur, workspace_id=BROADMATE_WORKSPACE, by="answered")
    assert rows()[0]["resolved_at"] is None


def test_resolving_with_nothing_open_is_none_not_an_error():
    """An owner may set a destination before ever asking for a campaign."""
    with service_conn() as conn, conn.cursor() as cur:
        assert held.resolve(cur, workspace_id=BROADMATE_WORKSPACE, by=held.CTA_SET) is None


# ---------------------------------------------------------------------------
# The tenant connection may read the row and nothing else
# ---------------------------------------------------------------------------


def test_a_tenant_connection_cannot_close_the_systems_record_of_its_own_question():
    """`authenticated` holds SELECT and nothing else, so the same function
    that the backend uses to resolve is refused on a member's own connection.
    That grant, not a review comment, is what keeps "a tenant cannot edit the
    system's record" true."""
    hold()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with tenant_tx(claims_for(OWNER)) as cur:
            held.resolve(cur, workspace_id=BROADMATE_WORKSPACE, by=held.CTA_SET)
    assert rows()[0]["resolved_at"] is None


def test_a_tenant_connection_cannot_hold_a_proposal_either():
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with tenant_tx(claims_for(OWNER)) as cur:
            held.hold(cur, workspace_id=BROADMATE_WORKSPACE, run_id=None, gated=gated())
    assert rows() == []


def test_a_member_of_another_workspace_reads_nothing_through_the_policy():
    hold()
    with tenant_tx(claims_for(OUTSIDER)) as cur:
        assert held.open_for(cur, BROADMATE_WORKSPACE) is None


# ---------------------------------------------------------------------------
# The owner's answer releases it
# ---------------------------------------------------------------------------


@pytest.fixture
def no_cta_on_file():
    """The seed carries an owner_asserted primary_cta = 'call' for the
    Broadmate workspace. PUT .../cta supersedes it; restore the seed's row and
    remove what the test wrote, so the next suite sees the seed."""
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
        cur.execute(
            """delete from t_advit.account_context
                where workspace_id = %s::uuid and dimension = 'sales_operation'
                  and key = 'primary_cta' and source = 'owner_asserted'
                  and id <> all(%s::uuid[])""",
            (BROADMATE_WORKSPACE, expired or ["00000000-0000-0000-0000-000000000000"]),
        )
        if expired:
            cur.execute("update t_advit.account_context set valid_to = null where id = any(%s::uuid[])",
                        (expired,))
        conn.commit()


def test_the_owners_answer_resolves_the_held_proposal_and_the_response_says_so(client, no_cta_on_file):
    held_id = hold(goal="leads for the new pack")

    r = client.put(
        f"/api/workspaces/{BROADMATE_WORKSPACE}/cta",
        json={"destination": "whatsapp"},
        headers=auth(OWNER),
    )
    assert r.status_code == 200, r.text
    released = r.json()["released_proposal"]
    assert released["id"] == held_id
    assert released["resolved_by"] == "cta_set"
    assert released["goal"] == "leads for the new pack"
    assert "where should it send people" in released["question"]

    row = rows()[0]
    assert row["resolved_by"] == "cta_set" and row["resolved_at"] is not None


def test_an_answer_with_nothing_waiting_says_so_rather_than_failing(client, no_cta_on_file):
    r = client.put(
        f"/api/workspaces/{BROADMATE_WORKSPACE}/cta",
        json={"destination": "call"},
        headers=auth(OWNER),
    )
    assert r.status_code == 200, r.text
    assert r.json()["released_proposal"] is None


def test_a_stranger_answering_for_another_workspace_releases_nothing(client):
    hold()
    r = client.put(
        f"/api/workspaces/{BROADMATE_WORKSPACE}/cta",
        json={"destination": "whatsapp"},
        headers=auth(OUTSIDER),
    )
    assert r.status_code == 404
    assert rows()[0]["resolved_at"] is None


# ---------------------------------------------------------------------------
# The reports route
# ---------------------------------------------------------------------------


def gate_for(client: TestClient, user: str, workspace: str = BROADMATE_WORKSPACE) -> dict:
    r = client.get(f"/api/workspaces/{workspace}/reports", headers=auth(user))
    assert r.status_code == 200, r.text
    return r.json()["suggestions"]["cta_gate"]


def test_the_reports_route_returns_the_open_row_to_a_member(client):
    held_id = hold(goal="leads for the new pack")
    for user in (OWNER, MEMBER):
        gate = gate_for(client, user)
        assert gate["held"] is True
        assert gate["id"] == held_id
        assert gate["run_id"] is None
        assert gate["proposal"]["goal"] == "leads for the new pack"
        assert gate["proposal"]["options"][0]["action"]["tool"] == "create_campaign_draft"
        assert gate["recommendation"]["recommended"] == "click_to_call"
        assert "where should it send people" in gate["question"]
        assert gate["reason"] == MARKER
        assert gate["held_at"]


def test_an_outsider_gets_404_for_the_workspace_and_nothing_for_their_own(client):
    hold()
    r = client.get(f"/api/workspaces/{BROADMATE_WORKSPACE}/reports", headers=auth(OUTSIDER))
    assert r.status_code == 404

    gate = gate_for(client, OUTSIDER, RIVAL_WORKSPACE)
    assert gate["held"] is False
    assert gate["id"] is None and gate["proposal"] is None


def test_a_resolved_row_is_not_returned_as_open(client):
    hold()
    with service_conn() as conn, conn.cursor() as cur:
        held.resolve(cur, workspace_id=BROADMATE_WORKSPACE, by=held.CTA_SET)
        conn.commit()

    gate = gate_for(client, OWNER)
    assert gate["held"] is False
    assert gate["proposal"] is None and gate["question"] is None
    assert gate["reason"] == "no proposal is held for this workspace"


# ---------------------------------------------------------------------------
# Through the graph: the strategy node writes the row
# ---------------------------------------------------------------------------


class CampaignProposer:
    """A strategy model that always proposes building a campaign."""

    def complete(self, role, *, system, user, **kw):
        from app.models.router import Completion

        return Completion(text="ok", model="stub", role=role, model_class="judgement",
                          tokens_in=1, tokens_out=1, cost_usd=0, cost_inr=0,
                          latency_ms=1, finish_reason="stop")

    def complete_json(self, role, *, system, user, schema, **kw):
        from app.models.router import Completion

        body = {
            "goal": "more qualified leads",
            "assumptions": [],
            "options": [
                {"label": "A", "what": "build a leads campaign", "expected_effect": "leads up",
                 "risk": "low", "cost_of_being_wrong": "a day of spend",
                 "action": {"tool": "create_campaign_draft", "ad_account_id": "1000000000000003",
                            "params": {"objective": "OUTCOME_LEADS"}}},
            ],
            "recommended": "A",
            "single_strongest_reason": "because",
            "questions": [],
        }
        if role == "intent":
            body = {"intent": "propose", "confidence": 0.9, "reason": "build"}
        return body, Completion(text=json.dumps(body), model="stub", role=role,
                                model_class="judgement", tokens_in=1, tokens_out=1,
                                cost_usd=0, cost_inr=0, latency_ms=1, finish_reason="stop")


def _run(user: str, workspace: str, message: str) -> dict:
    from app.agents.compliance import ComplianceGate
    from app.orchestrator.graph import Orchestrator, build_graph
    from conftest import acting_as

    orch = Orchestrator(router=CampaignProposer(), gate_factory=lambda bt: ComplianceGate([]))
    graph = build_graph(orch)
    with acting_as(user):
        return graph.invoke({
            "run_id": str(uuid.uuid4()),
            "workspace_id": workspace,
            "thread_id": f"held-{uuid.uuid4()}",
            "trigger": "user_message",
            "message": message,
        })


def test_the_strategy_node_writes_the_row_for_the_run_that_asked(client, no_cta_on_file):
    state = _run(OWNER, BROADMATE_WORKSPACE, "Naya campaign banao leads ke liye")
    assert state.get("mode") == "asked"
    assert state.get("proposal_held") is True

    held_id = state.get("held_proposal_id")
    assert held_id, "the strategy node held the proposal but wrote no row"
    assert state["cta_gate"]["held_proposal_id"] == held_id

    with psycopg.connect(SUPERUSER_DSN, row_factory=psycopg.rows.dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """select run_id::text as run_id, resolved_at, question,
                      proposal_json ->> 'goal' as goal, recommendation_json
                 from t_advit.held_proposals where id = %s::uuid""",
            (held_id,),
        )
        row = cur.fetchone()
    assert row["run_id"] == state["run_id"]
    assert row["resolved_at"] is None
    assert row["goal"] == "more qualified leads"
    assert row["question"] == state["questions"][0]
    assert row["recommendation_json"]["recommended"]

    # And the Suggestions tab shows the same question the chat asked.
    gate = gate_for(client, OWNER)
    assert gate["id"] == held_id and gate["run_id"] == state["run_id"]


@pytest.fixture
def broadmate_suspended():
    """The organisation the run belongs to is suspended for the duration, so
    core.access_mode answers `denied` and decide() halts the run."""
    with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
        cur.execute("select org_id::text from t_advit.workspaces where id = %s::uuid", (BROADMATE_WORKSPACE,))
        org = cur.fetchone()[0]
        cur.execute("update core.organisations set status = 'suspended', suspended_at = now(), "
                    "suspension_reason = %s where id = %s::uuid", (MARKER, org))
        conn.commit()
    yield
    with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
        cur.execute("update core.organisations set status = 'active', suspended_at = null, "
                    "suspension_reason = null where id = %s::uuid", (org,))
        conn.commit()


def test_a_run_that_halts_leaves_no_open_question(client, no_cta_on_file, broadmate_suspended):
    """The gate still holds the proposal - the question is in the turn - but
    decide() halts a run whose access mode is not `full`, and a halted run's
    question is not one the owner can act on. No row: the inbox of an account
    that cannot run must not show a question whose answer releases nothing."""
    state = _run(OWNER, BROADMATE_WORKSPACE, "Naya campaign banao leads ke liye")
    assert state.get("mode") == "halted", state.get("mode")
    assert state.get("access_mode") != "full"
    assert state.get("proposal_held") is True
    assert state.get("held_proposal_id") is None
    assert state["cta_gate"]["held_proposal_id"] is None
    with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
        cur.execute("select count(*) from t_advit.held_proposals where run_id = %s::uuid", (state["run_id"],))
        assert cur.fetchone()[0] == 0


def test_a_proposal_the_gate_let_through_writes_no_row():
    """The seed's owner_asserted 'call' is on file, so the same proposal
    passes with the CTA written in - and nothing is held."""
    state = _run(OWNER, BROADMATE_WORKSPACE, "Naya campaign banao leads ke liye")
    assert state.get("proposal_held") is False
    assert state.get("held_proposal_id") is None
    with psycopg.connect(SUPERUSER_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "select count(*) from t_advit.held_proposals where run_id = %s::uuid",
            (state["run_id"],),
        )
        assert cur.fetchone()[0] == 0
