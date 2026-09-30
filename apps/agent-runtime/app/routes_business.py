"""Business-truth, economics and CTA routes.

Kept apart from the Meta-facing surface because none of this needs a pixel, a
dataset, or any Meta signal at all. The owner is the source. An account with
broken tracking - which is the state of the connected accounts today - can
still run the closed loop on this path, and that is the whole thesis: business
truth beats platform truth.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.agents.business_truth import (
    BusinessTruth,
    TrailingStats,
    UnitEconomics,
    compute_economics,
    intake,
    scaling_verdict,
)
from app.agents.cta_model import AccountProfile, recommend_cta
from app.orchestrator import held
from app.orchestrator.cta_gate import ACCEPTED, CTA_DIMENSION, CTA_KEY
from app.auth.scope import (
    AuthorizedWorkspace,
    Capability,
    Principal,
    authorized_workspace,
    current_principal,
)
from app.db.pools import service_conn

router = APIRouter()


def _issues(result) -> list[dict[str, Any]]:
    return [
        {
            "severity": i.severity.value,
            "field": i.field,
            "message": i.message,
            "question": i.question,
        }
        for i in result.issues
    ]


# ---------------------------------------------------------------------------
# Daily business truth (PRD 12.1)
# ---------------------------------------------------------------------------


class DailyTruthRequest(BaseModel):
    # `workspace_id` is gone from the body. The tenant a request acts on is a
    # path parameter and nothing else - a field that exists will eventually be
    # read, and the whole point of this change is that the caller stops naming
    # the tenant anywhere the resolver does not see it.
    date: str
    # Either a structured card or a natural-language message - whichever is
    # lowest-friction for that owner on a phone at 20:30.
    message: str | None = None
    values: dict[str, Any] | None = None
    sales_feedback: str | None = None
    business_issues: str | None = None

    # `entered_by` is gone from the body. It is a uuid column naming the person
    # who reported the day's numbers - the ones the economics engine turns into
    # a scaling verdict - and a caller-supplied value there records whoever the
    # client typed. It now comes from auth.uid() in the statement itself.


@router.post("/api/workspaces/{workspace_id}/daily-truth")
def submit_daily_truth(
    payload: DailyTruthRequest,
    ws: Annotated[AuthorizedWorkspace, Depends(authorized_workspace)],
) -> dict[str, Any]:
    if payload.message is None and payload.values is None:
        raise HTTPException(status_code=422, detail="supply either message or values")

    ws.principal.require(Capability.SUBMIT_BUSINESS_TRUTH)

    with ws.principal.tx() as cur:
        cur.execute(
            """
            select count(*)::int as days,
                   avg(total_orders)::float as mean_total,
                   avg(confirmed_orders::float / nullif(total_orders, 0)) as mean_confirm
              from t_advit.business_truth
             where workspace_id = %s
               and date >= %s::date - 30
               and date <  %s::date
            """,
            (ws.id, payload.date, payload.date),
        )
        row = cur.fetchone()

    trailing = TrailingStats(
        days=row["days"],
        mean_total_orders=row["mean_total"],
        mean_confirm_rate=row["mean_confirm"],
    )

    source = payload.message if payload.message is not None else payload.values
    result = intake(source, trailing)

    if not result.accepted:
        return {
            "accepted": False,
            "parsed_from": result.parsed_from,
            "parsed": result.truth.as_dict(),
            "issues": _issues(result),
            "questions": result.questions,
        }

    t = result.truth
    # The insert is the tenant's own write, governed by business_truth's policy.
    with ws.principal.tx() as cur:
        cur.execute(
            """
            insert into t_advit.business_truth
              (date, workspace_id, total_orders, confirmed_orders, cancelled_orders,
               rto_orders, delivered_orders, revenue_inr, delivered_revenue_inr,
               leads_received, leads_contacted, avg_response_min,
               sales_feedback, business_issues, entered_by)
            values (%s::date, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    auth.uid())
            -- coalesce, not excluded, on every field.
            --
            -- A day is reported in more than one message by design: the owner
            -- sends orders and revenue at 20:30, and delivered orders arrive
            -- days later against that same back-dated date (PRD 12.1). Writing
            -- `excluded.x` meant the second message overwrote every field it did
            -- not carry with NULL - so a delivery update that knew only
            -- delivered_orders destroyed the evening's 42 orders, 28 confirmed,
            -- Rs 61,000 and the sales team's notes.
            --
            -- Worse than losing them: NULL is this schema's marker for "not
            -- reported", so the day then looked like a gap the owner had never
            -- filled in, and every figure derived from it - CAC, contribution
            -- margin, the scaling verdict - silently lost its denominator.
            --
            -- The trade-off, stated rather than stumbled into: a field cannot be
            -- cleared back to unknown through this route. Correcting a number is
            -- sending a different one; un-reporting something is not a thing the
            -- daily intake needs to do, and doing it silently is exactly what
            -- caused this.
            on conflict (workspace_id, date) do update set
              total_orders          = coalesce(excluded.total_orders,          t_advit.business_truth.total_orders),
              confirmed_orders      = coalesce(excluded.confirmed_orders,      t_advit.business_truth.confirmed_orders),
              cancelled_orders      = coalesce(excluded.cancelled_orders,      t_advit.business_truth.cancelled_orders),
              rto_orders            = coalesce(excluded.rto_orders,            t_advit.business_truth.rto_orders),
              delivered_orders      = coalesce(excluded.delivered_orders,      t_advit.business_truth.delivered_orders),
              revenue_inr           = coalesce(excluded.revenue_inr,           t_advit.business_truth.revenue_inr),
              delivered_revenue_inr = coalesce(excluded.delivered_revenue_inr, t_advit.business_truth.delivered_revenue_inr),
              leads_received        = coalesce(excluded.leads_received,        t_advit.business_truth.leads_received),
              leads_contacted       = coalesce(excluded.leads_contacted,       t_advit.business_truth.leads_contacted),
              avg_response_min      = coalesce(excluded.avg_response_min,      t_advit.business_truth.avg_response_min),
              sales_feedback        = coalesce(excluded.sales_feedback,        t_advit.business_truth.sales_feedback),
              business_issues       = coalesce(excluded.business_issues,       t_advit.business_truth.business_issues),
              entered_at            = now(),
              -- The correction names whoever made it, which is the question
              -- "who changed this number" actually asks.
              entered_by            = auth.uid()
            """,
            (
                payload.date,
                ws.id,
                t.total_orders,
                t.confirmed_orders,
                t.cancelled_orders,
                t.rto_orders,
                t.delivered_orders,
                t.revenue_inr,
                t.delivered_revenue_inr,
                t.leads_received,
                t.leads_contacted,
                t.avg_response_min,
                payload.sales_feedback or t.sales_feedback,
                payload.business_issues or t.business_issues,
            ),
        )
        cur.execute(
            """
            select core.log_audit('workspace', 'business_truth.received',
                                  -- p_org is required for a workspace-scoped
                                  -- row (hint audit_org_required). On the
                                  -- superuser connection this call took the
                                  -- backend branch and never reached the
                                  -- check, so every daily-truth submission
                                  -- would have 500'd on the first real tenant
                                  -- request.
                                  p_org        => %s::uuid,
                                  p_workspace  => %s::uuid,
                                  p_actor_type => 'user',
                                  p_actor      => auth.uid(),
                                  p_payload    => %s::jsonb)
            """,
            (
                ws.org_id,
                ws.id,
                json.dumps({"date": payload.date, "parsed_from": result.parsed_from}),
            ),
        )

    # Recompute on the SERVICE connection, and only after the tenant insert has
    # committed.
    #
    # t_advit.compute_blended_daily is revoked from `authenticated` by
    # 20260910000003 for a reason worth restating: it does not merely read, it
    # WRITES computed economics into blended_daily, which is the table the
    # scaling verdict reads. A tenant that could call it directly could
    # overwrite the numbers deciding whether their account may scale.
    with service_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "select t_advit.compute_blended_daily(%s::uuid, %s::date)",
            (ws.id, payload.date),
        )
        conn.commit()

    with ws.principal.tx() as cur:
        cur.execute(
            """
            select confirm_rate, rto_rate, blended_cac_inr,
                   contribution_margin_inr, delivered_aov_inr, mer
              from t_advit.blended_daily
             where workspace_id = %s and date = %s::date
            """,
            (ws.id, payload.date),
        )
        blended = cur.fetchone()

    return {
        "accepted": True,
        "parsed_from": result.parsed_from,
        "parsed": t.as_dict(),
        "issues": _issues(result),
        # Challenges are surfaced back, never silently swallowed.
        "questions": result.questions,
        "blended": blended,
    }


# ---------------------------------------------------------------------------
# Economics (PRD 12.4, founder decision D6)
# ---------------------------------------------------------------------------


class EconomicsRequest(BaseModel):
    spend_inr: float
    aov_inr: float
    gross_margin_rate: float
    total_orders: int | None = None
    confirmed_orders: int | None = None
    cancelled_orders: int | None = None
    rto_orders: int | None = None
    delivered_orders: int | None = None
    revenue_inr: float | None = None
    fulfilment_cost_inr: float = 0.0
    return_freight_inr: float = 0.0
    target_profit_share: float = 0.0
    confirm_rate_trend: float | None = None


@router.post("/api/economics")
def economics(
    payload: EconomicsRequest,
    # Authenticated, deliberately unscoped: arithmetic over numbers in the
    # request body, touching no tenant row. Listed in route_audit.UNSCOPED.
    principal: Annotated[Principal, Depends(current_principal)] = None,
) -> dict[str, Any]:
    """RTO-adjusted contribution margin and the CAC ceiling derived from the
    account's own margin structure.

    ROAS is returned because the owner expects it - always beside the number
    that actually decides whether to scale.
    """
    truth = BusinessTruth(
        total_orders=payload.total_orders,
        confirmed_orders=payload.confirmed_orders,
        cancelled_orders=payload.cancelled_orders,
        rto_orders=payload.rto_orders,
        delivered_orders=payload.delivered_orders,
        revenue_inr=payload.revenue_inr,
    )
    unit = UnitEconomics(
        aov_inr=payload.aov_inr,
        gross_margin_rate=payload.gross_margin_rate,
        fulfilment_cost_inr=payload.fulfilment_cost_inr,
        return_freight_inr=payload.return_freight_inr,
        target_profit_share=payload.target_profit_share,
    )

    e = compute_economics(truth, unit, payload.spend_inr)
    may_scale, why = scaling_verdict(e, payload.confirm_rate_trend)

    return {
        "economics": e.as_dict(),
        "scaling": {"permitted": may_scale, "reason": why},
        "note": (
            "Contribution margin is the optimisation target, not ROAS. For a COD "
            "business the two can differ by an order of magnitude."
        ),
    }


# ---------------------------------------------------------------------------
# CTA decision model (PRD 11.5)
# ---------------------------------------------------------------------------


class CTARequest(BaseModel):
    aov_inr: float
    gross_margin_rate: float
    is_cod: bool = True
    rto_rate: float | None = None
    rto_margin_tolerance: float = 0.25
    is_sensitive_category: bool = False
    sensitivity_reason: str | None = None
    needs_explanation: bool = False
    considered_purchase: bool = True
    sales_agents: int = 0
    working_hours_per_day: float = 8.0
    leads_per_day_capacity: int | None = None
    current_leads_per_day: int | None = None
    median_response_minutes: int | None = None
    has_whatsapp_bsp: bool = False
    has_crm: bool = False
    has_landing_page: bool = False
    pixel_event_volume_ok: bool = False
    dataset_present: bool = False
    audience_reads_comfortably: bool = True


@router.post("/api/cta/recommend")
def cta_recommend(
    payload: CTARequest,
    principal: Annotated[Principal, Depends(current_principal)] = None,
) -> dict[str, Any]:
    """Recommend a conversion destination from economics, sales capacity,
    category sensitivity and measurement readiness - never from preference."""
    return recommend_cta(AccountProfile(**payload.model_dump())).as_dict()


# ---------------------------------------------------------------------------
# The owner's CTA decision (PRD 11.5)
# ---------------------------------------------------------------------------


class CTADecision(BaseModel):
    destination: str
    # Kept as a training signal, the way a rejection reason is: WHY the owner
    # chose against the recommendation is more useful than the choice.
    reason: str | None = None


@router.put("/api/workspaces/{workspace_id}/cta")
def choose_cta(
    payload: CTADecision,
    ws: Annotated[AuthorizedWorkspace, Depends(authorized_workspace)],
) -> dict[str, Any]:
    """Record where this workspace's campaigns send people.

    Written to account_context under (sales_operation, primary_cta) as
    `owner_asserted`, on the TENANT connection under auth.uid(). That table is
    tenant-writable and this is the case it is for: the owner asserting a fact
    about their own operation. The strategy gate reads only `owner_asserted`
    rows, so an inference somebody seeded - "the ad account is called 'call
    ads', so probably calls" - never stands in for a decision.

    Answering releases the proposal the gate was holding, if there is one:
    the open t_advit.held_proposals row is resolved as `cta_set` and returned
    as `released_proposal`, so the page can say what the answer unblocked.
    """
    key = payload.destination.strip().lower()
    if key not in ACCEPTED:
        raise HTTPException(
            422,
            f"{payload.destination!r} is not a destination; choose one of "
            + ", ".join(sorted(ACCEPTED)),
        )
    destination = ACCEPTED[key].value

    with ws.principal.tx() as cur:
        # Supersede rather than overwrite. The previous decision stays as the
        # record of what campaigns built before today were built for.
        cur.execute(
            """
            update t_advit.account_context
               set valid_to = now()
             where workspace_id = %s::uuid
               and dimension = %s and key = %s
               and valid_to is null
            """,
            (ws.id, CTA_DIMENSION, CTA_KEY),
        )
        cur.execute(
            """
            insert into t_advit.account_context
              (workspace_id, dimension, key, value_json, confidence, source, asserted_by)
            values (%s::uuid, %s, %s, to_jsonb(%s::text), 1.0, 'owner_asserted', auth.uid())
            returning id::text
            """,
            (ws.id, CTA_DIMENSION, CTA_KEY, destination),
        )
        row_id = cur.fetchone()["id"]
        cur.execute(
            """
            select core.log_audit('workspace', 'cta.chosen',
                                  p_org => %s::uuid, p_workspace => %s::uuid,
                                  p_actor_type => 'user', p_actor => auth.uid(),
                                  p_payload => %s::jsonb)
            """,
            (ws.org_id, ws.id, json.dumps({"destination": destination, "reason": payload.reason})),
        )

    # The answer releases whatever question was waiting on it. On the SERVICE
    # connection, after the tenant write has committed, for the same reason
    # compute_blended_daily runs there above: t_advit.held_proposals is the
    # system's record of its own question, `authenticated` holds SELECT on it
    # and nothing else, and granting a tenant UPDATE - even on the two
    # resolution columns - would let a member close a question by editing the
    # record rather than by answering it. The resolution is a consequence the
    # backend observes of the owner's own act, which is exactly what the
    # destination row above is. None when nothing was waiting: an owner may
    # set a destination before ever asking for a campaign.
    with service_conn() as conn, conn.cursor() as cur:
        released = held.resolve(cur, workspace_id=ws.id, by=held.CTA_SET)
        conn.commit()

    return {
        "destination": destination,
        "account_context_id": row_id,
        # The proposal this answer released, so the page can say so and point
        # the owner back to Chat: the held proposal is not re-run here, because
        # it was proposed without a destination and the next turn re-proposes
        # it with the destination written into the action.
        "released_proposal": released,
    }
