"""The report, analytics and suggestions read model.

One route, because the three tabs the owner asked for are three views of the
same period: what the money did, what the system learned from doing things to
it, and what it would like to do next. Reading them in one request means the
KPI band, the daily table and the pending proposals cannot describe three
different moments.

Everything here runs on the TENANT connection and nothing here writes. That is
not caution - it is the definition of a report: the rows RLS says this caller
may see, computed in SQL and handed over as facts. No model arithmetic, and no
service-role read that could quietly return a row the caller is not entitled
to.

What this module refuses:

  * to turn an unknown into a zero. A day with no ingested spend has a NULL
    spend, a NULL CAC and a NULL MER, and the ``gaps`` column says why. The
    same rule holds for every total: a sum over inputs that were never reported
    is NULL, never 0, because a CAC of zero makes acquisition look free and a
    margin of zero looks like break-even rather than "we do not know".
  * to show a resolved question as open. A proposal the CTA gate held is a
    row in ``t_advit.held_proposals`` (app/orchestrator/held.py), written by
    the orchestrator and closed by the backend when the owner answers. The
    open row - at most one per workspace - is returned with its question, the
    proposal and the CTA model's recommendation; a row the owner has answered
    or a newer hold has superseded is history, not a suggestion. ``held`` is
    a bool here because the table WAS checked: the old ``held: null`` said
    the check could not be made, and now it can.
  * to show one tenant another tenant's learnings. Account-tier rows are
    filtered to this workspace in the query AND by ``learnings_select``; the
    shared tiers are gated on the plan the same way ``assemble_context`` gates
    industry intelligence, read from ``core.can`` on every call.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.auth.scope import AuthorizedWorkspace, authorized_workspace
from app.orchestrator import held

router = APIRouter()

# A shorter window than a week has no week-over-week to speak of; a longer one
# than a quarter is an export, not a report. Clamped rather than refused so a
# stale link keeps working.
MIN_DAYS = 7
MAX_DAYS = 90
DEFAULT_DAYS = 30

# Relative change below this reads as flat. Half a percent is inside the
# rounding of the business-truth figures it is computed from.
FLAT_BELOW = 0.005

# Which way is good. Spend has no better direction on its own - more spend at
# a CAC under the ceiling is the goal, more spend at one over it is the failure
# - so it is reported without a judgement.
BETTER_WHEN = {"spend_inr": None, "blended_cac_inr": "down", "mer": "up"}


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


def _ratio(numerator: Any, denominator: Any) -> float | None:
    """A quotient that is unknown when either side is, or the denominator is
    zero. `None`, not `0.0`, for the reason the module docstring gives."""
    if numerator is None or denominator is None:
        return None
    d = float(denominator)
    if d == 0:
        return None
    return float(numerator) / d


def _compare(key: str, this: float | None, previous: float | None) -> dict[str, Any]:
    """This week against last, with a direction only when both sides are known."""
    direction: str | None = None
    change_pct: float | None = None
    if this is not None and previous is not None:
        delta = this - previous
        if previous != 0:
            change_pct = delta / abs(previous)
            direction = "flat" if abs(change_pct) < FLAT_BELOW else ("up" if delta > 0 else "down")
        elif delta == 0:
            direction = "flat"
        else:
            # From nothing to something has no percentage, but it has a direction.
            direction = "up" if delta > 0 else "down"
    return {
        "this": this,
        "previous": previous,
        "change_pct": change_pct,
        "direction": direction,
        "better_when": BETTER_WHEN[key],
    }


# ---------------------------------------------------------------------------
# The queries
# ---------------------------------------------------------------------------

# Driven by blended_daily with the dashboard's own filter and order, so the
# rows here ARE the dashboard's money rows, joined to the two inputs the
# dashboard does not show: what was spent that day and what was delivered.
# `sum()` over no metrics rows is NULL, which is the honest reading of "nothing
# ingested"; `coalesce(..., 0)` is precisely the mistake 20260911000002 fixed.
MONEY_SERIES = """
select b.date, s.spend_inr, bt.delivered_revenue_inr, bt.delivered_orders,
       b.blended_cac_inr, b.mer, b.contribution_margin_inr,
       b.rto_rate, b.confirm_rate, b.delivered_aov_inr, b.gaps, b.computed_at
  from t_advit.blended_daily b
  left join t_advit.business_truth bt
         on bt.workspace_id = b.workspace_id and bt.date = b.date
  left join lateral (
         select sum(m.spend_inr) as spend_inr
           from t_advit.metrics_daily m
          where m.workspace_id = b.workspace_id
            and m.date = b.date and m.level = 'account'
       ) s on true
 where b.workspace_id = %(workspace)s::uuid
   and b.date >= current_date - make_interval(days => %(days)s)
 order by b.date desc
