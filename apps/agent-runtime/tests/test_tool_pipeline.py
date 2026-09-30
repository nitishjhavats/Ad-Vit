"""The tool invocation pipeline (PRD 10.8).

These are the tests that decide whether this system can be trusted with a
budget. Each one pins a step of the pipeline, and several encode PRD Appendix
D.1 - the twelve binary criteria for "Rs 2,500/day mein Product A ke qualified
leads chahiye".
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from app.meta.driver import EntityLevel, EntityStatus
from app.meta.fixture import FixtureDriver
from app.policy.pipeline import (
    AgentIdentity,
    Decision,
    DenialReason,
    ToolPipeline,
    ToolRequest,
    WorkspacePolicy,
)
from app.policy.risk import (
    RiskClass,
    Tool,
    requires_approval,
    risk_class,
)
from doubles import (
    ANALYTICS_TOOLS,
    FUNDED_ACCOUNT,
    MEDIA_BUYING_TOOLS,
    WORKSPACE,
    WRITABLE_ACCOUNT,
    FakeAuditSink,
    FakeLockManager,
    FakePolicyStore,
    FakeScheduler,
    workspace_policy,
)


@pytest.fixture
def driver() -> FixtureDriver:
    return FixtureDriver(write_allowlist={WRITABLE_ACCOUNT})


@pytest.fixture
def parts(driver):
    policy = FakePolicyStore()
    audit = FakeAuditSink()
    locks = FakeLockManager()
    scheduler = FakeScheduler()
    pipeline = ToolPipeline(
        driver=driver, policy=policy, audit=audit, locks=locks, scheduler=scheduler
    )
    return pipeline, driver, policy, audit, locks, scheduler


@pytest.fixture
def media_buyer() -> AgentIdentity:
    return AgentIdentity(name="media_buying", allowed_tools=MEDIA_BUYING_TOOLS)


def create_campaign(pipeline, agent, *, name="TEST|ACQ|LEADS|AYUR|2609|01", approval=None):
    return pipeline.invoke(
        agent,
        ToolRequest(
            tool=Tool.CREATE_CAMPAIGN_DRAFT,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            params={"name": name, "objective": "OUTCOME_LEADS"},
            decision_id="dec-1",
            approval_id=approval,
        ),
    )


def approve_and_retry(pipeline, agent, policy, first, request):
    policy.grant(first.approval_id)
    request.approval_id = first.approval_id
    return pipeline.invoke(agent, request)


# ---------------------------------------------------------------------------
# Step 1 - tool router
# ---------------------------------------------------------------------------


def test_agent_cannot_call_a_tool_outside_its_allow_list(parts):
    """An agent cannot request a capability, because requesting one is not a
    representable action (PRD 14.7)."""
    pipeline, *_ = parts
    analyst = AgentIdentity(name="analytics", allowed_tools=ANALYTICS_TOOLS)

    outcome = create_campaign(pipeline, analyst)

    assert outcome.decision is Decision.DENIED
    assert outcome.reason is DenialReason.TOOL_NOT_ALLOWED
    assert outcome.steps_completed == []


def test_read_only_agent_may_still_read(parts):
    pipeline, *_ = parts
    analyst = AgentIdentity(name="analytics", allowed_tools=ANALYTICS_TOOLS)

    outcome = pipeline.invoke(
        analyst,
        ToolRequest(
            tool=Tool.READ_ENTITY,
            workspace_id=WORKSPACE,
            ad_account_id=FUNDED_ACCOUNT,
            target_entity_id="120000000000000001",
        ),
    )
    assert outcome.decision is Decision.EXECUTED


# ---------------------------------------------------------------------------
# Step 2 - autonomy, and the risk matrix behind it
# ---------------------------------------------------------------------------


def test_medium_risk_needs_approval_below_l2_and_not_above(parts, media_buyer):
    pipeline, driver, policy, *_ = parts

    assert requires_approval(Tool.CREATE_CAMPAIGN_DRAFT, 1) is True
    assert requires_approval(Tool.CREATE_CAMPAIGN_DRAFT, 2) is False


def test_pausing_is_pre_authorised_from_l1(parts, media_buyer):
    """Pausing is HIGH class - it changes live delivery - but stopping
    something is always safe, and PRD 10.5 grants it from L1."""
    assert risk_class(Tool.PAUSE_ENTITY) is RiskClass.HIGH
    assert requires_approval(Tool.PAUSE_ENTITY, 1) is False
    assert requires_approval(Tool.PAUSE_ENTITY, 0) is True


@pytest.mark.parametrize("autonomy", [0, 1, 2, 3, 4])
def test_activation_needs_approval_at_every_tier_including_l4(autonomy):
    """A permanent design commitment, not a v1 limitation (PRD 4.5)."""
    assert requires_approval(Tool.ACTIVATE_ENTITY, autonomy) is True


@pytest.mark.parametrize(
    "tool",
    [Tool.CHANGE_OBJECTIVE, Tool.CHANGE_OPTIMISATION_EVENT, Tool.UPDATE_TARGETING],
)
def test_structural_change_never_becomes_autonomous(tool):
    assert requires_approval(tool, 4) is True


def test_autonomy_at_l2_executes_a_draft_without_approval(driver, media_buyer):
    policy = FakePolicyStore(workspace_policy(effective_autonomy=2))
    pipeline = ToolPipeline(
        driver=driver, policy=policy, audit=FakeAuditSink(), locks=FakeLockManager()
    )
    outcome = create_campaign(pipeline, media_buyer)

    assert outcome.decision is Decision.EXECUTED
    assert policy.created_approvals == []


# ---------------------------------------------------------------------------
# Step 3 - tenant and connection checks
# ---------------------------------------------------------------------------


def test_unconnected_ad_account_is_refused(parts, media_buyer):
    pipeline, *_ = parts
    outcome = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.CREATE_CAMPAIGN_DRAFT,
            workspace_id=WORKSPACE,
            ad_account_id="9999999999",
            params={"name": "x", "objective": "OUTCOME_LEADS"},
        ),
    )
    assert outcome.reason is DenialReason.TENANT_MISMATCH


def test_funded_account_is_connected_read_only(parts, media_buyer):
    """The two funded Demo Brand accounts are seeded read-only. The product's
    switch refuses the write before the driver's allowlist is even consulted."""
    pipeline, *_ = parts
    outcome = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.CREATE_CAMPAIGN_DRAFT,
            workspace_id=WORKSPACE,
            ad_account_id=FUNDED_ACCOUNT,
            params={"name": "x", "objective": "OUTCOME_LEADS"},
        ),
    )
    assert outcome.reason is DenialReason.CONNECTION_NOT_WRITABLE


def test_driver_allowlist_is_an_independent_second_switch(media_buyer):
    """Even if the product's write_enabled column were set wrongly, the
    operator's allowlist still refuses."""
    driver = FixtureDriver(write_allowlist=set())          # operator: nothing writable
    policy = FakePolicyStore(connections={WRITABLE_ACCOUNT: True})  # product: writable
    audit = FakeAuditSink()
    pipeline = ToolPipeline(
        driver=driver, policy=policy, audit=audit, locks=FakeLockManager()
    )

    first = create_campaign(pipeline, media_buyer)
    outcome = approve_and_retry(
        pipeline, media_buyer, policy, first,
        ToolRequest(
            tool=Tool.CREATE_CAMPAIGN_DRAFT,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            params={"name": "x", "objective": "OUTCOME_LEADS"},
            decision_id="dec-1",
        ),
    )
    assert outcome.reason is DenialReason.CONNECTION_NOT_WRITABLE


