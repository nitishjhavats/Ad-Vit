"""The first production rows the pipeline has ever written.

`policy/pipeline.py` is a thousand lines implementing a fifteen-step tool
invocation with an autonomy matrix, tenant checks, absolute caps, an approval
gate bound to an authorisation fingerprint, verification and rollback. Until
this commit **nothing called it**: `invoke` had one caller in the application,
the rollback route, which needs a `t_advit.actions` row that only `invoke`
creates. The path was circular, `Mode.EXECUTED` was assigned by no code
anywhere, and every actions row in the database had been written by a pytest
fixture.

These tests are about the join, in two halves:

  * `action_from_option` — a pure function, and the only place a model's words
    become a typed request. It has to refuse rather than improvise.
  * the graph and the approval route — that a proposal records its decision
    BEFORE anything executes, that a critical action suspends rather than
    running, and that answering the approval is what carries it out.
"""

from __future__ import annotations

import json
import os
import uuid

import psycopg
import pytest

from app.orchestrator.execution import (
    PROPOSABLE_TOOLS,
    UnusableAction,
    action_from_option,
    recommended_option,
)
from app.policy.risk import Tool

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")
ACCOUNT = "1000000000000003"
OTHER_ACCOUNT = "1000000000000009"
WRITABLE = frozenset({ACCOUNT})


def option(**action) -> dict:
    return {
        "label": "A",
        "what": "raise the budget",
        "expected_effect": "more leads",
        "risk": "learning reset",
        "cost_of_being_wrong": "a day of delivery",
        "action": action,
    }


# ---------------------------------------------------------------------------
# The typed action
# ---------------------------------------------------------------------------


def test_a_well_formed_option_becomes_a_typed_request():
    action = action_from_option(
        option(tool="update_budget", target_entity_id="as1",
               params={"daily_budget_inr": 12000}),
        default_ad_account_id=ACCOUNT,
        writable_ad_accounts=WRITABLE,
    )
    assert action.tool is Tool.UPDATE_BUDGET
    assert action.ad_account_id == ACCOUNT
    assert action.target_entity_id == "as1"
    assert action.params == {"daily_budget_inr": 12000.0}
    assert isinstance(action.params["daily_budget_inr"], float)


def test_an_option_with_no_action_block_is_discussable_but_not_executable():
    """A proposal the system cannot carry out is still worth making. What it
    must not do is look executable."""
    with pytest.raises(UnusableAction, match="no machine-readable action"):
        action_from_option(
            {"label": "A", "what": "hire a second media buyer"},
            default_ad_account_id=ACCOUNT,
            writable_ad_accounts=WRITABLE,
        )


def test_an_invented_tool_is_refused():
    """The specific thing a language model does under pressure: produce
    something plausible. `delete_campaign` reads like a tool and is not one."""
    with pytest.raises(UnusableAction, match="not a proposable tool"):
        action_from_option(
            option(tool="delete_campaign", target_entity_id="c1"),
            default_ad_account_id=ACCOUNT,
            writable_ad_accounts=WRITABLE,
        )


@pytest.mark.parametrize("tool", ["read_insights", "read_account", "read_entities"])
def test_a_read_cannot_be_proposed(tool):
    """Reads have no approval gate and no verification step, so routing one
    through the proposal path would be a way to get work done with LESS scrutiny
    rather than more. The analytics node does reads, under its own budget."""
    with pytest.raises(UnusableAction, match="not a proposable tool"):
        action_from_option(
            option(tool=tool),
            default_ad_account_id=ACCOUNT,
            writable_ad_accounts=WRITABLE,
        )


def test_an_unknown_parameter_is_refused_rather_than_dropped():
    """Silently dropping it would produce an action that does not match the
    proposal the owner read and approved."""
    with pytest.raises(UnusableAction, match="does not accept"):
        action_from_option(
            option(tool="update_budget", target_entity_id="as1",
                   params={"daily_budget_inr": 12000, "bid_strategy": "LOWEST_COST"}),
            default_ad_account_id=ACCOUNT,
            writable_ad_accounts=WRITABLE,
        )