"""

# Period totals and the two seven-day windows in one pass, all from ONE
# day-level join of spend to business truth.
#
# The first draft summed spend over every day with a metrics row and orders
# over every day with a truth row, then divided the two. On the product's own
# common case - spend synced nightly, the evening report missed on some days -
# that overstates CAC and understates MER by the share of unreported days,
# while claiming to be a rate. Every ratio below is built from PAIRED days:
# spend and delivered orders from the same date, both reported. The raw period
# sums keep their own, wider, coverage and say so.
#
# The two windows are bounded on their own dates, not by the period: at
# days=7 the previous week lies entirely outside the period, and a version
# that filtered by period first compared this week with one day and printed
# a direction. contribution_margin_inr on blended_daily is PER DELIVERED
# ORDER (t_advit.contribution_margin_per_delivered_order), so the period
# figure is the order-weighted sum, not a sum of per-order values.
TOTALS = """
with bounds as (
  select current_date                     as today,
         current_date - %(days)s          as period_from,
         current_date - 6                 as this_from,
         current_date - 13                as prev_from,
         current_date - 7                 as prev_to
),
spend as (
  select m.date, sum(m.spend_inr) as spend_inr
    from t_advit.metrics_daily m, bounds b
   where m.workspace_id = %(workspace)s::uuid and m.level = 'account'
     and m.date >= least(b.period_from, b.prev_from)
   group by m.date
),
truth as (
  select t.date, t.delivered_revenue_inr, t.delivered_orders,
         t.rto_orders, t.confirmed_orders, t.total_orders
    from t_advit.business_truth t, bounds b
   where t.workspace_id = %(workspace)s::uuid
     and t.date >= least(b.period_from, b.prev_from)
),
margin as (
  select d.date, d.contribution_margin_inr
    from t_advit.blended_daily d, bounds b
   where d.workspace_id = %(workspace)s::uuid and d.date >= b.period_from
),
days as (
  select coalesce(s.date, t.date, mg.date) as date,
         s.spend_inr, t.delivered_revenue_inr, t.delivered_orders,
         t.rto_orders, t.confirmed_orders, t.total_orders,
         mg.contribution_margin_inr
    from spend s
    full join truth t  on t.date = s.date
    full join margin mg on mg.date = coalesce(s.date, t.date)
),
agg as (
  select
    -- raw period sums, each over whatever days it was reported
    sum(spend_inr)             filter (where date >= b.period_from) as spend,
    sum(delivered_revenue_inr) filter (where date >= b.period_from) as revenue,
    sum(delivered_orders)      filter (where date >= b.period_from) as delivered,
    count(*) filter (where date >= b.period_from and spend_inr is not null)             as spend_days,
    count(*) filter (where date >= b.period_from and delivered_orders is not null)      as reported_days,
    -- paired sums for the rates: both sides reported on the same day
    sum(spend_inr)        filter (where date >= b.period_from and spend_inr is not null and delivered_orders is not null) as cac_spend,
    sum(delivered_orders) filter (where date >= b.period_from and spend_inr is not null and delivered_orders is not null) as cac_delivered,
    count(*)              filter (where date >= b.period_from and spend_inr is not null and delivered_orders is not null) as cac_days,
    sum(spend_inr)             filter (where date >= b.period_from and spend_inr is not null and delivered_revenue_inr is not null) as mer_spend,
    sum(delivered_revenue_inr) filter (where date >= b.period_from and spend_inr is not null and delivered_revenue_inr is not null) as mer_revenue,
    count(*)                   filter (where date >= b.period_from and spend_inr is not null and delivered_revenue_inr is not null) as mer_days,
    sum(rto_orders)       filter (where date >= b.period_from and rto_orders is not null and confirmed_orders is not null) as rto_orders,
    sum(confirmed_orders) filter (where date >= b.period_from and rto_orders is not null and confirmed_orders is not null) as rto_base,
    sum(confirmed_orders) filter (where date >= b.period_from and confirmed_orders is not null and total_orders is not null) as confirmed,
    sum(total_orders)     filter (where date >= b.period_from and confirmed_orders is not null and total_orders is not null) as confirm_base,
    -- the period's margin: per-order margin times that day's delivered orders
    sum(contribution_margin_inr * delivered_orders)
      filter (where date >= b.period_from and contribution_margin_inr is not null and delivered_orders is not null) as margin,
    count(*) filter (where date >= b.period_from and contribution_margin_inr is not null and delivered_orders is not null) as margin_days,
    -- the two windows, on their own dates, paired the same way
    sum(spend_inr) filter (where date >= b.this_from) as spend_this,
    sum(spend_inr) filter (where date between b.prev_from and b.prev_to) as spend_prev,
    sum(spend_inr)        filter (where date >= b.this_from and spend_inr is not null and delivered_orders is not null) as cac_spend_this,
    sum(delivered_orders) filter (where date >= b.this_from and spend_inr is not null and delivered_orders is not null) as cac_delivered_this,
    sum(spend_inr)        filter (where date between b.prev_from and b.prev_to and spend_inr is not null and delivered_orders is not null) as cac_spend_prev,
    sum(delivered_orders) filter (where date between b.prev_from and b.prev_to and spend_inr is not null and delivered_orders is not null) as cac_delivered_prev,
    sum(spend_inr)             filter (where date >= b.this_from and spend_inr is not null and delivered_revenue_inr is not null) as mer_spend_this,
    sum(delivered_revenue_inr) filter (where date >= b.this_from and spend_inr is not null and delivered_revenue_inr is not null) as mer_revenue_this,
    sum(spend_inr)             filter (where date between b.prev_from and b.prev_to and spend_inr is not null and delivered_revenue_inr is not null) as mer_spend_prev,
    sum(delivered_revenue_inr) filter (where date between b.prev_from and b.prev_to and spend_inr is not null and delivered_revenue_inr is not null) as mer_revenue_prev,
    count(distinct date) filter (where date between b.prev_from and b.prev_to and (spend_inr is not null or delivered_orders is not null)) as prev_days_with_data
  from days, bounds b
  group by b.period_from, b.this_from, b.prev_from, b.prev_to
)
select b.today, b.period_from, b.this_from, b.prev_from, b.prev_to, a.*
  from bounds b left join agg a on true