# ---------------------------------------------------------------------------
# Step 4 - parameter validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params,fragment",
    [
        ({"objective": "OUTCOME_LEADS"}, "name is required"),
        ({"name": "  ", "objective": "OUTCOME_LEADS"}, "name is required"),
        ({"name": "x"}, "objective is required"),
    ],
)
def test_malformed_parameters_are_refused(parts, media_buyer, params, fragment):
    pipeline, *_ = parts
    outcome = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.CREATE_CAMPAIGN_DRAFT,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            params=params,
        ),
    )
    assert outcome.reason is DenialReason.INVALID_PARAMETERS
    assert fragment in outcome.message


def test_negative_budget_is_refused(parts, media_buyer):
    """A hallucinated budget must not reach the API (PRD 14.7)."""
    pipeline, *_ = parts
    outcome = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.CREATE_AD_SET_DRAFT,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            params={
                "name": "x",
                "campaign_id": "c1",
                "daily_budget_inr": -500,
                "optimisation_event": "LEAD",
            },
        ),
    )
    assert outcome.reason is DenialReason.INVALID_PARAMETERS


# ---------------------------------------------------------------------------
# Step 5 - guardrails
# ---------------------------------------------------------------------------


def test_creating_a_paused_object_commits_no_spend(driver, media_buyer):
    """The whole point of paused-first: a draft cannot breach a spend cap,
    because a paused object costs nothing."""
    policy = FakePolicyStore(
        workspace_policy(effective_autonomy=2, daily_cap_inr=100.0, spend_today_inr=99.0)
    )
    pipeline = ToolPipeline(
        driver=driver, policy=policy, audit=FakeAuditSink(), locks=FakeLockManager()
    )
    assert create_campaign(pipeline, media_buyer).decision is Decision.EXECUTED


def test_activation_that_would_breach_the_daily_cap_is_refused(driver, media_buyer):
    """Caps are absolute: the system halts and asks rather than exceeding them
    by a rupee (PRD 10.5)."""
    policy = FakePolicyStore(
        workspace_policy(effective_autonomy=4, daily_cap_inr=5000.0, spend_today_inr=4800.0)
    )
    audit = FakeAuditSink()
    pipeline = ToolPipeline(
        driver=driver, policy=policy, audit=audit, locks=FakeLockManager()
    )

    campaign = create_campaign(pipeline, media_buyer)
    ad_set = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.CREATE_AD_SET_DRAFT,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            params={
                "name": "ACQ|BROAD|IN-N|LEAD|01",
                "campaign_id": campaign.entity.id,
                "daily_budget_inr": 2500.0,
                "optimisation_event": "LEAD",
            },
            decision_id="dec-2",
        ),
    )
    assert ad_set.decision is Decision.EXECUTED

    # Guardrails are step 5 and the approval gate is step 6, so a breach is
    # refused outright: the owner is never asked to approve something already
    # known to exceed a hard cap.
    activation = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.ACTIVATE_ENTITY,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            target_entity_id=ad_set.entity.id,
            decision_id="dec-3",
        ),
    )

    assert activation.decision is Decision.DENIED
    assert activation.reason is DenialReason.GUARDRAIL_BREACH
    assert activation.steps_completed == [1, 2, 3, 4]
    assert policy.created_approvals == [], "no approval should be raised for a capped action"

    breach = next(b for b in activation.breaches if b.guardrail == "daily_spend_cap")
    assert breach.threshold == 5000.0
    assert breach.observed == 7300.0     # 4800 already committed + 2500 requested
    assert audit.guardrail_events, "a breach must be recorded, not merely refused"

    # And the object stays paused.
    assert driver.get_entity(WRITABLE_ACCOUNT, ad_set.entity.id).status is EntityStatus.PAUSED


def test_committing_spend_without_an_ingested_basis_is_refused(driver, media_buyer):
    """Regression: the caps were checked against an assumed zero.

    Both spend figures come from `coalesce(sum(...), 0)` over metrics_daily and
    ad_sets. A workspace that has never been synced has no rows in either, so
    the sums returned 0, every projection came in under the ceiling, and the
    caps could never fire - on the first action, which is exactly when nobody
    has seen the account behave yet.

    "No rows" is a data gap, not a zero (PRD 14.1).
    """
    policy = FakePolicyStore(
        workspace_policy(
            effective_autonomy=4, daily_cap_inr=5000.0,
            spend_basis_known=False, month_basis_known=False
        )
    )
    audit = FakeAuditSink()
    pipeline = ToolPipeline(
        driver=driver, policy=policy, audit=audit, locks=FakeLockManager()
    )

    campaign = create_campaign(pipeline, media_buyer)
    ad_set = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.CREATE_AD_SET_DRAFT,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            params={
                "name": "ACQ|BROAD|IN-N|LEAD|01",
                "campaign_id": campaign.entity.id,
                "daily_budget_inr": 500.0,      # far below the cap
                "optimisation_event": "LEAD",
            },
            decision_id="dec-2",
        ),
    )
    assert ad_set.decision is Decision.EXECUTED

    activation = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.ACTIVATE_ENTITY,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            target_entity_id=ad_set.entity.id,
            decision_id="dec-3",
        ),
    )

    assert activation.decision is Decision.DENIED
    assert activation.reason is DenialReason.GUARDRAIL_BREACH

    breach = next(b for b in activation.breaches if b.guardrail == "spend_basis_unknown")
    assert breach.guardrail_class == "financial"
    assert "never" in breach.message or "no Meta spend" in breach.message
    assert audit.guardrail_events, "the refusal must be recorded, not merely returned"

    assert driver.get_entity(WRITABLE_ACCOUNT, ad_set.entity.id).status is EntityStatus.PAUSED


def test_a_paused_draft_still_works_without_an_ingested_basis(driver, media_buyer):
    """The guard is scoped to actions that actually commit money.

    A workspace connecting for the first time has nothing ingested yet, and
    building a paused structure is precisely what it should be doing. Blocking
    that would make the fix worse than the bug.
    """
    policy = FakePolicyStore(
        workspace_policy(
            effective_autonomy=2, spend_basis_known=False, month_basis_known=False
        )
    )
    pipeline = ToolPipeline(
        driver=driver, policy=policy, audit=FakeAuditSink(), locks=FakeLockManager()
    )

    assert create_campaign(pipeline, media_buyer).decision is Decision.EXECUTED