def test_an_ad_account_outside_the_workspace_is_refused():
    """The pipeline's tenant check would refuse it too. Refusing here means the
    system never attempts the cross-tenant call, and the message names the
    proposal rather than surfacing as a denial three layers down."""
    with pytest.raises(UnusableAction, match="not a writable connection"):
        action_from_option(
            option(tool="pause_entity", ad_account_id=OTHER_ACCOUNT, target_entity_id="as1"),
            default_ad_account_id=ACCOUNT,
            writable_ad_accounts=WRITABLE,
        )


def test_a_tool_that_needs_a_target_is_refused_without_one():
    with pytest.raises(UnusableAction, match="names no entity"):
        action_from_option(
            option(tool="update_budget", params={"daily_budget_inr": 1000}),
            default_ad_account_id=ACCOUNT,
            writable_ad_accounts=WRITABLE,
        )


def test_a_creating_tool_is_refused_with_a_target():
    """`create_campaign_draft` targeting an existing entity is incoherent, and
    an incoherent request that reaches the driver is one whose behaviour nobody
    predicted."""
    with pytest.raises(UnusableAction, match="cannot also target"):
        action_from_option(
            option(tool="create_campaign_draft", target_entity_id="c1",
                   params={"name": "x", "objective": "OUTCOME_LEADS"}),
            default_ad_account_id=ACCOUNT,
            writable_ad_accounts=WRITABLE,
        )


def test_a_non_numeric_budget_is_refused():
    with pytest.raises(UnusableAction, match="not a number"):
        action_from_option(
            option(tool="update_budget", target_entity_id="as1",
                   params={"daily_budget_inr": "twenty thousand"}),
            default_ad_account_id=ACCOUNT,
            writable_ad_accounts=WRITABLE,
        )


@pytest.mark.parametrize("given,expected", [(0, 1), (-5, 1), (400, 90), (14, 14), (None, 7)])
def test_the_horizon_is_clamped_to_something_measurable(given, expected):
    """The outcome check is scheduled at this horizon and the learning loop
    reads it. Zero and a year are both ways of never being measured."""
    opt = option(tool="pause_entity", target_entity_id="as1")
    if given is not None:
        opt["horizon_days"] = given
    action = action_from_option(
        opt, default_ad_account_id=ACCOUNT, writable_ad_accounts=WRITABLE
    )
    assert action.horizon_days == expected


def test_the_default_ad_account_is_used_only_when_there_is_exactly_one():
    """With two writable connections, "which account?" has a real answer that
    the proposal has to give. Picking one would be picking whose money to
    spend."""
    with pytest.raises(UnusableAction, match="names no ad account"):
        action_from_option(
            option(tool="pause_entity", target_entity_id="as1"),
            default_ad_account_id=None,
            writable_ad_accounts=frozenset({ACCOUNT, OTHER_ACCOUNT}),
        )


def test_a_recommendation_naming_no_listed_option_resolves_to_nothing():
    """Falling back to the first option is how an owner approves one action and
    gets another."""
    assert recommended_option(
        {"options": [{"label": "A"}, {"label": "B"}], "recommended": "C"}
    ) is None
    assert recommended_option(
        {"options": [{"label": "A"}, {"label": "B"}], "recommended": "B"}
    ) == {"label": "B"}


def test_every_proposable_tool_declares_its_allowed_parameters():
    """A tool added to PROPOSABLE_TOOLS without an ALLOWED_PARAMS entry would
    raise KeyError at the moment a model proposed it, which is both the worst
    time and the hardest to reproduce."""
    from app.orchestrator.execution import ALLOWED_PARAMS

    missing = sorted(t.value for t in PROPOSABLE_TOOLS.values() if t not in ALLOWED_PARAMS)
    assert missing == []


# ---------------------------------------------------------------------------
# The loop, end to end
# ---------------------------------------------------------------------------

