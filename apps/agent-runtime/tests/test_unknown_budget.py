"""An unreported budget is not a budget of zero.

``_spend_delta`` scored ``target.fields.get("daily_budget_inr") or 0``, and
``_guardrails`` gates all four financial guards behind ``delta > 0``. So a zero
that meant "Meta did not tell us" skipped the daily cap, the monthly cap, the
spend-basis guard and the CAC ceiling **in one step** — on "turn the Diwali ad
set back on", which is the most ordinary operation an account has.

``Entity.fields`` is whatever the read returned minus id/name/status/parent_id,
and the captured snapshot's campaigns and ad sets carry only id and name. So
every entity this system did not itself create scored 0.0 — which is every
entity in a real connected account.

The ledger had this row marked PARTLY REFUTED on the grounds that an ad set's
budget is counted when the AD SET is activated. That holds only for ad sets
created in the same process. The compensating count does not exist for the
entities the finding actually named.

These tests run against the doubles, with an in-memory snapshot, so they need no
database and no network.
"""

from __future__ import annotations

import pytest

from app.meta.fixture import FixtureDriver
from app.policy.pipeline import (
    AgentIdentity,
    Decision,
    DenialReason,
    ToolPipeline,
    ToolRequest,
)
from app.policy.risk import Tool
from doubles import (
    MEDIA_BUYING_TOOLS,
    WORKSPACE,
    WRITABLE_ACCOUNT,
    FakeAuditSink,
    FakeLockManager,
    FakePolicyStore,
    FakeScheduler,
    workspace_policy,
)

ACCT = WRITABLE_ACCOUNT


def snapshot(campaigns, adsets):
    return {"campaigns": {ACCT: campaigns}, "adsets": {ACCT: adsets}, "ads": {ACCT: []}}


def build(snap, *, policy=None):
    driver = FixtureDriver(snapshot=snap, write_allowlist={ACCT})
    store = policy or FakePolicyStore()
    return (
        ToolPipeline(
            driver=driver,
            policy=store,
            audit=FakeAuditSink(),
            locks=FakeLockManager(),
            scheduler=FakeScheduler(),
        ),
        store,
    )


@pytest.fixture
def media_buyer() -> AgentIdentity:
    return AgentIdentity(name="media_buying", allowed_tools=MEDIA_BUYING_TOOLS)


def activate(pipeline, agent, entity_id):
    return pipeline.invoke(
        agent,
        ToolRequest(
            tool=Tool.ACTIVATE_ENTITY,
            workspace_id=WORKSPACE,
            ad_account_id=ACCT,
            target_entity_id=entity_id,
            decision_id="d-1",
        ),
    )


# ---------------------------------------------------------------------------
# The finding
# ---------------------------------------------------------------------------


def test_activating_an_ad_set_whose_budget_meta_did_not_report_is_refused(media_buyer):
    """The defect, at the level that is supposed to carry the number.

    Before: AWAITING_APPROVAL with no breaches and an approval card whose
    impact_inr was NULL — the approver asked to let money start moving and
    shown no rupee figure at all.
    """
    pipeline, _ = build(
        snapshot(
            [{"id": "c1", "name": "Diwali"}],
            [{"id": "s1", "name": "live", "status": "PAUSED", "parent_id": "c1"}],
        ),
        policy=FakePolicyStore(policy=workspace_policy(spend_today_inr=5000.0)),
    )

    out = activate(pipeline, media_buyer, "s1")

    assert out.decision is Decision.DENIED
    assert out.reason is DenialReason.GUARDRAIL_BREACH
    assert [b.guardrail for b in out.breaches] == ["spend_delta_unknown"]