def test_budget_step_ceiling_blocks_a_4x_jump(driver, media_buyer):
    """PRD 5.3's worked example: a 4x jump resets the learning phase and
    destabilises delivery for 5-7 days."""
    policy = FakePolicyStore(workspace_policy(effective_autonomy=4))
    pipeline = ToolPipeline(
        driver=driver, policy=policy, audit=FakeAuditSink(), locks=FakeLockManager()
    )

    campaign = create_campaign(pipeline, media_buyer)
    ad_set = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.CREATE_AD_SET_DRAFT,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            params={
                "name": "as",
                "campaign_id": campaign.entity.id,
                "daily_budget_inr": 5000.0,
                "optimisation_event": "LEAD",
            },
        ),
    )

    outcome = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.UPDATE_BUDGET,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            target_entity_id=ad_set.entity.id,
            params={"daily_budget_inr": 20000.0},
        ),
    )

    assert outcome.reason is DenialReason.GUARDRAIL_BREACH
    breach = next(b for b in outcome.breaches if b.guardrail == "budget_step_ceiling")
    assert "learning phase" in breach.message


def test_a_20_percent_step_is_permitted(driver, media_buyer):
    policy = FakePolicyStore(workspace_policy(effective_autonomy=4))
    pipeline = ToolPipeline(
        driver=driver, policy=policy, audit=FakeAuditSink(), locks=FakeLockManager()
    )
    campaign = create_campaign(pipeline, media_buyer)
    ad_set = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.CREATE_AD_SET_DRAFT,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            params={
                "name": "as",
                "campaign_id": campaign.entity.id,
                "daily_budget_inr": 5000.0,
                "optimisation_event": "LEAD",
            },
        ),
    )
    outcome = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.UPDATE_BUDGET,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            target_entity_id=ad_set.entity.id,
            params={"daily_budget_inr": 6000.0},
        ),
    )
    assert outcome.decision is Decision.EXECUTED
    assert outcome.after_state["daily_budget_inr"] == 6000.0


# ---------------------------------------------------------------------------
# Kill switch and subscription state outrank everything
# ---------------------------------------------------------------------------


def test_frozen_workspace_halts_mutations(driver, media_buyer):
    policy = FakePolicyStore(workspace_policy(effective_autonomy=4, is_paused=True))
    pipeline = ToolPipeline(
        driver=driver, policy=policy, audit=FakeAuditSink(), locks=FakeLockManager()
    )
    outcome = create_campaign(pipeline, media_buyer)

    assert outcome.decision is Decision.HALTED
    assert outcome.reason is DenialReason.WORKSPACE_PAUSED


def test_read_only_access_mode_blocks_writes_but_not_reads(driver, media_buyer):
    """A lapsed payment degrades to read-only rather than failing open, so it
    never strands live campaigns (PRD 18)."""
    policy = FakePolicyStore(workspace_policy(effective_autonomy=4, access_mode="read_only"))
    pipeline = ToolPipeline(
        driver=driver, policy=policy, audit=FakeAuditSink(), locks=FakeLockManager()
    )

    assert create_campaign(pipeline, media_buyer).reason is DenialReason.ACCESS_READ_ONLY

    read = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.READ_ENTITY,
            workspace_id=WORKSPACE,
            ad_account_id=FUNDED_ACCOUNT,
            target_entity_id="120000000000000001",
        ),
    )
    assert read.decision is Decision.EXECUTED


# ---------------------------------------------------------------------------
# Step 6 - approvals
# ---------------------------------------------------------------------------


def test_no_object_is_created_before_approval(parts, media_buyer):
    """PRD Appendix D.1: no object exists in the Meta account before approval
    is given."""
    pipeline, driver, policy, *_ = parts

    before = driver.get_entities(WRITABLE_ACCOUNT, EntityLevel.CAMPAIGN)
    outcome = create_campaign(pipeline, media_buyer)
    after = driver.get_entities(WRITABLE_ACCOUNT, EntityLevel.CAMPAIGN)

    assert outcome.decision is Decision.AWAITING_APPROVAL
    assert outcome.entity is None
    assert len(after) == len(before)


def test_rejected_proposal_never_executes(parts, media_buyer):
    pipeline, driver, policy, *_ = parts
    first = create_campaign(pipeline, media_buyer)
    policy.reject(first.approval_id)

    outcome = create_campaign(pipeline, media_buyer, approval=first.approval_id)

    assert outcome.reason is DenialReason.APPROVAL_REJECTED
    assert driver.get_entities(WRITABLE_ACCOUNT, EntityLevel.CAMPAIGN) == []


def test_expired_approval_is_refused_rather_than_executed(parts, media_buyer):
    """A budget proposal built on Tuesday's data is void by Friday; the system
    re-derives rather than executing stale intent (PRD 10.6)."""
    pipeline, driver, policy, *_ = parts
    first = create_campaign(pipeline, media_buyer)
    policy.expire(first.approval_id)

    outcome = create_campaign(pipeline, media_buyer, approval=first.approval_id)

    assert outcome.reason is DenialReason.APPROVAL_EXPIRED
    assert "re-derive" in outcome.message


def test_approval_carries_the_rupee_impact(parts, media_buyer):
    pipeline, driver, policy, *_ = parts
    campaign = create_campaign(pipeline, media_buyer)
    policy.grant(campaign.approval_id)
    campaign = create_campaign(pipeline, media_buyer, approval=campaign.approval_id)

    ad_set = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.CREATE_AD_SET_DRAFT,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            params={
                "name": "as",
                "campaign_id": campaign.entity.id,
                "daily_budget_inr": 2500.0,
                "optimisation_event": "LEAD",
            },
        ),
    )
    policy.grant(ad_set.approval_id)
    ad_set = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.CREATE_AD_SET_DRAFT,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            params={
                "name": "as",
                "campaign_id": campaign.entity.id,
                "daily_budget_inr": 2500.0,
                "optimisation_event": "LEAD",
            },
            approval_id=ad_set.approval_id,
        ),
    )

    activation = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.ACTIVATE_ENTITY,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            target_entity_id=ad_set.entity.id,
        ),
    )
    record = next(a for a in policy.created_approvals if a["id"] == activation.approval_id)
    assert record["impact_inr"] == 2500.0
    assert record["risk"] is RiskClass.CRITICAL


# ---------------------------------------------------------------------------
# Step 7 - freshness re-check
# ---------------------------------------------------------------------------


def test_freezing_between_approval_and_execution_halts_a_critical_action(driver, media_buyer):
    """Policy state can change between approval and execution, so CRITICAL
    actions re-evaluate immediately before executing (PRD 14.6)."""
    policy = FakePolicyStore(workspace_policy(effective_autonomy=4))
    pipeline = ToolPipeline(
        driver=driver, policy=policy, audit=FakeAuditSink(), locks=FakeLockManager()
    )

    campaign = create_campaign(pipeline, media_buyer)
    ad_set = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.CREATE_AD_SET_DRAFT,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            params={
                "name": "as",
                "campaign_id": campaign.entity.id,
                "daily_budget_inr": 1000.0,
                "optimisation_event": "LEAD",
            },
        ),
    )
    first = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.ACTIVATE_ENTITY,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            target_entity_id=ad_set.entity.id,
        ),
    )
    policy.grant(first.approval_id)

    # The owner hits the kill switch after approving.
    policy.set_fresh_policy(workspace_policy(effective_autonomy=4, is_paused=True))
    policy.policy_reads = 0

    outcome = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.ACTIVATE_ENTITY,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            target_entity_id=ad_set.entity.id,
            approval_id=first.approval_id,
        ),
    )
    assert outcome.decision is Decision.HALTED