"""

# The same retrieval assemble_context makes, minus the prompt: this
# workspace's own account-tier rows always, the shared tiers only when the plan
# carries industry intelligence. `learnings_select` already refuses another
# workspace's account rows; the predicate here is so the query says what it
# means rather than relying on the policy to make it true.
LEARNINGS = """
select id::text, tier::text as tier, statement, confidence, evidence_n,
       status::text as status, effect_size, conditions_json, updated_at as updated
  from t_advit.learnings
 where status <> 'historical'
   and (valid_to is null or valid_to > now())
   and ((tier = 'account' and workspace_id = %(workspace)s::uuid)
        or (tier <> 'account' and %(shared_entitled)s))
 order by (tier = 'account') desc, confidence desc, evidence_n desc
 limit 50
"""

ENTITLED = "select core.can(%(org)s::uuid, 'feature.industry_intelligence') as entitled"

# "Measured" is `vs_expected is not null` - the mark app/learning/outcomes.py
# leaves when it has looked, whatever it found. A row still queued at its
# horizon has nothing to report and is not one of the twenty.
OUTCOMES = """
select o.id::text, o.decision_id::text as decision_id, d.decision_type,
       d.chosen_option, o.verdict::text as verdict, o.horizon_days, o.measured_at,
       o.vs_expected ->> 'metric'              as metric,
       o.vs_expected ->> 'metric_label'        as metric_label,
       o.vs_expected ->> 'predicted_direction' as predicted_direction,
       o.vs_expected ->> 'better_when'         as better_when,
       (o.vs_expected -> 'before' ->> 'value')::float as before_value,
       (o.vs_expected -> 'after'  ->> 'value')::float as after_value,
       (o.vs_expected ->> 'delta')::float      as delta,
       (o.vs_expected ->> 'target')::float     as target,
       o.notes
  from t_advit.outcomes o
  join t_advit.decisions d on d.id = o.decision_id
 where o.workspace_id = %(workspace)s::uuid
   and o.vs_expected is not null
 order by o.measured_at desc
 limit 20
