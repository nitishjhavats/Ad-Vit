"""The pipeline against the real schema.

Everything here exercises behaviour that only exists because the SaaS core and
the agent runtime meet. The unit suite proves the pipeline's logic with
doubles; this proves the adapters actually read the same authority the database
enforces - that ``effective_autonomy`` really is min(workspace, plan), that a
lapsed subscription really does close the write path, and that the constraints
guarding unapproved spend really do fire.

Skipped automatically when no local Postgres is reachable.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from app.meta.driver import EntityStatus
from app.meta.fixture import FixtureDriver
from app.policy.pipeline import (
    AgentIdentity,
    AlreadyExecuted,
    Decision,
    DenialReason,
    ToolOutcome,
    ToolPipeline,
    ToolRequest,
)
from app.policy.risk import RiskClass, Tool
from app.policy.store import (
    PostgresAuditSink,
    PostgresLockManager,
    PostgresOutcomeScheduler,
    PostgresPolicyStore,
    _advisory_key,
)
from doubles import MEDIA_BUYING_TOOLS

DSN = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
)

WORKSPACE = "00000000-0000-4000-8000-000000000050"
ORG = "00000000-0000-4000-8000-000000000010"
WRITABLE_ACCOUNT = "1000000000000003"
FUNDED_ACCOUNT = "1000000000000001"


def _reachable() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(), reason="local Supabase Postgres is not running"
)


@pytest.fixture
def db():
    with psycopg.connect(DSN, autocommit=True) as conn:
        yield conn


@pytest.fixture
def decision(db):
    """A pre-registered decision, with its expected effect and horizon written
    BEFORE any action refers to it (PRD 8.2)."""
    decision_id = str(uuid.uuid4())
    with db.cursor() as cur:
        cur.execute(
            """
            insert into t_advit.decisions
              (id, workspace_id, decision_type, situation_json, options_json,
               chosen_option, reasoning, expected_effect_json, horizon_days, confidence)
            values (%s, %s, 'launch_test', '{}'::jsonb, '[]'::jsonb,
                    'option_a', 'integration test',
                    %s::jsonb, 14, 0.6)
            """,
            (
                decision_id,
                WORKSPACE,
                '{"metric": "confirmed_orders", "direction": "up", "range": [5, 12]}',
            ),
        )
    yield decision_id
    with db.cursor() as cur:
        # Cascades to actions, approvals and outcomes. audit_log rows are
        # append-only by design and are deliberately left behind.
        cur.execute("delete from t_advit.decisions where id = %s", (decision_id,))
        cur.execute(
            "delete from t_advit.guardrail_events where workspace_id = %s", (WORKSPACE,)
        )


@pytest.fixture
def workspace_state(db):
    """Restores the seeded workspace and entitlements after each test."""
    with db.cursor() as cur:
        cur.execute(
            "select autonomy_level, is_paused, daily_cap_inr from t_advit.workspaces where id = %s",
            (WORKSPACE,),
        )
        before = cur.fetchone()
    yield
    with db.cursor() as cur:
        cur.execute(
            "update t_advit.workspaces set autonomy_level=%s, is_paused=%s, daily_cap_inr=%s "
            " where id = %s",
            (*before, WORKSPACE),
        )
        cur.execute("delete from t_advit.ad_sets where workspace_id = %s", (WORKSPACE,))
        cur.execute("delete from t_advit.campaigns where workspace_id = %s", (WORKSPACE,))
        cur.execute(
            "delete from core.entitlement_overrides where org_id = %s", (ORG,)
        )
        cur.execute(
            "update core.subscriptions set status = 'active' where org_id = %s", (ORG,)
        )


def build(driver: FixtureDriver | None = None) -> ToolPipeline:
    return ToolPipeline(
        driver=driver or FixtureDriver(write_allowlist={WRITABLE_ACCOUNT}),
        policy=PostgresPolicyStore(),
        audit=PostgresAuditSink(),
        locks=PostgresLockManager(),
        scheduler=PostgresOutcomeScheduler(),
    )


@pytest.fixture
def media_buyer() -> AgentIdentity:
    return AgentIdentity(name="media_buying", allowed_tools=MEDIA_BUYING_TOOLS)


def set_autonomy(db, level: int) -> None:
    with db.cursor() as cur:
        cur.execute(
            "update t_advit.workspaces set autonomy_level = %s where id = %s",
            (level, WORKSPACE),
        )


def cap_autonomy_entitlement(db, level: int) -> None:
    with db.cursor() as cur:
        cur.execute(
            """
            insert into core.entitlement_overrides
              (org_id, feature_key, value_json, reason)
            values (%s, 'max_autonomy_level', %s::jsonb, 'integration test')
            on conflict (org_id, feature_key) do update set value_json = excluded.value_json
            """,
            (ORG, str(level)),
        )


def set_subscription(db, status: str) -> None:
    with db.cursor() as cur:
        cur.execute(
            "update core.subscriptions set status = %s::core.subscription_status "
            " where org_id = %s",
            (status, ORG),
        )


def draft_campaign(pipeline, agent, decision_id, **kw):
    return pipeline.invoke(
        agent,
        ToolRequest(
            tool=Tool.CREATE_CAMPAIGN_DRAFT,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            params={"name": "PLC|ACQ|LEADS|AYUR|2609|01", "objective": "OUTCOME_LEADS"},
            decision_id=decision_id,
            **kw,
        ),
    )


# ---------------------------------------------------------------------------
# The plan cap and the autonomy ladder are one number
# ---------------------------------------------------------------------------


def test_plan_entitlement_caps_workspace_autonomy(db, workspace_state, media_buyer, decision):
    """A workspace set to L4 on a plan capped at L2 behaves as L2. Neither
    number alone is authoritative."""
    set_autonomy(db, 4)
    cap_autonomy_entitlement(db, 2)

    policy = PostgresPolicyStore().workspace_policy(WORKSPACE)
    assert policy.effective_autonomy == 2

    # MEDIUM is auto at L2, so a draft executes without an approval.
    assert draft_campaign(build(), media_buyer, decision).decision is Decision.EXECUTED


def test_workspace_intent_caps_a_generous_plan(db, workspace_state, media_buyer, decision):
    """The inverse: a generous plan does not raise a cautious workspace."""
    set_autonomy(db, 1)
    cap_autonomy_entitlement(db, 4)

    policy = PostgresPolicyStore().workspace_policy(WORKSPACE)
    assert policy.effective_autonomy == 1

    assert draft_campaign(build(), media_buyer, decision).decision is Decision.AWAITING_APPROVAL


def test_frozen_workspace_collapses_autonomy_to_zero(db, workspace_state):
    with db.cursor() as cur:
        cur.execute(
            "update t_advit.workspaces set autonomy_level = 4, is_paused = true where id = %s",
            (WORKSPACE,),
        )
    assert PostgresPolicyStore().workspace_policy(WORKSPACE).effective_autonomy == 0


# ---------------------------------------------------------------------------
# Subscription state closes the write path
# ---------------------------------------------------------------------------


def test_grace_period_reads_but_does_not_write(db, workspace_state, media_buyer, decision):
    """A lapsed payment must not strand live campaigns: the owner keeps the
    dashboard, loses the levers (PRD 18)."""
    set_autonomy(db, 4)
    set_subscription(db, "grace")

    policy = PostgresPolicyStore()
    assert policy.workspace_policy(WORKSPACE).access_mode == "read_only"
    # Autonomy collapses too, so nothing acts under a degraded subscription.
    assert policy.workspace_policy(WORKSPACE).effective_autonomy == 0

    pipeline = build()
    write = draft_campaign(pipeline, media_buyer, decision)
    assert write.reason is DenialReason.ACCESS_READ_ONLY

    read = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.READ_ENTITIES,
            workspace_id=WORKSPACE,
            ad_account_id=FUNDED_ACCOUNT,
            params={"level": "campaign"},
        ),
    )
    assert read.decision is Decision.EXECUTED
    assert len(read.data) == 4          # the four real Demo Brand campaigns


@pytest.mark.parametrize("status", ["expired", "suspended"])
def test_dead_subscription_denies_writes(db, workspace_state, media_buyer, decision, status):
    set_autonomy(db, 4)
    set_subscription(db, status)
    assert PostgresPolicyStore().workspace_policy(WORKSPACE).access_mode == "denied"
    assert draft_campaign(build(), media_buyer, decision).decision is Decision.DENIED


# ---------------------------------------------------------------------------
# Audit and action records
# ---------------------------------------------------------------------------


def test_action_row_is_written_before_the_driver_call(
    db, workspace_state, media_buyer, decision
):
    set_autonomy(db, 2)
    outcome = draft_campaign(build(), media_buyer, decision)
    assert outcome.decision is Decision.EXECUTED

    with db.cursor() as cur:
        cur.execute(
            """
            select action_type, risk_class::text, verified, idempotency_key,
                   rollback_handle, rollback_expires_at, executed_at,
                   after_state_json
              from t_advit.actions where decision_id = %s
            """,
            (decision,),
        )
        row = cur.fetchone()

    assert row is not None
    (
        action_type, risk_class, verified, idem_key,
        rollback_handle, rollback_expires, executed_at, after_state,
    ) = row

    assert action_type == "create_campaign_draft"
    assert risk_class == "medium"
    assert verified is True
    assert idem_key == outcome.idempotency_key
    assert rollback_handle["kind"] == "pause_created_entity"
    assert rollback_expires is not None, "rollback must carry a visible time limit"
    assert executed_at is not None
    assert after_state["status"] == "paused", "the persisted form is the database's vocabulary"


def test_audit_log_records_the_agent_as_the_actor(db, workspace_state, media_buyer, decision):
    """The AI is an actor with an identity, not an implicit superuser."""
    set_autonomy(db, 2)
    draft_campaign(build(), media_buyer, decision)

    with db.cursor() as cur:
        cur.execute(
            """
            select event, actor_type::text, scope::text, payload_json
              from core.audit_log
             where workspace_id = %s
               and event like 'action.%%'
             order by at desc limit 2
            """,
            (WORKSPACE,),
        )
        rows = cur.fetchall()

    assert rows, "the action must appear in the audit trail"
    assert all(r[1] == "agent" for r in rows)
    assert all(r[2] == "workspace" for r in rows)
    events = {r[0] for r in rows}
    assert "action.intent.create_campaign_draft" in events
    assert "action.executed" in events


def test_guardrail_breach_is_recorded_in_the_database(
    db, workspace_state, media_buyer, decision
):
    set_autonomy(db, 4)
    with db.cursor() as cur:
        cur.execute(
            "update t_advit.workspaces set daily_cap_inr = 100 where id = %s", (WORKSPACE,)
        )

    pipeline = build()
    campaign = draft_campaign(pipeline, media_buyer, decision)
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
            decision_id=decision,
        ),
    )
    activation = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.ACTIVATE_ENTITY,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            target_entity_id=ad_set.entity.id,
            decision_id=decision,
        ),
    )
    assert activation.reason is DenialReason.GUARDRAIL_BREACH

    with db.cursor() as cur:
        cur.execute(
            """
            select guardrail, guardrail_class, threshold, observed, action_taken
              from t_advit.guardrail_events
             where workspace_id = %s and guardrail = 'daily_spend_cap'
            """,
            (WORKSPACE,),
        )
        row = cur.fetchone()

    assert row is not None
    assert row[1] == "financial"
    assert float(row[2]) == 100.0
    assert row[4] == "denied"


# ---------------------------------------------------------------------------
# Constraints the database enforces regardless of application code
# ---------------------------------------------------------------------------


def test_database_refuses_a_critical_action_without_an_approval(db, decision):
    """t_advit.actions carries a check constraint, so an unapproved
    critical-class action cannot be recorded at all - not merely refused by
    application logic."""
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.cursor() as cur:
            cur.execute(
                """
                insert into t_advit.actions
                  (decision_id, workspace_id, action_type, risk_class, idempotency_key)
                values (%s, %s, 'activate_entity', 'critical', %s)
                """,
                (decision, WORKSPACE, f"idem-{uuid.uuid4()}"),
            )


def test_idempotency_key_is_unique_at_the_database_level(db, decision):
    key = f"idem-{uuid.uuid4()}"
    with db.cursor() as cur:
        cur.execute(
            """
            insert into t_advit.actions
              (decision_id, workspace_id, action_type, risk_class, idempotency_key)
            values (%s, %s, 'pause_entity', 'high', %s)
            """,
            (decision, WORKSPACE, key),
        )
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.cursor() as cur:
            cur.execute(
                """
                insert into t_advit.actions
                  (decision_id, workspace_id, action_type, risk_class, idempotency_key)
                values (%s, %s, 'pause_entity', 'high', %s)
                """,
                (decision, WORKSPACE, key),
            )


def test_pre_call_audit_requires_a_decision(media_buyer):
    """Every material action must be explainable from its evidence, so an
    action with no decision is refused rather than recorded anonymously."""
    sink = PostgresAuditSink()
    with pytest.raises(ValueError, match="decision_id"):
        sink.pre(
            workspace_id=WORKSPACE,
            agent="media_buying",
            tool=Tool.PAUSE_ENTITY,
            risk=RiskClass.HIGH,
            idempotency_key=f"idem-{uuid.uuid4()}",
            params={},
            decision_id=None,
            approval_id=None,
            policy_decision_id=str(uuid.uuid4()),
        )


# ---------------------------------------------------------------------------
# Approvals and the outcome obligation
# ---------------------------------------------------------------------------


def test_approval_is_persisted_with_an_expiry(db, workspace_state, media_buyer, decision):
    set_autonomy(db, 1)
    outcome = draft_campaign(build(), media_buyer, decision)
    assert outcome.decision is Decision.AWAITING_APPROVAL

    with db.cursor() as cur:
        cur.execute(
            """
            select status::text, risk_class::text, expires_at > now() as live, proposed_json
              from t_advit.approvals where id = %s
            """,
            (outcome.approval_id,),
        )
        status, risk_class, live, proposed = cur.fetchone()

    assert status == "pending"
    assert risk_class == "medium"
    assert live is True, "a proposal must expire, so it needs a future expiry"
    assert proposed["tool"] == "create_campaign_draft"


def test_activation_queues_the_outcome_check(db, workspace_state, media_buyer, decision):
    set_autonomy(db, 4)
    pipeline = build()

    campaign = draft_campaign(pipeline, media_buyer, decision)
    ad_set = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.CREATE_AD_SET_DRAFT,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            params={
                "name": "as",
                "campaign_id": campaign.entity.id,
                "daily_budget_inr": 500.0,
                "optimisation_event": "LEAD",
            },
            decision_id=decision,
        ),
    )
    first = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.ACTIVATE_ENTITY,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            target_entity_id=ad_set.entity.id,
            decision_id=decision,
            horizon_days=14,
        ),
    )
    with db.cursor() as cur:
        cur.execute(
            "update t_advit.approvals set status='approved', responded_at=now() where id=%s",
            (first.approval_id,),
        )

    outcome = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.ACTIVATE_ENTITY,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            target_entity_id=ad_set.entity.id,
            decision_id=decision,
            approval_id=first.approval_id,
            horizon_days=14,
        ),
    )
    assert outcome.decision is Decision.EXECUTED
    assert outcome.outcome_check_queued is True

    with db.cursor() as cur:
        cur.execute(
            "select horizon_days, verdict::text from t_advit.outcomes where decision_id = %s",
            (decision,),
        )
        row = cur.fetchone()

    assert row == (14, "unmeasurable"), "the obligation to measure must outlive the run"


def _draft_ad_set(pipeline, agent, decision_id, campaign_id, budget, n):
    return pipeline.invoke(
        agent,
        ToolRequest(
            tool=Tool.CREATE_AD_SET_DRAFT,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            params={
                "name": f"ACQ|BROAD|IN-N|LEAD|{n:02d}",
                "campaign_id": campaign_id,
                "daily_budget_inr": budget,
                "optimisation_event": "LEAD",
            },
            decision_id=decision_id,
        ),
    )


def _activate_with_approval(pipeline, agent, db, decision_id, entity_id):
    """Activation is CRITICAL, so it raises an approval, gets granted one, and
    is retried against it - the same two-step the product uses."""
    first = pipeline.invoke(
        agent,
        ToolRequest(
            tool=Tool.ACTIVATE_ENTITY,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            target_entity_id=entity_id,
            decision_id=decision_id,
            horizon_days=14,
        ),
    )
    if first.approval_id is None:
        return first
    with db.cursor() as cur:
        cur.execute(
            "update t_advit.approvals set status='approved', responded_at=now() where id=%s",
            (first.approval_id,),
        )
    return pipeline.invoke(
        agent,
        ToolRequest(
            tool=Tool.ACTIVATE_ENTITY,
            workspace_id=WORKSPACE,
            ad_account_id=WRITABLE_ACCOUNT,
            target_entity_id=entity_id,
            decision_id=decision_id,
            approval_id=first.approval_id,
            horizon_days=14,
        ),
    )


def test_the_daily_cap_accumulates_across_sequential_activations(
    db, workspace_state, media_buyer, decision
):
    """Regression: the caps caught one oversized action but never accumulated.

    `spend_today_inr` was summed from `t_advit.ad_sets`, which only changes
    when a Meta sync runs. Between syncs it read stale - so each activation was
    projected against the same figure and eleven small ones all passed. That is
    exactly the repeated-small-step scale-up PRD D4 exists to prevent: nobody
    approves a 4x increase, they approve eleven 15% ones.

    Three activations of Rs 2,000 against a Rs 5,000 cap. The third must be
    refused, using nothing but this system's own record of what it did.
    """
    set_autonomy(db, 4)
    pipeline = build()
    campaign = draft_campaign(pipeline, media_buyer, decision)

    ad_sets = [
        _draft_ad_set(pipeline, media_buyer, decision, campaign.entity.id, 2000.0, n)
        for n in range(1, 4)
    ]
    assert all(a.decision is Decision.EXECUTED for a in ad_sets), "drafts commit nothing"

    first = _activate_with_approval(
        pipeline, media_buyer, db, decision, ad_sets[0].entity.id
    )
    assert first.decision is Decision.EXECUTED, "Rs 2,000 of a Rs 5,000 cap"

    second = _activate_with_approval(
        pipeline, media_buyer, db, decision, ad_sets[1].entity.id
    )
    assert second.decision is Decision.EXECUTED, "Rs 4,000 of a Rs 5,000 cap"

    third = _activate_with_approval(
        pipeline, media_buyer, db, decision, ad_sets[2].entity.id
    )
    assert third.decision is Decision.DENIED, (
        "Rs 6,000 against a Rs 5,000 cap: the two already-live activations must "
        "count even though no Meta sync has run since"
    )
    assert third.reason is DenialReason.GUARDRAIL_BREACH
    breach = next(b for b in third.breaches if b.guardrail == "daily_spend_cap")
    assert breach.observed == 6000.0
    assert breach.threshold == 5000.0


def test_a_rolled_back_activation_releases_its_committed_budget(
    db, workspace_state, media_buyer, decision
):
    """Committed spend is what is live now, not what was ever attempted. An
    activation that was undone must stop counting against the cap, or a single
    mistake would lock the account out for the rest of the day."""
    set_autonomy(db, 4)
    pipeline = build()
    campaign = draft_campaign(pipeline, media_buyer, decision)

    a = _draft_ad_set(pipeline, media_buyer, decision, campaign.entity.id, 4500.0, 1)
    activated = _activate_with_approval(pipeline, media_buyer, db, decision, a.entity.id)
    assert activated.decision is Decision.EXECUTED

    policy = PostgresPolicyStore()
    assert policy.workspace_policy(WORKSPACE).spend_today_inr == 4500.0

    with db.cursor() as cur:
        cur.execute(
            "update t_advit.actions set rolled_back_at = now() "
            " where workspace_id = %s and action_type = 'activate_entity'",
            (WORKSPACE,),
        )

    assert policy.workspace_policy(WORKSPACE).spend_today_inr == 0.0


def test_an_unverified_action_does_not_commit_budget(
    db, workspace_state, media_buyer, decision
):
    """A write we could not verify may or may not have landed. It must not
    consume cap headroom on the strength of an assumption - the same reason the
    pipeline refuses to report it as success."""
    set_autonomy(db, 4)
    pipeline = build()
    campaign = draft_campaign(pipeline, media_buyer, decision)
    a = _draft_ad_set(pipeline, media_buyer, decision, campaign.entity.id, 3000.0, 1)
    _activate_with_approval(pipeline, media_buyer, db, decision, a.entity.id)

    with db.cursor() as cur:
        cur.execute(
            "update t_advit.actions set verified = false where workspace_id = %s",
            (WORKSPACE,),
        )

    assert PostgresPolicyStore().workspace_policy(WORKSPACE).spend_today_inr == 0.0


def test_every_entity_status_matches_a_database_label(db):
    """The two vocabularies must agree, and the database is the authority.

    EntityStatus mirrors Meta's wire format (upper case); t_advit.entity_status
    is lower. They meet whenever a status is serialised into JSON that SQL later
    reads, and they met silently once already: the committed-spend CTE compared
    after_state_json->>'status' against 'active', matched nothing, returned zero,
    and left the daily cap unable to accumulate. No error - just a wrong number
    inside a guardrail.

    Asserting the mapping against the live enum means adding a status on either
    side without the other fails here rather than in a cap check months later.
    """
    with db.cursor() as cur:
        cur.execute(
            """
            select e.enumlabel
              from pg_type t
              join pg_enum e on e.enumtypid = t.oid
              join pg_namespace n on n.oid = t.typnamespace
             where n.nspname = 't_advit' and t.typname = 'entity_status'
            """
        )
        labels = {r[0] for r in cur.fetchall()}

    assert labels, "t_advit.entity_status not found"
    missing = {s.db for s in EntityStatus} - labels
    assert not missing, (
        f"EntityStatus values with no t_advit.entity_status label: {sorted(missing)}. "
        "A status that does not exist in the database silently matches nothing in SQL."
    )


def test_a_persisted_action_is_readable_by_the_databases_own_vocabulary(db, workspace_state, media_buyer, decision):
    """The end-to-end form of the above: what the pipeline writes must be
    findable by a query written in the database's terms, with no lower() and no
    translation layer in between."""
    set_autonomy(db, 4)
    pipeline = build()
    campaign = draft_campaign(pipeline, media_buyer, decision)
    ad_set = _draft_ad_set(pipeline, media_buyer, decision, campaign.entity.id, 1500.0, 1)
    _activate_with_approval(pipeline, media_buyer, db, decision, ad_set.entity.id)

    with db.cursor() as cur:
        cur.execute(
            """
            select count(*)
              from t_advit.actions
             where workspace_id = %s
               and action_type = 'activate_entity'
               -- deliberately no lower(): the written form must already match
               and after_state_json->>'status' = 'active'
            """,
            (WORKSPACE,),
        )
        assert cur.fetchone()[0] == 1


def _impact_of(db, approval_id):
    with db.cursor() as cur:
        cur.execute("select impact_inr from t_advit.approvals where id = %s", (approval_id,))
        row = cur.fetchone()
    return None if row is None or row[0] is None else float(row[0])


def test_activating_a_campaign_states_what_it_sets_moving(
    db, workspace_state, media_buyer, decision
):
    """The approval card used to say a campaign activation had no rupee impact.

    _impact was `_spend_delta or None`, and _spend_delta correctly returns zero
    for a campaign - each ad set was already counted against the cap when THAT
    ad set was activated, so counting them again would double-count. But the
    approver is not being asked about cap arithmetic. They are being asked
    whether to let money start moving, and a campaign standing in front of a
    live ad set moves Rs 4,000 a day the moment it goes live.

    Impact and committed spend are different questions. Conflating them made
    the card lie in the one direction that matters.
    """
    set_autonomy(db, 4)
    pipeline = build()
    campaign = draft_campaign(pipeline, media_buyer, decision)
    ad_set = _draft_ad_set(pipeline, media_buyer, decision, campaign.entity.id, 4000.0, 1)
    assert _activate_with_approval(
        pipeline, media_buyer, db, decision, ad_set.entity.id
    ).decision is Decision.EXECUTED

    pending = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.ACTIVATE_ENTITY, workspace_id=WORKSPACE, ad_account_id=WRITABLE_ACCOUNT,
            target_entity_id=campaign.entity.id, decision_id=decision, horizon_days=14,
        ),
    )
    assert pending.decision is Decision.AWAITING_APPROVAL
    assert _impact_of(db, pending.approval_id) == 4000.0


def test_a_campaign_fronting_only_drafts_states_no_impact(
    db, workspace_state, media_buyer, decision
):
    """The other half, and the reason this is not just "sum the children":
    paused children commit nothing. A campaign in front of nothing but drafts
    genuinely sets no money moving, and inflating that number would train
    approvers to ignore it."""
    set_autonomy(db, 4)
    pipeline = build()
    campaign = draft_campaign(pipeline, media_buyer, decision)
    _draft_ad_set(pipeline, media_buyer, decision, campaign.entity.id, 9000.0, 1)

    pending = pipeline.invoke(
        media_buyer,
        ToolRequest(
            tool=Tool.ACTIVATE_ENTITY, workspace_id=WORKSPACE, ad_account_id=WRITABLE_ACCOUNT,
            target_entity_id=campaign.entity.id, decision_id=decision, horizon_days=14,
        ),
    )
    assert pending.decision is Decision.AWAITING_APPROVAL
    assert _impact_of(db, pending.approval_id) is None


def test_activating_a_campaign_does_not_double_count_against_the_cap(
    db, workspace_state, media_buyer, decision
):
    """The half of the audit finding that does NOT hold, kept as a test so it
    is not "fixed" later by someone reading the finding and not the code.

    The finding claimed campaign activation bypasses the spend caps. It does
    not: the ad set's budget was counted when the ad set was activated, and
    adding it again on campaign activation would charge the same rupees twice
    and lock the account out of its own cap.
    """
    set_autonomy(db, 4)
    pipeline = build()
    policy = PostgresPolicyStore()
    campaign = draft_campaign(pipeline, media_buyer, decision)
    ad_set = _draft_ad_set(pipeline, media_buyer, decision, campaign.entity.id, 4000.0, 1)

    _activate_with_approval(pipeline, media_buyer, db, decision, ad_set.entity.id)
    after_ad_set = policy.workspace_policy(WORKSPACE).spend_today_inr
    assert after_ad_set == 4000.0

    _activate_with_approval(pipeline, media_buyer, db, decision, campaign.entity.id)
    assert policy.workspace_policy(WORKSPACE).spend_today_inr == 4000.0, (
        "the campaign fronts spend already committed by its ad set; counting it "
        "twice would exhaust a cap the account has not actually reached"
    )


def test_a_failed_retry_does_not_erase_a_successful_execution(
    db, workspace_state, media_buyer, decision
):
    """post() wrote `case when <ok> then now() end` with no ELSE, and a CASE
    with no ELSE yields NULL. So a post() whose outcome was not ok cleared
    executed_at and rollback_expires_at - including on a row that had already
    executed.

    pre() upserts on idempotency_key and hands back the SAME row for a retry, so
    a retry that fails after a first attempt succeeded erased the record of the
    success. The change stays live on Meta while the trail says it never ran and
    offers no way back, which is the one claim this product must never make.

    after_state_json mattered twice over: workspace_policy reads it to compute
    committed spend, so nulling it released a successful activation's budget
    back to the daily cap - the cap that exists to stop that money being
    committed twice.
    """
    set_autonomy(db, 4)
    pipeline = build()
    policy = PostgresPolicyStore()
    campaign = draft_campaign(pipeline, media_buyer, decision)
    ad_set = _draft_ad_set(pipeline, media_buyer, decision, campaign.entity.id, 3000.0, 1)
    executed = _activate_with_approval(pipeline, media_buyer, db, decision, ad_set.entity.id)
    assert executed.decision is Decision.EXECUTED
    assert policy.workspace_policy(WORKSPACE).spend_today_inr == 3000.0

    with db.cursor() as cur:
        cur.execute(
            "select id::text, executed_at, rollback_expires_at from t_advit.actions "
            " where workspace_id = %s and action_type = 'activate_entity'",
            (WORKSPACE,),
        )
        action_id, executed_at, rollback_expires_at = cur.fetchone()
    assert executed_at is not None and rollback_expires_at is not None

    # The retry: same audit row, an outcome that carries nothing.
    PostgresAuditSink().post(
        audit_id=action_id,
        outcome=ToolOutcome(
            decision=Decision.DENIED,
            tool=Tool.ACTIVATE_ENTITY,
            risk=RiskClass.CRITICAL,
            reason=DenialReason.GUARDRAIL_BREACH,
            message="retry refused",
        ),
    )

    with db.cursor() as cur:
        cur.execute(
            "select executed_at, rollback_expires_at, rollback_handle, after_state_json "
            "  from t_advit.actions where id = %s",
            (action_id,),
        )
        after = cur.fetchone()

    assert after[0] == executed_at, "an execution that happened is a fact about the past"
    assert after[1] == rollback_expires_at, "the way back must not vanish"
    assert after[2] is not None, "nor the handle that makes it possible"
    assert after[3] is not None
    assert policy.workspace_policy(WORKSPACE).spend_today_inr == 3000.0, (
        "the committed budget must not be released by a failed retry"
    )


# ---------------------------------------------------------------------------
# The real advisory lock
# ---------------------------------------------------------------------------


def test_advisory_lock_excludes_a_second_holder(db):
    locks = PostgresLockManager()
    with locks.acquire(WRITABLE_ACCOUNT, timeout_s=1) as first:
        assert first is True
        with locks.acquire(WRITABLE_ACCOUNT, timeout_s=1) as second:
            assert second is False, "a second holder must be refused, not admitted"

    # Released on exit, so the next caller succeeds.
    with locks.acquire(WRITABLE_ACCOUNT, timeout_s=1) as third:
        assert third is True


def test_different_accounts_do_not_block_each_other(db):
    locks = PostgresLockManager()
    assert _advisory_key(WRITABLE_ACCOUNT) != _advisory_key(FUNDED_ACCOUNT)
    with locks.acquire(WRITABLE_ACCOUNT, timeout_s=1) as a:
        with locks.acquire(FUNDED_ACCOUNT, timeout_s=1) as b:
            assert a is True and b is True


# ---------------------------------------------------------------------------
# Idempotency: the uniqueness constraint starts constraining
#
# Migration 7 introduced the unique index on idempotency_key with the sentence
# "idempotency is a uniqueness constraint, not a convention". It was a
# convention: pre() resolved the collision with `do update ... returning id` and
# never looked at executed_at, so a replayed request was handed the FIRST
# execution's row and the pipeline went on to issue the mutation again.
# ---------------------------------------------------------------------------


def _executed_outcome(**kw):
    return ToolOutcome(
        decision=Decision.EXECUTED,
        tool=Tool.UPDATE_BUDGET,
        risk=RiskClass.HIGH,
        after_state={"id": "123", "daily_budget_inr": 2000.0, "status": "ACTIVE"},
        verified=True,
        **kw,
    )


def _pre(sink, key, decision_id, params):
    return sink.pre(
        workspace_id=WORKSPACE,
        agent="media_buying",
        tool=Tool.UPDATE_BUDGET,
        risk=RiskClass.HIGH,
        idempotency_key=key,
        params=params,
        decision_id=decision_id,
        approval_id=None,
        policy_decision_id=str(uuid.uuid4()),
    )


def test_a_replayed_request_is_refused_rather_than_executed_twice(db, decision):
    """The finding. Two runs, one idempotency key, and the second used to be
    handed the first's action row and allowed to spend again."""
    sink = PostgresAuditSink()
    key = f"idem-test-{uuid.uuid4()}"

    first = _pre(sink, key, decision, {"daily_budget_inr": 2000.0})
    sink.post(audit_id=first, outcome=_executed_outcome())

    with pytest.raises(AlreadyExecuted) as exc:
        _pre(sink, key, decision, {"daily_budget_inr": 2000.0})

    assert exc.value.action_id == first, "the refusal must name the action that ran"
    assert exc.value.executed_at is not None
    assert "already executed" in str(exc.value)