# ---------------------------------------------------------------------------
# Step 8 - concurrency
# ---------------------------------------------------------------------------


def test_lock_timeout_refuses_rather_than_queueing(driver, media_buyer):
    """The state a blocked run reasoned about may no longer exist, so it
    re-derives instead of waiting (PRD 10.9)."""
    policy = FakePolicyStore(workspace_policy(effective_autonomy=2))
    pipeline = ToolPipeline(
        driver=driver,
        policy=policy,
        audit=FakeAuditSink(),
        locks=FakeLockManager(always_fail=True),
    )
    outcome = create_campaign(pipeline, media_buyer)

    assert outcome.reason is DenialReason.LOCK_TIMEOUT
    assert "re-derive" in outcome.message


def test_concurrent_writes_are_serialised_per_ad_account(driver, media_buyer):
    """One writer per ad account, and one per workspace.

    Two locks are held during a mutation now, always in the same order:
    `workspace:<id>` and then the ad account. The workspace one is what makes
    the cap arithmetic atomic - the caps are workspace-scoped, so a workspace
    holding three ad accounts would otherwise run three concurrent mutations
    against three different locks and check one shared daily cap three times
    against the same figure.
    """
    policy = FakePolicyStore(workspace_policy(effective_autonomy=2))
    locks = FakeLockManager()
    audit = FakeAuditSink()
    pipeline = ToolPipeline(driver=driver, policy=policy, audit=audit, locks=locks)

    observed: list[frozenset[str]] = []
    original_pre = audit.pre

    def watching_pre(**kwargs):
        observed.append(frozenset(locks.held))
        return original_pre(**kwargs)

    audit.pre = watching_pre  # type: ignore[method-assign]

    def worker(n: int) -> None:
        pipeline.invoke(
            media_buyer,
            ToolRequest(
                tool=Tool.CREATE_CAMPAIGN_DRAFT,
                workspace_id=WORKSPACE,
                ad_account_id=WRITABLE_ACCOUNT,
                params={"name": f"c{n}", "objective": "OUTCOME_LEADS"},
                decision_id=f"dec-{n}",
            ),
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(observed) == 6
    # Every audited write saw exactly its own two locks and nobody else's.
    expected = frozenset({f"workspace:{WORKSPACE}", WRITABLE_ACCOUNT})
    assert set(observed) == {expected}, (
        f"a write ran while a different set of locks was held: {set(observed)}"
    )


def test_the_workspace_lock_is_taken_before_the_account_lock(driver, media_buyer):
    """A fixed order is the entire deadlock argument.

    Two runs taking the same two locks in opposite orders deadlock under load
    and nowhere else, which is the worst possible place to discover it. There is
    exactly one site that takes them, so keeping the order is cheap - and this
    is what makes "keep it" a rule rather than a comment.
    """
    policy = FakePolicyStore(workspace_policy(effective_autonomy=2))
    locks = FakeLockManager()
    pipeline = ToolPipeline(
        driver=driver, policy=policy, audit=FakeAuditSink(), locks=locks
    )

    create_campaign(pipeline, media_buyer)

    assert locks.acquisitions[:2] == [f"workspace:{WORKSPACE}", WRITABLE_ACCOUNT]


def test_two_runs_against_one_cap_do_not_both_execute(driver, media_buyer):
    """The cap race, run as an actual race.

    A first attempt at this test ran the two invocations sequentially and passed
    with the fix reverted, which makes it a test of nothing: a sequential second
    run re-reads the policy at step 5 anyway and sees the first run's spend. The
    race needs both runs to complete their step-5 evaluation BEFORE either one
    executes, which is exactly what two requests arriving together do.

    The barrier below produces that, deterministically: both threads read the
    pre-mutation committed spend, both project Rs 1,000 onto Rs 8,500, both pass
    a Rs 10,000 cap, and only then do they contend for the lock.

    With the authoritative check outside the lock, both execute and the account
    ends at Rs 10,500 with no guardrail event, because from each run's point of
    view nothing was breached. With it inside, the loser re-evaluates against the
    winner's spend and is refused.
    """
    cap = 10_000.0
    committed = 8_500.0

    def policy_at(spend: float) -> WorkspacePolicy:
        return workspace_policy(
            effective_autonomy=4, daily_cap_inr=cap, spend_today_inr=spend
        )

    policy = FakePolicyStore(policy_at(committed))

    # Hold every thread just AFTER its first policy read until both have
    # arrived, so both are holding the same pre-mutation figure before either
    # proceeds.
    #
    # After, not before, and the first version of this test got it wrong: a
    # barrier ahead of the read synchronises only entry, and the winner then
    # ran the whole way through execution - guardrails, lock, driver, audit -
    # before the loser's read statement was scheduled. The loser read the
    # POST-mutation figure and was refused at step 5, so the test passed with
    # the fix reverted, which makes it a test of nothing.
    #
    # Later reads - the authoritative one inside the lock - must not wait, or
    # the winner would block forever on a barrier the loser cannot reach.
    gate = threading.Barrier(2, timeout=10)
    seen: set[int] = set()
    seen_lock = threading.Lock()
    original_read = policy.workspace_policy

    def gated_read(workspace_id: str) -> WorkspacePolicy:
        with seen_lock:
            first = threading.get_ident() not in seen
            seen.add(threading.get_ident())
        resolved = original_read(workspace_id)
        if first:
            gate.wait()
        return resolved

    policy.workspace_policy = gated_read  # type: ignore[method-assign]

    executed: list[str] = []
    executed_lock = threading.Lock()
    audit = FakeAuditSink()
    original_post = audit.post

    def counting_post(*, audit_id, outcome):
        if outcome.decision is Decision.EXECUTED:
            with executed_lock:
                executed.append(audit_id)
                # What the real store reports once the budget is committed.
                policy.set_policy(policy_at(committed + 1_000.0 * len(executed)))
        return original_post(audit_id=audit_id, outcome=outcome)

    audit.post = counting_post  # type: ignore[method-assign]

    pipeline = ToolPipeline(
        driver=driver, policy=policy, audit=audit, locks=FakeLockManager()
    )

    campaign = driver.create_campaign(
        WRITABLE_ACCOUNT, name="race", objective="OUTCOME_LEADS", idempotency_key="race-c"
    ).entity
    # Two ad sets at Rs 5,000, each raised by exactly 20% - the largest step the
    # per-step ceiling allows, so the only guardrail in play is the cap.
    ad_sets = [
        driver.create_ad_set(
            WRITABLE_ACCOUNT,
            campaign_id=campaign.id,
            name=f"as{n}",
            daily_budget_inr=5_000.0,
            optimisation_event="LEAD",
            idempotency_key=f"race-as-{n}",
        ).entity
        for n in range(2)
    ]

    outcomes: dict[int, Any] = {}

    def worker(n: int) -> None:
        outcomes[n] = pipeline.invoke(
            media_buyer,
            ToolRequest(
                tool=Tool.UPDATE_BUDGET,
                workspace_id=WORKSPACE,
                ad_account_id=WRITABLE_ACCOUNT,
                target_entity_id=ad_sets[n].id,
                params={"daily_budget_inr": 6_000.0},
                decision_id=f"dec-{n}",
            ),
        )

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert len(outcomes) == 2, "a thread did not finish"

    # Rs 8,500 committed + Rs 1,000 + Rs 1,000 against a Rs 10,000 cap.
    assert len(executed) == 1, (
        f"{len(executed)} of two concurrent runs executed against one cap; the "
        "loser was evaluated against the pre-mutation figure"
    )

    decisions = sorted(o.decision for o in outcomes.values())
    assert Decision.EXECUTED in decisions
    loser = next(o for o in outcomes.values() if o.decision is not Decision.EXECUTED)
    assert loser.reason is DenialReason.GUARDRAIL_BREACH
    assert "daily cap" in loser.message


def test_intent_is_audited_before_the_call_not_after(parts, media_buyer):
    """The pre-record is what the retry path consults after an ambiguous
    timeout, so it must exist before the request leaves (PRD 10.9)."""
    pipeline, driver, policy, audit, *_ = parts
    first = create_campaign(pipeline, media_buyer)
    policy.grant(first.approval_id)
    create_campaign(pipeline, media_buyer, approval=first.approval_id)

    assert audit.order == ["pre:audit-1", "post:audit-1"]
    entry = audit.entries["audit-1"]
    assert entry.idempotency_key
    assert entry.policy_decision_id
    assert entry.agent == "media_buying"


def test_a_driver_error_still_writes_the_audit_pair(driver, media_buyer):
    policy = FakePolicyStore(workspace_policy(effective_autonomy=2))
    audit = FakeAuditSink()
    pipeline = ToolPipeline(
        driver=driver, policy=policy, audit=audit, locks=FakeLockManager()
    )
    outcome = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.CREATE_AD_SET_DRAFT,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            params={
                "name": "as",
                "campaign_id": "does-not-exist",
                "daily_budget_inr": 1000.0,
                "optimisation_event": "LEAD",
            },
        ),
    )
    assert outcome.reason is DenialReason.DRIVER_ERROR
    assert audit.order == ["pre:audit-1", "post:audit-1"]