"""


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


@router.get("/api/workspaces/{workspace_id}/reports")
def reports(
    ws: Annotated[AuthorizedWorkspace, Depends(authorized_workspace)],
    days: int = Query(default=DEFAULT_DAYS),
) -> dict[str, Any]:
    """The period's money, what was learned from it, and what is proposed."""
    days = max(MIN_DAYS, min(MAX_DAYS, days))

    # The approvals inbox has one query, in app.main, and the suggestions tab
    # must show exactly the rows it shows. Imported here rather than at module
    # level because app.main includes this router: the cycle is real, and a
    # copy of the SQL would be a second inbox that drifts from the first.
    from app.main import list_approvals

    with ws.principal.tx() as cur:
        cur.execute(MONEY_SERIES, {"workspace": ws.id, "days": days})
        money = cur.fetchall()

        cur.execute(TOTALS, {"workspace": ws.id, "days": days})
        t = cur.fetchone()

        cur.execute(ENTITLED, {"org": ws.org_id})
        shared_entitled = bool((cur.fetchone() or {}).get("entitled"))

        cur.execute(LEARNINGS, {"workspace": ws.id, "shared_entitled": shared_entitled})
        learnings = cur.fetchall()

        cur.execute(OUTCOMES, {"workspace": ws.id})
        outcomes = cur.fetchall()

        # On the tenant connection like everything else here: held_proposals_select
        # decides whether the row is this caller's to see, and a non-member has
        # already been given 404 by authorized_workspace.
        held_open = held.open_for(cur, ws.id)

    pending = list_approvals(ws, status="pending")

    today: date = t["today"]
    period_from: date = t["period_from"]

    totals = {
        "spend_inr": _float(t["spend"]),
        "delivered_revenue_inr": _float(t["revenue"]),
        "delivered_orders": None if t["delivered"] is None else int(t["delivered"]),
        # Rates rest on paired days only - spend and the other side reported on
        # the same date - so a missed evening report lowers the coverage below
        # rather than inflating the rate.
        "blended_cac_inr": _ratio(t["cac_spend"], t["cac_delivered"]),
        "mer": _ratio(t["mer_revenue"], t["mer_spend"]),
        "contribution_margin_inr": _float(t["margin"]),
        "rto_rate": _ratio(t["rto_orders"], t["rto_base"]),
        "confirm_rate": _ratio(t["confirmed"], t["confirm_base"]),
        # How much of the period each figure actually covers. A margin summed
        # over four known days out of thirty is a fact about four days, and
        # the reader needs to know that before comparing it with the spend.
        "coverage": {
            "period_days": days,
            "reported_days": int(t["reported_days"] or 0),
            "spend_days": int(t["spend_days"] or 0),
            "cac_days": int(t["cac_days"] or 0),
            "mer_days": int(t["mer_days"] or 0),
            "margin_days": int(t["margin_days"] or 0),
        },
    }

    week_over_week = {
        "windows": {
            "this": {"from": t["this_from"].isoformat(), "to": today.isoformat()},
            "previous": {"from": t["prev_from"].isoformat(), "to": t["prev_to"].isoformat()},
            # Both windows are summed on their own dates whatever the period,
            # so a 7-day report still compares against a full previous week.
            "previous_days_with_data": int(t["prev_days_with_data"] or 0),
        },
        "spend_inr": _compare("spend_inr", _float(t["spend_this"]), _float(t["spend_prev"])),
        "blended_cac_inr": _compare(
            "blended_cac_inr",
            _ratio(t["cac_spend_this"], t["cac_delivered_this"]),
            _ratio(t["cac_spend_prev"], t["cac_delivered_prev"]),
        ),
        "mer": _compare(
            "mer",
            _ratio(t["mer_revenue_this"], t["mer_spend_this"]),
            _ratio(t["mer_revenue_prev"], t["mer_spend_prev"]),
        ),
    }

    return {
        "period": {
            "from": period_from.isoformat(),
            "to": today.isoformat(),
            "days": days,
        },
        "money": money,
        "totals": totals,
        "week_over_week": week_over_week,
        "learnings": learnings,
        "entitlements": {"industry_intelligence": shared_entitled},
        "outcomes": outcomes,
        "suggestions": {
            "pending_approvals": pending,
            # The question the CTA gate is holding a proposal behind, from
            # the one open t_advit.held_proposals row - or the same keys with
            # held: false when the table was checked and nothing is open.
            "cta_gate": held_open if held_open is not None else held.nothing_open(),
        },
        "note": (
            "Every figure is computed in SQL from business truth and ingested "
            "spend. A null is an unknown, never a zero; the gaps on each day say "
            "which input was missing."
        ),
    }