WORKSPACE = "00000000-0000-4000-8000-000000000050"
WRITABLE_ACCOUNT = "1000000000000003"


def _reachable() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _reachable(), reason="local Postgres is not running")


class ProposingRouter:
    """A strategy agent that returns a proposal carrying a typed action.

    The real one needs a model key. What is under test here is the wiring
    between a proposal and the tool pipeline, so the proposal is an input rather
    than something to be generated.
    """

    def __init__(self, entity_id: str, budget: float) -> None:
        self.entity_id = entity_id
        self.budget = budget
        self.calls: list[str] = []

    def complete(self, role, *, system, user, **kw):
        from app.models.router import Completion

        self.calls.append(role)
        return Completion(
            text=f"[{role}]", model="stub/model", role=role, model_class="judgement",
            tokens_in=100, tokens_out=50, cost_usd=0.01, cost_inr=0.88,
            latency_ms=5, finish_reason="stop",
        )

    def complete_json(self, role, *, system, user, schema, **kw):
        from app.models.router import Completion

        self.calls.append(role)
        obj = {
            "goal": "raise the budget on the winning ad set",
            "assumptions": ["the confirm rate holds"],
            "options": [
                {
                    "label": "A",
                    "what": f"raise to Rs {self.budget:,.0f}",
                    "expected_effect": "about 20% more leads at a similar CPL",
                    "risk": "learning phase disturbance",
                    "cost_of_being_wrong": "one day of worse delivery",
                    "horizon_days": 7,
                    "action": {
                        "tool": "update_budget",
                        "ad_account_id": WRITABLE_ACCOUNT,
                        "target_entity_id": self.entity_id,
                        "params": {"daily_budget_inr": self.budget},
                    },
                },
                {
                    "label": "B",
                    "what": "hold and gather another week of data",
                    "expected_effect": "no change",
                    "risk": "opportunity cost",
                    "cost_of_being_wrong": "a slower week",
                },
            ],
            "recommended": "A",
            "single_strongest_reason": "the confirm rate has held for nine days",
            "questions": [],
        }
        return obj, Completion(
            text="", model="stub/model", role=role, model_class="judgement",
            tokens_in=200, tokens_out=100, cost_usd=0.02, cost_inr=1.76,
            latency_ms=5, finish_reason="stop",
        )