# ---------------------------------------------------------------------------
# Paused-first, verification, rollback
# ---------------------------------------------------------------------------


def test_created_objects_are_paused(driver, media_buyer):
    """PRD Appendix D.1: on approval, every object is created PAUSED."""
    policy = FakePolicyStore(workspace_policy(effective_autonomy=2))
    pipeline = ToolPipeline(
        driver=driver, policy=policy, audit=FakeAuditSink(), locks=FakeLockManager()
    )
    outcome = create_campaign(pipeline, media_buyer)

    assert outcome.entity.status is EntityStatus.PAUSED
    assert outcome.after_state["status"] == "paused", "the persisted form is the database's vocabulary"


def test_write_is_verified_by_read_back_and_diff(driver, media_buyer):
    """Success is a read-back that matches the proposal, not a 200 response."""
    policy = FakePolicyStore(workspace_policy(effective_autonomy=2))
    pipeline = ToolPipeline(
        driver=driver, policy=policy, audit=FakeAuditSink(), locks=FakeLockManager()
    )
    outcome = create_campaign(pipeline, media_buyer)

    assert outcome.verified is True
    assert outcome.verification_diff == {}
    assert 11 in outcome.steps_completed and 12 in outcome.steps_completed


def test_verification_mismatch_refuses_to_report_success(driver, media_buyer):
    """A driver that writes something other than the proposal must not produce
    a success. Silent partial execution is the worst failure class here."""
    policy = FakePolicyStore(workspace_policy(effective_autonomy=2))
    pipeline = ToolPipeline(
        driver=driver, policy=policy, audit=FakeAuditSink(), locks=FakeLockManager()
    )

    real_create = driver.create_campaign

    def lying_create(ad_account_id, *, name, objective, idempotency_key):
        return real_create(
            ad_account_id,
            name="something-else-entirely",
            objective=objective,
            idempotency_key=idempotency_key,
        )

    driver.create_campaign = lying_create  # type: ignore[method-assign]

    outcome = create_campaign(pipeline, media_buyer, name="what-we-proposed")

    assert outcome.decision is Decision.DENIED
    assert outcome.reason is DenialReason.VERIFICATION_MISMATCH
    assert outcome.verified is False
    assert outcome.verification_diff["name"]["expected"] == "what-we-proposed"


def _lying_driver(driver, writes="something-else-entirely"):
    """Make the driver write something other than what was proposed, so the
    step-12 diff fires."""
    real_create = driver.create_campaign

    def lying_create(ad_account_id, *, name, objective, idempotency_key):
        return real_create(
            ad_account_id, name=writes, objective=objective,
            idempotency_key=idempotency_key,
        )

    driver.create_campaign = lying_create  # type: ignore[method-assign]
    return driver


def test_a_verification_mismatch_actually_rolls_back(driver, media_buyer):
    """Regression: the message said "rolled back" and nothing was rolled back.

    The write has already landed on Meta by step 12. Refusing to report success
    is necessary but not sufficient - the object is live, and the outcome
    claimed an undo that never happened. Nothing else in this product asserts an
    action it did not take, and this must not either.
    """
    policy = FakePolicyStore(workspace_policy(effective_autonomy=2))
    pipeline = ToolPipeline(
        driver=driver, policy=policy, audit=FakeAuditSink(), locks=FakeLockManager()
    )

    calls: list[tuple] = []
    real_status = driver.update_status

    def spying_status(ad_account_id, entity_id, status, *, idempotency_key):
        calls.append((entity_id, status))
        return real_status(
            ad_account_id, entity_id, status, idempotency_key=idempotency_key
        )

    driver.update_status = spying_status  # type: ignore[method-assign]
    _lying_driver(driver)

    outcome = create_campaign(pipeline, media_buyer, name="what-we-proposed")

    assert outcome.decision is Decision.DENIED
    assert outcome.reason is DenialReason.VERIFICATION_MISMATCH
    assert outcome.rolled_back is True, "the claim in the message must be true"
    assert outcome.rollback_error is None
    assert calls, "no rollback call reached the driver"
    assert calls[-1][1] is EntityStatus.PAUSED
    assert driver.get_entity(WRITABLE_ACCOUNT, outcome.entity.id).status is EntityStatus.PAUSED

    # And the caller is given a documented way back regardless.
    assert outcome.rollback_handle is not None
    assert outcome.rollback_handle["kind"] == "pause_created_entity"


def test_a_failed_rollback_is_reported_not_claimed(driver, media_buyer):
    """The rollback can fail too - Meta can be down, or the token revoked.

    That leaves a live object nobody approved, which is exactly the moment the
    system must escalate loudly rather than emit a reassuring sentence.
    """
    policy = FakePolicyStore(workspace_policy(effective_autonomy=2))
    pipeline = ToolPipeline(
        driver=driver, policy=policy, audit=FakeAuditSink(), locks=FakeLockManager()
    )

    def refusing_status(*a, **kw):
        raise RuntimeError("meta unreachable")

    driver.update_status = refusing_status  # type: ignore[method-assign]
    _lying_driver(driver)

    outcome = create_campaign(pipeline, media_buyer, name="what-we-proposed")

    assert outcome.decision is Decision.DENIED
    assert outcome.rolled_back is False
    assert "meta unreachable" in (outcome.rollback_error or "")
    assert "rollback did not succeed" in outcome.message
    assert "needs a human" in outcome.message
    # The handle survives, so the escalation is actionable rather than a dead end.
    assert outcome.rollback_handle is not None