def test_the_replay_does_not_overwrite_the_first_executions_record(db, decision):
    """The second harm, and the quieter one. `do update set meta_request_json =
    excluded.meta_request_json` rewrote the original request payload on the way
    past, so the trail described the second attempt's parameters while carrying
    the first attempt's outcome."""
    sink = PostgresAuditSink()
    key = f"idem-test-{uuid.uuid4()}"

    first = _pre(sink, key, decision, {"daily_budget_inr": 2000.0})
    sink.post(audit_id=first, outcome=_executed_outcome())

    with pytest.raises(AlreadyExecuted):
        _pre(sink, key, decision, {"daily_budget_inr": 9999.0})

    with db.cursor() as cur:
        cur.execute(
            "select meta_request_json from t_advit.actions where id = %s", (first,)
        )
        stored = cur.fetchone()[0]
    assert stored["daily_budget_inr"] == 2000.0, (
        "the replay rewrote the executed action's recorded request"
    )


def test_an_attempt_that_never_reached_the_driver_is_still_retryable(db, decision):
    """The other half, and the reason the predicate is `executed_at is null`
    rather than `not exists`.

    A process that died between pre() and the driver call leaves an unexecuted
    row. That is exactly what an idempotency key is for, and rebinding it must
    keep working - otherwise the fix turns every crash into a permanently
    poisoned key.
    """
    sink = PostgresAuditSink()
    key = f"idem-test-{uuid.uuid4()}"

    first = _pre(sink, key, decision, {"daily_budget_inr": 2000.0})
    again = _pre(sink, key, decision, {"daily_budget_inr": 2500.0})

    assert again == first, "a retry must land on the same action row"
    with db.cursor() as cur:
        cur.execute(
            "select meta_request_json, executed_at from t_advit.actions where id = %s",
            (first,),
        )
        stored, executed_at = cur.fetchone()
    assert executed_at is None
    assert stored["daily_budget_inr"] == 2500.0, "the retry's parameters should bind"


def test_a_failed_attempt_can_be_retried_but_a_succeeded_one_cannot(db, decision):
    """The boundary stated as one test, because the two halves are easy to
    conflate: what closes the key is EXECUTION, not the existence of a row and
    not a previous denial."""
    sink = PostgresAuditSink()
    key = f"idem-test-{uuid.uuid4()}"

    first = _pre(sink, key, decision, {"daily_budget_inr": 2000.0})
    sink.post(
        audit_id=first,
        outcome=ToolOutcome(
            decision=Decision.DENIED,
            tool=Tool.UPDATE_BUDGET,
            risk=RiskClass.HIGH,
            reason=DenialReason.DRIVER_ERROR,
            message="meta timed out",
        ),
    )

    assert _pre(sink, key, decision, {"daily_budget_inr": 2000.0}) == first

    sink.post(audit_id=first, outcome=_executed_outcome())
    with pytest.raises(AlreadyExecuted):
        _pre(sink, key, decision, {"daily_budget_inr": 2000.0})