def test_the_refusal_names_a_remedy_that_exists(media_buyer):
    """"Sync the account" was the obvious wording and is not a remedy here:
    nothing in this repo writes t_advit.ad_sets, and the fixture snapshot
    carries no budgets, so the refusal would be permanent rather than
    corrective. Setting the budget explicitly is an update_budget through this
    same pipeline, and that is what the message says."""
    pipeline, _ = build(
        snapshot(
            [{"id": "c1", "name": "Diwali"}],
            [{"id": "s1", "name": "live", "status": "PAUSED", "parent_id": "c1"}],
        )
    )
    out = activate(pipeline, media_buyer, "s1")

    assert "Set the budget explicitly" in out.message
    assert "Sync the account" not in out.message


def test_a_known_budget_still_activates(media_buyer):
    """The counterpart. A guard that refuses everything is not a guard."""
    pipeline, store = build(
        snapshot(
            [{"id": "c1", "name": "Diwali"}],
            [
                {
                    "id": "s1",
                    "name": "live",
                    "status": "PAUSED",
                    "parent_id": "c1",
                    "daily_budget_inr": 1500.0,
                }
            ],
        )
    )
    out = activate(pipeline, media_buyer, "s1")

    assert out.decision is Decision.AWAITING_APPROVAL
    assert store.created_approvals[-1]["impact_inr"] == 1500.0


def test_a_known_budget_over_the_cap_is_still_refused_by_the_cap(media_buyer):
    """The guard the unknown was skipping. If this stopped firing, the fix would
    have moved the hole rather than closed it."""
    pipeline, _ = build(
        snapshot(
            [{"id": "c1", "name": "Diwali"}],
            [
                {
                    "id": "s1",
                    "name": "live",
                    "status": "PAUSED",
                    "parent_id": "c1",
                    "daily_budget_inr": 4000.0,
                }
            ],
        ),
        policy=FakePolicyStore(policy=workspace_policy(spend_today_inr=4000.0)),
    )
    out = activate(pipeline, media_buyer, "s1")

    assert out.decision is Decision.DENIED
    assert "daily_spend_cap" in [b.guardrail for b in out.breaches]


# ---------------------------------------------------------------------------
# The campaign level, where absence means something different
# ---------------------------------------------------------------------------


def test_a_campaign_fronting_only_drafts_is_not_refused(media_buyer):
    """The trap the over-broad version of this fix walks into.

    A campaign has no budget of its own under ABO. That absence is a fact about
    the level, not a gap in what we know — and refusing on it would refuse every
    campaign activation, including the paused-first default the whole product is
    built on.
    """
    pipeline, store = build(
        snapshot(
            [{"id": "c1", "name": "Diwali"}],
            [{"id": "s1", "name": "draft", "status": "PAUSED", "parent_id": "c1"}],
        )
    )
    out = activate(pipeline, media_buyer, "c1")

    assert out.decision is Decision.AWAITING_APPROVAL
    assert store.created_approvals[-1]["impact_inr"] is None


def test_a_campaign_fronting_live_children_of_unknown_cost_is_refused(media_buyer):
    """The half the ledger's "FIXED" note left open.

    A blank impact card is what the legitimate case above looks like, so an
    approver cannot tell "this commits nothing" from "this commits an amount
    nobody can state". One of those must not be presented as the other.
    """
    pipeline, _ = build(
        snapshot(
            [{"id": "c1", "name": "Diwali"}],
            [{"id": "s1", "name": "live", "status": "ACTIVE", "parent_id": "c1"}],
        )
    )
    out = activate(pipeline, media_buyer, "c1")

    assert out.decision is Decision.DENIED
    assert [b.guardrail for b in out.breaches] == ["impact_unknown"]


def test_a_cbo_campaign_commits_its_own_budget(media_buyer):
    """Under CBO the campaign holds the budget. An earlier draft of this fix
    returned 0.0 for every non-ad-set level and would have discarded a number
    that is right there in the response — turning a firing cap into no breach."""
    pipeline, store = build(
        snapshot(
            [{"id": "c1", "name": "Diwali", "daily_budget_inr": 20000.0}],
            [{"id": "s1", "name": "draft", "status": "PAUSED", "parent_id": "c1"}],
        ),
        policy=FakePolicyStore(policy=workspace_policy(daily_cap_inr=5000.0)),
    )
    out = activate(pipeline, media_buyer, "c1")

    assert out.decision is Decision.DENIED
    assert "daily_spend_cap" in [b.guardrail for b in out.breaches]