def test_executed_action_carries_a_rollback_handle(driver, media_buyer):
    """PRD Appendix D.1: the action appears in the audit log with a rollback
    handle."""
    policy = FakePolicyStore(workspace_policy(effective_autonomy=2))
    pipeline = ToolPipeline(
        driver=driver, policy=policy, audit=FakeAuditSink(), locks=FakeLockManager()
    )
    outcome = create_campaign(pipeline, media_buyer)

    assert outcome.rollback_handle["kind"] == "pause_created_entity"
    assert outcome.rollback_handle["entity_id"] == outcome.entity.id


def test_budget_rollback_handle_captures_the_prior_value(driver, media_buyer):
    policy = FakePolicyStore(workspace_policy(effective_autonomy=4))
    pipeline = ToolPipeline(
        driver=driver, policy=policy, audit=FakeAuditSink(), locks=FakeLockManager()
    )
    campaign = create_campaign(pipeline, media_buyer)
    ad_set = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.CREATE_AD_SET_DRAFT,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            params={
                "name": "as",
                "campaign_id": campaign.entity.id,
                "daily_budget_inr": 5000.0,
                "optimisation_event": "LEAD",
            },
        ),
    )
    outcome = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.UPDATE_BUDGET,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            target_entity_id=ad_set.entity.id,
            params={"daily_budget_inr": 5500.0},
        ),
    )
    assert outcome.rollback_handle["kind"] == "restore_budget"
    assert outcome.rollback_handle["daily_budget_inr"] == 5000.0


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_the_same_request_yields_the_same_idempotency_key(parts, media_buyer):
    pipeline, *_ = parts
    a = ToolRequest(
        tool=Tool.CREATE_CAMPAIGN_DRAFT,
        workspace_id=WORKSPACE,
        ad_account_id=WRITABLE_ACCOUNT,
        params={"name": "c", "objective": "OUTCOME_LEADS"},
        decision_id="dec-1",
    )
    b = ToolRequest(
        tool=Tool.CREATE_CAMPAIGN_DRAFT,
        workspace_id=WORKSPACE,
        ad_account_id=WRITABLE_ACCOUNT,
        params={"objective": "OUTCOME_LEADS", "name": "c"},   # different order
        decision_id="dec-1",
    )
    assert a.derive_idempotency_key() == b.derive_idempotency_key()


def test_replaying_a_write_does_not_create_a_duplicate(driver, media_buyer):
    """Creating a duplicate campaign because a response was lost in transit is
    not an acceptable outcome (PRD 10.9)."""
    policy = FakePolicyStore(workspace_policy(effective_autonomy=2))
    pipeline = ToolPipeline(
        driver=driver, policy=policy, audit=FakeAuditSink(), locks=FakeLockManager()
    )

    def request() -> ToolRequest:
        return ToolRequest(
            tool=Tool.CREATE_CAMPAIGN_DRAFT,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            params={"name": "c", "objective": "OUTCOME_LEADS"},
            decision_id="dec-1",
        )

    first = pipeline.invoke(media_buyer, request())
    second = pipeline.invoke(media_buyer, request())

    assert first.decision is Decision.EXECUTED
    assert second.decision is Decision.EXECUTED
    assert first.entity.id == second.entity.id
    assert len(driver.get_entities(WRITABLE_ACCOUNT, EntityLevel.CAMPAIGN)) == 1


def test_different_parameters_produce_different_keys(parts):
    a = ToolRequest(
        tool=Tool.CREATE_CAMPAIGN_DRAFT,
        workspace_id=WORKSPACE,
        ad_account_id=WRITABLE_ACCOUNT,
        params={"name": "c1", "objective": "OUTCOME_LEADS"},
        decision_id="dec-1",
    )
    b = ToolRequest(
        tool=Tool.CREATE_CAMPAIGN_DRAFT,
        workspace_id=WORKSPACE,
        ad_account_id=WRITABLE_ACCOUNT,
        params={"name": "c2", "objective": "OUTCOME_LEADS"},
        decision_id="dec-1",
    )
    assert a.derive_idempotency_key() != b.derive_idempotency_key()


# ---------------------------------------------------------------------------
# Step 15 - the outcome check
# ---------------------------------------------------------------------------


def test_critical_action_queues_an_outcome_check_at_the_horizon(driver, media_buyer):
    """PRD 8.2: a system that only records outcomes learns to rationalise; one
    that records predictions learns to calibrate."""
    policy = FakePolicyStore(workspace_policy(effective_autonomy=4))
    scheduler = FakeScheduler()
    pipeline = ToolPipeline(
        driver=driver,
        policy=policy,
        audit=FakeAuditSink(),
        locks=FakeLockManager(),
        scheduler=scheduler,
    )

    campaign = create_campaign(pipeline, media_buyer)
    ad_set = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.CREATE_AD_SET_DRAFT,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            params={
                "name": "as",
                "campaign_id": campaign.entity.id,
                "daily_budget_inr": 1000.0,
                "optimisation_event": "LEAD",
            },
        ),
    )
    first = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.ACTIVATE_ENTITY,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            target_entity_id=ad_set.entity.id,
            decision_id="dec-activate",
            horizon_days=14,
        ),
    )
    policy.grant(first.approval_id)
    outcome = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.ACTIVATE_ENTITY,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            target_entity_id=ad_set.entity.id,
            decision_id="dec-activate",
            approval_id=first.approval_id,
            horizon_days=14,
        ),
    )

    assert outcome.decision is Decision.EXECUTED
    assert outcome.outcome_check_queued is True
    assert scheduler.queued == [{"decision_id": "dec-activate", "horizon_days": 14}]


def test_activation_is_a_separate_action_from_creation(driver, media_buyer):
    """The two are never one call. A misread budget becomes a paused artefact
    costing nothing, rather than a live campaign burning money (PRD 10.8)."""
    policy = FakePolicyStore(workspace_policy(effective_autonomy=2))
    pipeline = ToolPipeline(
        driver=driver, policy=policy, audit=FakeAuditSink(), locks=FakeLockManager()
    )
    campaign = create_campaign(pipeline, media_buyer)
    assert campaign.entity.status is EntityStatus.PAUSED

    activation = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.ACTIVATE_ENTITY,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            target_entity_id=campaign.entity.id,
        ),
    )
    # Even at L2, activation demands its own approval.
    assert activation.decision is Decision.AWAITING_APPROVAL
    assert driver.get_entity(WRITABLE_ACCOUNT, campaign.entity.id).status is EntityStatus.PAUSED


# ---------------------------------------------------------------------------
# Approval binding
#
# Regression suite for the worst audit finding: step 6 checked that an approval
# existed, was not rejected and had not expired - but never that it authorised
# THIS request. An approval was therefore a bearer token. Holding any valid id
# was permission to perform any mutation, so a Rs 500 pause approval would
# execute a Rs 200,000 activation.
# ---------------------------------------------------------------------------


