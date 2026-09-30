-- =============================================================================
-- Fix: t_advit.compute_blended_daily reported unknowns as zeros
--
-- The function is the product's economics engine: everything downstream - the
-- dashboard, the facts block the model is told is authoritative, the scaling
-- verdict - reads what it writes. It had five places where a missing input
-- became a number instead of an absence.
--
-- The sharpest, reproduced against the running database with metrics_daily
-- empty (which it is - the table has no writer yet):
--
--     30 delivered orders, no ingested spend
--       -> blended_cac_inr = 0.0000   written as fact
--
-- A CAC of zero makes every scaling verdict maximally permissive: acquisition
-- appears free, so any spend looks justified. And the same function already
-- knew how to say "unknown" - v_mer correctly returns NULL when spend is zero.
-- It knew, and did not, for CAC.
--
-- The five:
--   1. coalesce(sum(spend_inr), 0)      - no rows became zero spend
--   2. coalesce(p.margin_rate, 0.5)     - an unknown margin became 50%
--   3. coalesce(delivered_revenue_inr,0)- unreported revenue became zero
--   4. coalesce(v_rto, 0)               - unreported returns became a 0% RTO
--   5. literal 0 for fulfilment cost and return freight
--
-- (5) is different from the others and is handled differently. Fulfilment cost
-- and return freight had nowhere to be stored at all, so zero was not a wrong
-- reading of the data - it was the absence of the data. Quantified: at a
-- realistic 26% RTO, passing zeros overstates margin per delivered order by
-- roughly 16%. Those columns are added here, and until a workspace fills them
-- in the margin is computed without them AND SAYS SO, so the figure is an
-- explicit upper bound rather than a silent overstatement.
--
-- The distinction that makes the rest work: `sum()` over zero rows returns
-- NULL, over rows returns a number. So a workspace that genuinely spent nothing
-- on a day it has metrics for still reads 0, and a workspace with no metrics at
-- all reads NULL. Those are different facts and the schema can now hold both.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Somewhere to record what was missing.
--
-- Without this the function's only options are a wrong number or a silent NULL,
-- and a silent NULL is barely better - the reader cannot tell "we measured zero"
-- from "we could not measure". Every consumer already renders gaps: the chat
-- response has a `gaps` list, and compute_facts emits gap strings.
-- ---------------------------------------------------------------------------

alter table t_advit.blended_daily
  add column if not exists gaps text[] not null default '{}';

comment on column t_advit.blended_daily.gaps is
  'Named inputs that were unavailable when this row was computed. A NULL metric '
  'beside an empty gaps array means genuinely zero; a NULL metric with a gap '
  'naming it means unmeasured. The two must never be confused.';


-- ---------------------------------------------------------------------------
-- The costs that had nowhere to live.
-- ---------------------------------------------------------------------------

alter table t_advit.catalog_products
  add column if not exists fulfilment_cost_inr numeric(12,2),
  add column if not exists return_freight_inr  numeric(12,2);

alter table t_advit.catalog_products
  drop constraint if exists catalog_products_costs_non_negative;
alter table t_advit.catalog_products
  add constraint catalog_products_costs_non_negative check (
    (fulfilment_cost_inr is null or fulfilment_cost_inr >= 0)
    and (return_freight_inr is null or return_freight_inr >= 0)
  );

comment on column t_advit.catalog_products.fulfilment_cost_inr is
  'Pick, pack and ship per delivered order. NULL means unknown, not zero - the '
  'contribution margin then reports itself as an upper bound.';
comment on column t_advit.catalog_products.return_freight_inr is
  'Cost of a failed delivery coming back. Amortised across the orders that DID '
  'deliver, at rto/(1-rto). NULL means unknown, not zero.';


-- ---------------------------------------------------------------------------
-- The function.
-- ---------------------------------------------------------------------------

create or replace function t_advit.compute_blended_daily(
  p_workspace uuid,
  p_date      date
)
returns void
language plpgsql
security definer
set search_path = t_advit, core, pg_catalog
as $fn$
declare
  v_spend        numeric;
  v_bt           t_advit.business_truth%rowtype;
  v_confirm      numeric;
  v_rto          numeric;
  v_aov          numeric;
  v_cm           numeric;
  v_cac          numeric;
  v_mer          numeric;
  v_gaps         text[] := '{}';
  v_known_margin integer;
  v_products     integer;
  v_missing_cost integer;