# ---------------------------------------------------------------------------
# The step ceiling, which the same `or 0` was skipping
# ---------------------------------------------------------------------------


def test_a_step_ceiling_on_an_unknown_current_budget_is_reported(media_buyer):
    """`current = float(... or 0)` then `if current > 0` skipped the 20%
    per-step ceiling outright on any pre-existing ad set, so an unknown
    Rs 2,000 ad set could be raised to Rs 50,000 unchallenged.

    A breach rather than a refusal: the cap still fires here because the delta
    over-estimates in the safe direction, and refusing would block the one
    operation that can also supply the missing number.
    """
    pipeline, _ = build(
        snapshot(
            [{"id": "c1", "name": "Diwali"}],
            [{"id": "s1", "name": "live", "status": "ACTIVE", "parent_id": "c1"}],
        ),
        policy=FakePolicyStore(policy=workspace_policy(daily_cap_inr=100000.0)),
    )
    out = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.UPDATE_BUDGET,
            workspace_id=WORKSPACE,
            ad_account_id=ACCT,
            target_entity_id="s1",
            params={"daily_budget_inr": 50000.0},
            decision_id="d-1",
        ),
    )

    assert "budget_step_unknown" in [b.guardrail for b in out.breaches]


def test_a_known_step_ceiling_still_fires(media_buyer):
    pipeline, _ = build(
        snapshot(
            [{"id": "c1", "name": "Diwali"}],
            [
                {
                    "id": "s1",
                    "name": "live",
                    "status": "ACTIVE",
                    "parent_id": "c1",
                    "daily_budget_inr": 2000.0,
                }
            ],
        ),
        policy=FakePolicyStore(policy=workspace_policy(daily_cap_inr=100000.0)),
    )
    out = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.UPDATE_BUDGET,
            workspace_id=WORKSPACE,
            ad_account_id=ACCT,
            target_entity_id="s1",
            params={"daily_budget_inr": 50000.0},
            decision_id="d-1",
        ),
    )

    guardrails = [b.guardrail for b in out.breaches]
    assert "budget_step_ceiling" in guardrails
    assert "budget_step_unknown" not in guardrails


# ---------------------------------------------------------------------------
# Cost of the fix, pinned so it cannot creep
# ---------------------------------------------------------------------------


def test_the_campaign_child_budget_is_read_once_per_invocation(media_buyer):
    """`_guardrails` runs twice (step 5, and again at step 7 after approval) and
    neither call site sits inside an `except MetaError`. Reading the driver from
    in there turned one campaign activation into as many as five reads, and a
    Meta hiccup into a 500 instead of a DENIED.
    """
    snap = snapshot(
        [{"id": "c1", "name": "Diwali"}],
        [
            {
                "id": "s1",
                "name": "live",
                "status": "ACTIVE",
                "parent_id": "c1",
                "daily_budget_inr": 1000.0,
            }
        ],
    )
    driver = FixtureDriver(snapshot=snap, write_allowlist={ACCT})
    reads: list[str] = []
    original = driver.get_entities

    def counted(account_id, level, parent_id=None, **kw):
        reads.append(f"{level}:{parent_id}")
        return original(account_id, level, parent_id=parent_id, **kw)

    driver.get_entities = counted  # type: ignore[method-assign]
    pipeline = ToolPipeline(
        driver=driver,
        policy=FakePolicyStore(),
        audit=FakeAuditSink(),
        locks=FakeLockManager(),
        scheduler=FakeScheduler(),
    )

    activate(pipeline, media_buyer, "c1")

    child_reads = [r for r in reads if r.endswith(":c1")]
    assert len(child_reads) <= 2, (
        f"the campaign's children were read {len(child_reads)} times; the budget "
        "read belongs in invoke(), once, not inside _guardrails"
    )