def _approved_for(pipeline, agent, policy, request: ToolRequest) -> str:
    """Raise an approval for `request` and grant it."""
    first = pipeline.invoke(agent, request)
    assert first.decision is Decision.AWAITING_APPROVAL
    policy.grant(first.approval_id)
    return first.approval_id


def test_an_approval_does_not_authorise_a_different_tool(driver, media_buyer):
    policy = FakePolicyStore(workspace_policy(effective_autonomy=2))
    pipeline = ToolPipeline(
        driver=driver, policy=policy, audit=FakeAuditSink(), locks=FakeLockManager()
    )
    # Something real to act on, in the account that is actually writable.
    campaign = create_campaign(pipeline, media_buyer)
    assert campaign.decision is Decision.EXECUTED

    # PAUSE is pre-authorised from L1, so drop to L0 to force an approval.
    policy._policy = workspace_policy(effective_autonomy=0)
    approval_id = _approved_for(
        pipeline, media_buyer, policy,
        ToolRequest(
            tool=Tool.PAUSE_ENTITY, workspace_id=WORKSPACE, ad_account_id=WRITABLE_ACCOUNT,
            target_entity_id=campaign.entity.id, decision_id="d1",
        ),
    )

    # Present the pause approval for a campaign creation instead.
    outcome = create_campaign(pipeline, media_buyer, approval=approval_id)

    assert outcome.decision is Decision.DENIED
    assert outcome.reason is DenialReason.APPROVAL_MISMATCH
    assert "tool" in outcome.message


def test_an_approval_does_not_authorise_a_different_entity(driver, media_buyer):
    policy = FakePolicyStore(workspace_policy(effective_autonomy=2))
    pipeline = ToolPipeline(
        driver=driver, policy=policy, audit=FakeAuditSink(), locks=FakeLockManager()
    )
    first = create_campaign(pipeline, media_buyer, name="ONE")
    second = create_campaign(pipeline, media_buyer, name="TWO")

    def pause(entity_id: str, approval: str | None = None) -> ToolRequest:
        return ToolRequest(
            tool=Tool.PAUSE_ENTITY, workspace_id=WORKSPACE, ad_account_id=WRITABLE_ACCOUNT,
            target_entity_id=entity_id, decision_id="d1", approval_id=approval,
        )

    policy._policy = workspace_policy(effective_autonomy=0)
    approval_id = _approved_for(pipeline, media_buyer, policy, pause(first.entity.id))
    outcome = pipeline.invoke(media_buyer, pause(second.entity.id, approval_id))

    assert outcome.reason is DenialReason.APPROVAL_MISMATCH
    assert "target_entity_id" in outcome.message


def test_an_approval_for_one_budget_does_not_authorise_a_larger_one(driver, media_buyer):
    """The money case: approving Rs 6,000 must not execute Rs 20,000."""
    policy = FakePolicyStore(workspace_policy(effective_autonomy=4))
    pipeline = ToolPipeline(
        driver=driver, policy=policy, audit=FakeAuditSink(), locks=FakeLockManager()
    )

    campaign = create_campaign(pipeline, media_buyer)
    ad_set = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.CREATE_AD_SET_DRAFT, workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            params={"name": "as", "campaign_id": campaign.entity.id,
                    "daily_budget_inr": 5000.0, "optimisation_event": "LEAD"},
        ),
    )

    def budget(amount: float, approval: str | None = None) -> ToolRequest:
        return ToolRequest(
            tool=Tool.UPDATE_BUDGET, workspace_id=WORKSPACE, ad_account_id=WRITABLE_ACCOUNT,
            target_entity_id=ad_set.entity.id, params={"daily_budget_inr": amount},
            decision_id="d1", approval_id=approval,
        )

    # At L4 a budget change is auto, so drop to L0 to force an approval.
    policy._policy = workspace_policy(effective_autonomy=0)
    approval_id = _approved_for(pipeline, media_buyer, policy, budget(5200.0))

    # Rs 5,900 is a different amount but still inside the 20% step ceiling, so
    # the guardrail at step 5 lets it through and the BINDING is what refuses
    # it. (Rs 20,000 would be caught earlier, by the ceiling - correctly, but
    # that would not exercise this fix.)
    outcome = pipeline.invoke(media_buyer, budget(5900.0, approval_id))

    assert outcome.reason is DenialReason.APPROVAL_MISMATCH
    assert "params" in outcome.message
    assert driver.get_entity(WRITABLE_ACCOUNT, ad_set.entity.id).fields[
        "daily_budget_inr"
    ] == 5000.0, "the unapproved amount must not have been written"


def test_an_approval_from_another_workspace_is_invisible(driver, media_buyer):
    """Scoped at the store, so a cross-tenant approval cannot even be read."""
    policy = FakePolicyStore(workspace_policy(effective_autonomy=0))
    pipeline = ToolPipeline(
        driver=driver, policy=policy, audit=FakeAuditSink(), locks=FakeLockManager()
    )

    approval_id = policy.create_approval(
        workspace_id="00000000-0000-4000-8000-999999999999",
        decision_id="d1", risk=RiskClass.MEDIUM,
        proposed={"authorisation": {"tool": "create_campaign_draft"}}, impact_inr=None,
    )
    policy.grant(approval_id)

    outcome = create_campaign(pipeline, media_buyer, approval=approval_id)
    assert outcome.reason is DenialReason.APPROVAL_EXPIRED
    assert "not found" in outcome.message


def test_the_matching_approval_still_executes(driver, media_buyer):
    """The fix must not break the legitimate path."""
    policy = FakePolicyStore(workspace_policy(effective_autonomy=1))
    pipeline = ToolPipeline(
        driver=driver, policy=policy, audit=FakeAuditSink(), locks=FakeLockManager()
    )

    first = create_campaign(pipeline, media_buyer)
    policy.grant(first.approval_id)
    outcome = create_campaign(pipeline, media_buyer, approval=first.approval_id)

    assert outcome.decision is Decision.EXECUTED
    assert outcome.entity.status is EntityStatus.PAUSED


def test_an_approval_without_a_binding_cannot_be_redeemed(driver, media_buyer):
    """Fail closed on a legacy or hand-made approval row that predates the
    binding, rather than honouring it as a bearer token."""
    policy = FakePolicyStore(workspace_policy(effective_autonomy=1))
    pipeline = ToolPipeline(
        driver=driver, policy=policy, audit=FakeAuditSink(), locks=FakeLockManager()
    )

    approval_id = policy.create_approval(
        workspace_id=WORKSPACE, decision_id="d1", risk=RiskClass.MEDIUM,
        proposed={"tool": "create_campaign_draft"},   # no "authorisation" key
        impact_inr=None,
    )
    policy.grant(approval_id)

    outcome = create_campaign(pipeline, media_buyer, approval=approval_id)
    assert outcome.reason is DenialReason.APPROVAL_MISMATCH
    assert "no authorisation binding" in outcome.message