begin
  -- No coalesce. sum() over zero rows is NULL, which is the honest answer for
  -- "nothing has been ingested"; over rows it is a number, so a real zero-spend
  -- day still reads 0.
  select sum(spend_inr)
    into v_spend
    from t_advit.metrics_daily
   where workspace_id = p_workspace and date = p_date and level = 'account';

  if v_spend is null then
    v_gaps := v_gaps || 'spend_not_ingested'::text;
  end if;

  select * into v_bt
    from t_advit.business_truth
   where workspace_id = p_workspace and date = p_date;

  if not found then
    insert into t_advit.blended_daily (date, workspace_id, gaps, computed_at)
    values (p_date, p_workspace, v_gaps || 'business_truth_not_reported'::text, now())
    on conflict (workspace_id, date) do update
      set gaps = excluded.gaps, computed_at = now();
    return;
  end if;

  v_confirm := case when coalesce(v_bt.total_orders, 0) > 0
                         and v_bt.confirmed_orders is not null
                    then v_bt.confirmed_orders::numeric / v_bt.total_orders end;
  if v_confirm is null then
    v_gaps := v_gaps || 'confirm_rate_unmeasurable'::text;
  end if;

  -- Returns unreported is not the same as no returns. A 0% RTO flatters the
  -- contribution margin, which is exactly the direction that invites scaling.
  v_rto := case when coalesce(v_bt.confirmed_orders, 0) > 0
                     and v_bt.rto_orders is not null
                then v_bt.rto_orders::numeric / v_bt.confirmed_orders end;
  if v_rto is null then
    v_gaps := v_gaps || 'rto_not_reported'::text;
  end if;

  v_aov := case when coalesce(v_bt.delivered_orders, 0) > 0
                     and v_bt.delivered_revenue_inr is not null
                then v_bt.delivered_revenue_inr / v_bt.delivered_orders end;

  -- Blended CAC is measured against DELIVERED orders, not leads and not
  -- platform-reported purchases: it is the money that actually reached the bank.
  -- It requires spend to be KNOWN, not merely present - this is the line that
  -- used to write zero.
  v_cac := case when v_spend is not null and coalesce(v_bt.delivered_orders, 0) > 0
                then v_spend / v_bt.delivered_orders end;

  v_mer := case when v_spend > 0 and v_bt.delivered_revenue_inr is not null
                then v_bt.delivered_revenue_inr / v_spend end;

  -- Margin is refused rather than assumed. An invented 50% is not an upper
  -- bound - it can err in either direction - and it feeds the CAC ceiling,
  -- which decides whether the account is told it may scale.
  select count(*),
         count(*) filter (where p.margin_rate is not null),
         count(*) filter (where p.fulfilment_cost_inr is null
                             or p.return_freight_inr is null)
    into v_products, v_known_margin, v_missing_cost
    from t_advit.catalog_products p
   where p.workspace_id = p_workspace;

  if v_known_margin = 0 then
    v_cm := null;
    v_gaps := v_gaps || 'margin_rate_unknown'::text;
  else
    if v_known_margin < v_products then
      v_gaps := v_gaps || 'margin_rate_partial'::text;
    end if;
    if v_missing_cost > 0 then
      -- The margin is still computed, because a figure missing two cost lines
      -- is directionally useful. It is named as an upper bound so nobody reads
      -- it as measured.
      v_gaps := v_gaps || 'costs_incomplete_margin_is_an_upper_bound'::text;
    end if;

    select avg(
             t_advit.contribution_margin_per_delivered_order(
               v_aov, p.margin_rate,
               p.fulfilment_cost_inr, v_rto, p.return_freight_inr
             )
           )
      into v_cm
      from t_advit.catalog_products p
     where p.workspace_id = p_workspace and p.margin_rate is not null;
  end if;

  insert into t_advit.blended_daily (
    date, workspace_id, blended_cac_inr, contribution_margin_inr,
    confirm_rate, rto_rate, delivered_aov_inr, mer, gaps, computed_at
  )
  values (
    p_date, p_workspace, v_cac,
    -- Subtracting an unknown spend from a known margin produces a number that
    -- looks like profit and is not one.
    case when v_cm is not null
              and v_bt.delivered_orders is not null
              and v_spend is not null
         then (v_cm * v_bt.delivered_orders) - v_spend end,
    v_confirm, v_rto, v_aov, v_mer, v_gaps, now()
  )
  on conflict (workspace_id, date) do update set
    blended_cac_inr         = excluded.blended_cac_inr,
    contribution_margin_inr = excluded.contribution_margin_inr,
    confirm_rate            = excluded.confirm_rate,
    rto_rate                = excluded.rto_rate,
    delivered_aov_inr       = excluded.delivered_aov_inr,
    mer                     = excluded.mer,
    gaps                    = excluded.gaps,
    computed_at             = now();
end;
$fn$;

comment on function t_advit.compute_blended_daily(uuid, date) is
  'Recompute a day''s blended economics. Every metric is NULL when its inputs '
  'are unavailable, and t_advit.blended_daily.gaps names which - because a '
  'zero CAC makes acquisition look free and every scaling verdict permissive.';

revoke execute on function t_advit.compute_blended_daily(uuid, date) from public;
revoke execute on function t_advit.compute_blended_daily(uuid, date) from authenticated;

grant all on all tables    in schema t_advit to service_role;
grant all on all sequences in schema t_advit to service_role;
grant all on all functions in schema t_advit to service_role;
