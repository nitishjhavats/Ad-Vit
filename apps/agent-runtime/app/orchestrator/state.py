"""Run state for the orchestrator graph.

The blackboard from PRD 7.1: agents never call each other, they read and write
this and the orchestrator decides who runs next. That is what makes a run
auditable and replayable rather than a chain of opaque handoffs.

One rule shapes the whole structure. ``facts`` is computed deterministically -
SQL and Python, never a model - and every agent is told to state no number that
is not in it (PRD 17.7, Appendix A). Models narrate, judge and propose; they do
not calculate. That removes an entire class of confident numerical error and is
a cost decision as much as a correctness one.
"""

from __future__ import annotations

import operator
from enum import Enum
from typing import Annotated, Any, TypedDict


class Intent(str, Enum):
    """The four conversation modes of PRD 5.3."""

    ASK = "ask"            # read-only; assemble an answer with evidence
    REPORT = "report"      # owner submits business truth
    PROPOSE = "propose"    # owner wants something consequential done
    EXECUTE = "execute"    # owner approved a proposal
    UNKNOWN = "unknown"


class Mode(str, Enum):
    """What the orchestrator decided to do (PRD 7.2 step 6)."""

    ANSWERED = "answered"
    PROPOSED = "proposed"
    ASKED = "asked"
    EXECUTED = "executed"
    HALTED = "halted"


class RunState(TypedDict, total=False):
    # --- identity ---
    run_id: str
    workspace_id: str
    org_id: str
    thread_id: str
    trigger: str
    message: str
    # An explicit creative bundle, when this turn is submitting one. Absent for
    # ordinary conversation, which the compliance gate must not treat as ad copy.
    creative: dict[str, Any] | str | None

    # --- classification ---
    intent: str
    intent_confidence: float
    goal: str
    assumptions: list[str]

    # --- context (retrieved, with record ids so claims are traceable) ---
    # Read from t_advit.workspaces, and deliberately NOT from account_context,
    # which any workspace member can write. It selects the compliance ruleset,
    # so a tenant-writable copy of it is a tenant-writable law.
    business_type: str
    account_context: list[dict[str, Any]]
    platform_knowledge: list[dict[str, Any]]
    # The three memory tiers (PRD 5.1). `learnings` carries tier 1 - what this
    # account has learned about itself, written by app/learning/promote.py from
    # measured outcomes - and any tier 2 or 3 rows that have been promoted into
    # it. `industry_patterns` is the tier-2 catalogue, which reaches an account
    # only after the independence gate (>= 3 workspaces, >= 2 owners) and a
    # superadmin's approval.
    learnings: list[dict[str, Any]]
    industry_patterns: list[dict[str, Any]]
    # Set by the strategy node when the recommended option would build a
    # campaign and no owner-asserted CTA is on file. decide() records no
    # decision for a held proposal; the turn ends ASKED.
    proposal_held: bool
    cta_gate: dict[str, Any]
    # The t_advit.held_proposals row the strategy node wrote for a held
    # proposal, so the chat response and the Suggestions tab name the same
    # question. None when nothing was held - and None when something was
    # held on a run decide() will halt (access mode not `full`): the
    # question is in the turn's text, but no open row is left for an
    # account that cannot act on it.
    held_proposal_id: str | None
    retrieved_record_ids: list[str]
    stable_prefix: str

    # --- computed facts: the ONLY place numbers may come from ---
    facts: dict[str, Any]
    facts_gaps: list[str]

    # --- agent outputs ---
    analysis: dict[str, Any]
    proposal: dict[str, Any]
    compliance: dict[str, Any]

    # --- governance ---
    effective_autonomy: int
    access_mode: str
    mode: str
    decision_id: str
    approval_id: str
    execution: dict[str, Any]

    # --- output ---
    narration: str
    questions: list[str]

    # --- telemetry. Reducers so concurrent nodes append rather than clobber. ---
    events: Annotated[list[dict[str, Any]], operator.add]
    completions: Annotated[list[dict[str, Any]], operator.add]
    errors: Annotated[list[str], operator.add]


def event(agent: str, kind: str, **payload: Any) -> dict[str, Any]:
    """One row of the live activity stream (PRD 7.5).

    The same stream feeds the UI tracker, the reasoning panel and the trace, so
    it is a typed event rather than a decorative animation.
    """
    return {"agent": agent, "event": f"agent.{kind}", **payload}