def test_the_fingerprint_ignores_incidental_parameters(driver, media_buyer):
    """Only fields an owner would consider part of what they approved are
    bound. A cosmetic name change must not invalidate the approval."""
    a = ToolRequest(
        tool=Tool.CREATE_AD_SET_DRAFT, workspace_id=WORKSPACE, ad_account_id=WRITABLE_ACCOUNT,
        params={"name": "first", "campaign_id": "c1", "daily_budget_inr": 5000.0,
                "optimisation_event": "LEAD"},
    )
    b = ToolRequest(
        tool=Tool.CREATE_AD_SET_DRAFT, workspace_id=WORKSPACE, ad_account_id=WRITABLE_ACCOUNT,
        params={"name": "second", "campaign_id": "c1", "daily_budget_inr": 5000.0,
                "optimisation_event": "LEAD"},
    )
    assert a.authorisation_fingerprint() == b.authorisation_fingerprint()

    c = ToolRequest(
        tool=Tool.CREATE_AD_SET_DRAFT, workspace_id=WORKSPACE, ad_account_id=WRITABLE_ACCOUNT,
        params={"name": "first", "campaign_id": "c1", "daily_budget_inr": 9000.0,
                "optimisation_event": "LEAD"},
    )
    assert a.authorisation_fingerprint() != c.authorisation_fingerprint()

def test_the_fixture_driver_reports_one_world_from_both_reads():
    """A test double that contradicts itself is worse than no double.

    get_entity applied later status and budget mutations; get_entities appended
    created entities in their original state and skipped budget overrides on
    snapshot ones. So the same ad set read ACTIVE one way and PAUSED the other,
    and anything reasoning over a campaign's children saw the world as it was
    at creation. That is not a simulation of Meta, it is a simulation of a
    filesystem.
    """
    driver = FixtureDriver(write_allowlist={WRITABLE_ACCOUNT})
    campaign = driver.create_campaign(
        WRITABLE_ACCOUNT, name="c", objective="OUTCOME_LEADS", idempotency_key="k1"
    ).entity
    ad_set = driver.create_ad_set(
        WRITABLE_ACCOUNT,
        campaign_id=campaign.id,
        name="a",
        daily_budget_inr=1000.0,
        optimisation_event="LEAD",
        idempotency_key="k2",
    ).entity

    driver.update_status(
        WRITABLE_ACCOUNT, ad_set.id, EntityStatus.ACTIVE, idempotency_key="k3"
    )
    driver.update_budget(
        WRITABLE_ACCOUNT, ad_set.id, daily_budget_inr=2500.0, idempotency_key="k4"
    )

    singular = driver.get_entity(WRITABLE_ACCOUNT, ad_set.id)
    plural = next(
        e
        for e in driver.get_entities(
            WRITABLE_ACCOUNT, EntityLevel.AD_SET, parent_id=campaign.id
        )
        if e.id == ad_set.id
    )

    assert singular.status is plural.status is EntityStatus.ACTIVE
    assert singular.fields["daily_budget_inr"] == plural.fields["daily_budget_inr"] == 2500.0

# ---------------------------------------------------------------------------
# The CAC ceiling, and the monthly basis
# ---------------------------------------------------------------------------


def _activate(pipeline, media_buyer, driver, policy_store, budget=500.0):
    """Draft a campaign and ad set, then try to activate it."""
    campaign = create_campaign(pipeline, media_buyer)
    ad_set = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.CREATE_AD_SET_DRAFT,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            params={
                "name": "ACQ|BROAD|IN-N|LEAD|01",
                "campaign_id": campaign.entity.id,
                "daily_budget_inr": budget,
                "optimisation_event": "LEAD",
            },
            decision_id="dec-cac-2",
        ),
    )
    assert ad_set.decision is Decision.EXECUTED
    return pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.ACTIVATE_ENTITY,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            target_entity_id=ad_set.entity.id,
            decision_id="dec-cac-3",
        ),
    )


def _pipeline_with(driver, **policy_overrides):
    policy = FakePolicyStore(workspace_policy(effective_autonomy=4, **policy_overrides))
    return ToolPipeline(
        driver=driver, policy=policy, audit=FakeAuditSink(), locks=FakeLockManager()
    )


def test_a_measured_cac_above_the_ceiling_refuses_more_spend(driver, media_buyer):
    """`cac_ceiling_inr` was loaded from the workspace on every request and
    compared against nothing at all — a guardrail that reads as protection and
    is not one, which is worse than an absent one.

    The rule it now enforces is PRD 10.3: do not scale into an account whose
    acquisition cost already exceeds what its own margin and RTO can carry.
    """
    pipeline = _pipeline_with(driver, cac_ceiling_inr=900.0, recent_cac_inr=1400.0)
    outcome = _activate(pipeline, media_buyer, driver, None)

    assert outcome.decision is Decision.DENIED
    assert outcome.reason is DenialReason.GUARDRAIL_BREACH
    breach = next(b for b in outcome.breaches if b.guardrail == "cac_ceiling")
    assert breach.observed == 1400.0
    assert breach.threshold == 900.0


def test_a_measured_cac_below_the_ceiling_permits_it(driver, media_buyer):
    """A guardrail that always fires teaches people to route around it.

    Asserted as "reached the approval gate", not "executed": activation is
    CRITICAL and needs an approval regardless of the ceiling. Getting that far
    is precisely what passing this guardrail means.
    """
    pipeline = _pipeline_with(driver, cac_ceiling_inr=900.0, recent_cac_inr=620.0)
    outcome = _activate(pipeline, media_buyer, driver, None)

    assert outcome.decision is Decision.AWAITING_APPROVAL
    assert not [b for b in outcome.breaches if b.guardrail == "cac_ceiling"]


def test_an_unmeasured_cac_does_not_fire_this_particular_guardrail(driver, media_buyer):
    """Deliberate, and worth stating because it looks like the permissive
    default this codebase keeps getting wrong.

    It is not. An account nobody has measured is already refused by the daily
    and monthly basis guards, which speak to exactly that absence. Firing a
    third refusal on the same missing data would report one problem as three.
    """
    pipeline = _pipeline_with(driver, cac_ceiling_inr=900.0, recent_cac_inr=None)
    outcome = _activate(pipeline, media_buyer, driver, None)
    assert not [b for b in outcome.breaches if b.guardrail == "cac_ceiling"]


def test_a_breached_ceiling_does_not_block_a_paused_draft(driver, media_buyer):
    """Scoped to actions that commit money. Restructuring an account that is
    over its ceiling is exactly what the owner should be free to do."""
    pipeline = _pipeline_with(driver, cac_ceiling_inr=900.0, recent_cac_inr=1400.0)
    campaign = create_campaign(pipeline, media_buyer)
    assert campaign.decision is Decision.EXECUTED


def test_an_unsynced_month_refuses_spend_even_when_the_day_looks_known(
    driver, media_buyer
):
    """The two basis flags mean different things and the monthly one was
    missing.

    `spend_basis_known` is satisfied by a completed connection check — we have
    enumerated the account. It says nothing about holding its spend history, so
    every workspace satisfied it while t_advit.metrics_daily was empty, and
    the monthly cap spent months comparing against a confident zero.
    """
    pipeline = _pipeline_with(
        driver, spend_basis_known=True, month_basis_known=False, monthly_cap_inr=150000.0
    )
    outcome = _activate(pipeline, media_buyer, driver, None)

    assert outcome.decision is Decision.DENIED
    breach = next(b for b in outcome.breaches if b.guardrail == "monthly_basis_unknown")
    assert "monthly cap" in breach.message