@pytest.fixture
def seeded_ad_set():
    """A real ad set in the fixture driver, at Rs 1,000.

    Cleans up afterwards, unlike most of this repository's integration tests.
    These write to `runs`, `decisions`, `approvals` and `actions` - the four
    tables `workspace_policy` reads to compute committed spend - so leaving rows
    behind does not merely accumulate, it changes what a later cap test sees.
    Deleting the runs and the decisions is enough: approvals, actions and
    outcomes are all `on delete cascade` from decisions.
    """
    from app.deps import get_driver

    driver = get_driver()
    campaign = driver.create_campaign(
        WRITABLE_ACCOUNT, name="e2e", objective="OUTCOME_LEADS",
        idempotency_key=f"e2e-c-{uuid.uuid4()}",
    ).entity
    ad_set = driver.create_ad_set(
        WRITABLE_ACCOUNT,
        campaign_id=campaign.id,
        name="e2e-as",
        daily_budget_inr=1_000.0,
        optimisation_event="LEAD",
        idempotency_key=f"e2e-as-{uuid.uuid4()}",
    ).entity

    yield ad_set

    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            delete from t_advit.decisions
             where id in (select d.id from t_advit.decisions d
                            join t_advit.runs r on r.id = d.run_id
                           where r.thread_id like 'e2e-%')
            """
        )
        cur.execute("delete from t_advit.runs where thread_id like 'e2e-%'")


def _run_a_proposal(ad_set, budget: float = 1_200.0):
    """Drive the graph the way the chat route does, and return (run_id, state)."""
    from app.agents.compliance import ComplianceGate
    from app.deps import get_pipeline
    from app.orchestrator.graph import Orchestrator, build_graph
    from conftest import OWNER, acting_as

    orch = Orchestrator(
        router=ProposingRouter(ad_set.id, budget),
        gate_factory=lambda bt: ComplianceGate([]),
        pipeline=get_pipeline(),
    )
    graph = build_graph(orch)
    run_id = str(uuid.uuid4())
    with acting_as(OWNER):
        state = graph.invoke(
            {
                "run_id": run_id,
                "workspace_id": WORKSPACE,
                "thread_id": f"e2e-{run_id}",
                "trigger": "user_message",
                "message": "Budget badha do 1200 tak",
            }
        )
    return run_id, state


def _rows(sql: str, params: tuple):
    with psycopg.connect(DSN, row_factory=psycopg.rows.dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


@needs_db
def test_a_proposal_records_its_decision_before_anything_executes(seeded_ad_set):
    """The ordering the learning loop depends on.

    `expected_effect_json` and `horizon_days` have to be written before the
    outcome is known, or the loop compares a result against a prediction made
    after the fact. `t_advit.outcomes` is queued at that horizon, and
    `PostgresAuditSink.pre` refuses a mutating call with no decision_id at all.
    """
    run_id, state = _run_a_proposal(seeded_ad_set)

    assert state.get("decision_id"), "the run proposed and recorded no decision"

    decision = _rows(
        """
        select d.run_id::text as run_id, d.decision_type, d.chosen_option,
               d.reasoning, d.expected_effect_json, d.horizon_days, d.created_at,
               (select min(a.created_at) from t_advit.approvals a
                 where a.decision_id = d.id) as approval_at
          from t_advit.decisions d where d.id = %s::uuid
        """,
        (state["decision_id"],),
    )
    assert decision, "no t_advit.decisions row was written"
    row = decision[0]

    assert row["run_id"] == run_id, "the decision does not name its run"
    assert row["decision_type"] == "update_budget"
    assert row["chosen_option"] == "A"
    assert row["reasoning"]
    assert row["expected_effect_json"]["expected_effect"]
    assert row["horizon_days"] == 7
    if row["approval_at"] is not None:
        assert row["created_at"] <= row["approval_at"], (
            "the decision was recorded after the approval it is supposed to justify"
        )


@needs_db
def test_the_run_is_recorded_from_the_moment_it_starts(seeded_ad_set):
    """`t_advit.runs` had no writer at all, which is why `decisions.run_id` - a
    foreign key to it - had nothing to point at.

    The row is written BEFORE any work, with status 'running'. A row written at
    the end records only the runs that finished, which is the opposite of what
    an audit trail is for.
    """
    run_id, state = _run_a_proposal(seeded_ad_set)

    run = _rows(
        "select status::text as status, intent, ended_at, cost_inr, thread_id "
        "from t_advit.runs where id = %s::uuid",
        (run_id,),
    )
    assert run, "no t_advit.runs row was written"
    assert run[0]["ended_at"] is not None
    assert run[0]["status"] in ("completed", "awaiting_approval"), run[0]["status"]
    assert float(run[0]["cost_inr"]) > 0, "the run recorded no model spend"


@needs_db
def test_a_budget_change_suspends_at_the_approval_gate_rather_than_running(seeded_ad_set):
    """The seeded workspace is at autonomy L1, where a budget change needs a
    human. The proposal must produce an approval and no action."""
    run_id, state = _run_a_proposal(seeded_ad_set)

    execution = state.get("execution") or {}
    assert execution.get("attempted") is True, (
        f"the media-buying node never reached the pipeline: {execution}"
    )
    assert execution["decision"] == "awaiting_approval", execution
    assert execution["approval_id"]
    assert state.get("mode") != "executed"

    actions = _rows(
        "select count(*) as n from t_advit.actions where decision_id = %s::uuid",
        (state["decision_id"],),
    )
    assert actions[0]["n"] == 0, "an action ran before anyone approved it"


@needs_db
def test_answering_the_approval_is_what_carries_the_action_out(seeded_ad_set):
    """The half that was missing.

    Answering an approval used to update a row and stop. `ToolPipeline.invoke`
    had one caller in the whole application - the rollback route, which needs an
    actions row that only invoke() creates - so the approvals inbox was a button
    that recorded an opinion.
    """
    from fastapi.testclient import TestClient

    from app.main import app
    from conftest import OWNER, auth

    run_id, state = _run_a_proposal(seeded_ad_set)
    approval_id = (state.get("execution") or {}).get("approval_id")
    assert approval_id

    client = TestClient(app)
    client.headers.update(auth(OWNER))
    response = client.post(
        f"/api/approvals/{approval_id}/respond", json={"action": "approve"}
    )
    assert response.status_code == 200, response.text

    execution = response.json().get("execution") or {}
    assert execution.get("attempted") is True, response.json()
    assert execution["decision"] == "executed", execution
    assert execution["verified"] is True, "the change was not read back and verified"
    assert execution["rollback_handle"], "an executed change with no way back"

    action = _rows(
        """
        select a.verified, a.rollback_handle, a.after_state_json,
               a.approval_id::text as approval_id
          from t_advit.actions a where a.decision_id = %s::uuid
        """,
        (state["decision_id"],),
    )
    assert action, "no t_advit.actions row - the loop is still open"
    assert action[0]["verified"] is True
    assert action[0]["approval_id"] == approval_id, (
        "the action does not name the approval that authorised it"
    )
    assert float(action[0]["after_state_json"]["daily_budget_inr"]) == 1_200.0


@needs_db
def test_a_rejected_approval_executes_nothing(seeded_ad_set):
    """A rejection is a training signal, not a slower yes."""
    from fastapi.testclient import TestClient

    from app.main import app
    from conftest import OWNER, auth

    run_id, state = _run_a_proposal(seeded_ad_set)
    approval_id = (state.get("execution") or {}).get("approval_id")

    client = TestClient(app)
    client.headers.update(auth(OWNER))
    response = client.post(
        f"/api/approvals/{approval_id}/respond",
        json={"action": "reject", "reason": "RTO is climbing; not scaling this week"},
    )
    assert response.status_code == 200
    assert "execution" not in response.json()

    actions = _rows(
        "select count(*) as n from t_advit.actions where decision_id = %s::uuid",
        (state["decision_id"],),
    )
    assert actions[0]["n"] == 0


@needs_db
def test_a_proposal_the_model_could_not_type_leaves_the_run_proposed(seeded_ad_set):
    """An option with no `action` block, or one naming an invented tool, must
    produce a proposal the owner can read and a run that says why it could not
    be executed - not a failed turn, and not a silent nothing."""
    from app.agents.compliance import ComplianceGate
    from app.deps import get_pipeline
    from app.orchestrator.graph import Orchestrator, build_graph
    from conftest import OWNER, acting_as

    router = ProposingRouter(seeded_ad_set.id, 1_200.0)
    original = router.complete_json

    def untyped(role, *, system, user, schema, **kw):
        obj, completion = original(role, system=system, user=user, schema=schema, **kw)
        obj["options"][0]["action"]["tool"] = "delete_everything"
        return obj, completion

    router.complete_json = untyped  # type: ignore[method-assign]

    orch = Orchestrator(
        router=router,
        gate_factory=lambda bt: ComplianceGate([]),
        pipeline=get_pipeline(),
    )
    run_id = str(uuid.uuid4())
    with acting_as(OWNER):
        state = build_graph(orch).invoke(
            {
                "run_id": run_id,
                "workspace_id": WORKSPACE,
                "thread_id": f"e2e-{run_id}",
                "trigger": "user_message",
                "message": "Budget badha do",
            }
        )

    assert state.get("mode") == "proposed"
    assert state.get("proposal"), "the owner lost the proposal along with the action"
    execution = state.get("execution") or {}
    assert execution.get("attempted") is False
    assert "not a proposable tool" in execution.get("reason", "")
