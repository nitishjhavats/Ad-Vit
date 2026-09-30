-- =============================================================================
-- ad-vit - mirrored Meta entities, metrics warehouse, business truth
--
-- The warehouse is the source of truth for money; Meta is the source of truth
-- for delivery (PRD 12.2). Every metric row records the attribution regime in
-- force when it landed, because the January and March 2026 window changes make
-- naive year-over-year comparison confidently wrong (PRD 12.5, FR-041).
-- =============================================================================

create type t_advit.attribution_regime as enum (
  'pre_2026',        -- 7d/28d view-through still available
  'post_2026_01',    -- view-through windows removed 12 Jan 2026
  'post_2026_03'     -- click-through = link clicks only; engaged view 10s -> 5s
);

create type t_advit.learning_state as enum (
  'learning', 'learning_limited', 'active', 'unknown'
);

-- ---------------------------------------------------------------------------
-- Mirrored Meta entities
-- ---------------------------------------------------------------------------

create table t_advit.campaigns (
  id           uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references t_advit.workspaces(id) on delete cascade,
  meta_id      text,
  name         text not null,
  objective    text,
  status       t_advit.entity_status not null default 'paused',
  stage        text,
  buying_type  text,
  budget_type  text,
  budget_inr   numeric(14,2),
  created_at   timestamptz not null default now(),
  synced_at    timestamptz,
  unique (workspace_id, meta_id)
);

create index campaigns_workspace_idx on t_advit.campaigns (workspace_id, status);

create table t_advit.ad_sets (
  id                 uuid primary key default gen_random_uuid(),
  workspace_id       uuid not null references t_advit.workspaces(id) on delete cascade,
  campaign_id        uuid references t_advit.campaigns(id) on delete cascade,
  meta_id            text,
  name               text not null,
  optimisation_event text,
  audience_json      jsonb not null default '{}'::jsonb,
  placements_json    jsonb not null default '{}'::jsonb,
  budget_inr         numeric(14,2),
  status             t_advit.entity_status not null default 'paused',
  learning_state     t_advit.learning_state not null default 'unknown',
  created_at         timestamptz not null default now(),
  synced_at          timestamptz,
  unique (workspace_id, meta_id)
);

create index ad_sets_campaign_idx on t_advit.ad_sets (campaign_id);

create table t_advit.creatives (
  id                 uuid primary key default gen_random_uuid(),
  workspace_id       uuid not null references t_advit.workspaces(id) on delete cascade,
  meta_id            text,
  asset_ref          text,
  media_type         text,
  ratio              text,
  duration_s         numeric(8,2),
  has_captions       boolean,

  -- Declared at upload. Undisclosed AI content is roughly 14% of all Meta
  -- rejections (PRD 13.2), so the declaration is recorded, not inferred.
  ai_generated       boolean not null default false,

  tags_json          jsonb not null default '{}'::jsonb,
  pillars_json       jsonb not null default '{}'::jsonb,
  embedding          extensions.vector(1536),
  similarity_max     numeric(4,3),
  compliance_verdict t_advit.compliance_verdict not null default 'not_evaluated',
  fatigue_score      numeric(5,4),
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),
  unique (workspace_id, meta_id),
  constraint creatives_similarity_range check (
    similarity_max is null or similarity_max between 0 and 1
  )
);

create index creatives_workspace_idx on t_advit.creatives (workspace_id);

create trigger creatives_touch
  before update on t_advit.creatives
  for each row execute function core.touch_updated_at();

create table t_advit.ads (
  id           uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references t_advit.workspaces(id) on delete cascade,
  ad_set_id    uuid references t_advit.ad_sets(id) on delete cascade,
  meta_id      text,
  name         text not null,
  creative_id  uuid references t_advit.creatives(id) on delete set null,
  cta_type     text,
  status       t_advit.entity_status not null default 'paused',
  created_at   timestamptz not null default now(),
  synced_at    timestamptz,
  unique (workspace_id, meta_id)
);

create index ads_ad_set_idx on t_advit.ads (ad_set_id);

-- ---------------------------------------------------------------------------
-- Metrics warehouse
-- ---------------------------------------------------------------------------

create table t_advit.metrics_daily (
  date                 date not null,
  workspace_id         uuid not null references t_advit.workspaces(id) on delete cascade,
  level                text not null,
  entity_id            text not null,
  impressions          bigint  not null default 0,
  reach                bigint  not null default 0,
  frequency            numeric(8,4),
  spend_inr            numeric(14,2) not null default 0,
  clicks               bigint  not null default 0,
  link_clicks          bigint  not null default 0,
  results              bigint  not null default 0,
  cost_per_result      numeric(14,4),
  ctr                  numeric(8,6),
  cpm                  numeric(12,4),
  cpc                  numeric(12,4),
  purchases            bigint  not null default 0,
  purchase_value_inr   numeric(14,2) not null default 0,
  attribution_regime   t_advit.attribution_regime not null default 'post_2026_03',
  ingested_at          timestamptz not null default now(),
  primary key (workspace_id, date, level, entity_id),
  constraint metrics_level_valid check (level in ('account','campaign','ad_set','ad'))
);

create index metrics_daily_ws_date_idx on t_advit.metrics_daily (workspace_id, date desc);

-- ---------------------------------------------------------------------------
-- Business truth - the product's heartbeat (PRD 12.1)
--
-- Without this the OS is another dashboard; with it, it is the only system in
-- the account that knows what actually happened.
-- ---------------------------------------------------------------------------

create table t_advit.business_truth (
  date                 date not null,
  workspace_id         uuid not null references t_advit.workspaces(id) on delete cascade,
  total_orders         integer,
  confirmed_orders     integer,
  cancelled_orders     integer,
  rto_orders           integer,
  delivered_orders     integer,
  revenue_inr          numeric(14,2),
  delivered_revenue_inr numeric(14,2),
  leads_received       integer,
  leads_contacted      integer,
  avg_response_min     integer,
  sales_feedback       text,
  business_issues      text,
  entered_by           uuid references core.platform_users(id) on delete set null,
  entered_at           timestamptz not null default now(),
  is_estimated         boolean not null default false,
  primary key (workspace_id, date),

  -- Validation on entry, not after the fact (PRD 12.1, FR-037).
  constraint business_truth_counts_non_negative check (
    coalesce(total_orders,0)     >= 0 and coalesce(confirmed_orders,0) >= 0 and
    coalesce(cancelled_orders,0) >= 0 and coalesce(rto_orders,0)       >= 0 and
    coalesce(delivered_orders,0) >= 0 and coalesce(leads_received,0)   >= 0 and
    coalesce(leads_contacted,0)  >= 0
  ),
  constraint business_truth_confirmed_within_total check (
    total_orders is null or confirmed_orders is null or cancelled_orders is null
    or (confirmed_orders + cancelled_orders) <= total_orders
  ),
  constraint business_truth_delivered_within_confirmed check (
    confirmed_orders is null or delivered_orders is null
    or delivered_orders <= confirmed_orders
  )
);

comment on table t_advit.business_truth is
  'Gaps are marked, never interpolated. A learning derived from a window with '
  'missing ground truth carries reduced confidence (PRD 12.1).';

create table t_advit.blended_daily (
  date                   date not null,
  workspace_id           uuid not null references t_advit.workspaces(id) on delete cascade,
  blended_cac_inr        numeric(14,4),
  contribution_margin_inr numeric(14,2),
  confirm_rate           numeric(6,5),
  rto_rate               numeric(6,5),
  delivered_aov_inr      numeric(14,2),
  mer                    numeric(10,4),
  cac_ceiling_inr        numeric(14,4),
  computed_at            timestamptz not null default now(),
  primary key (workspace_id, date)
);

-- ---------------------------------------------------------------------------
-- The economics, in SQL
--
-- PRD 17.7: arithmetic is never done by a model. Every rupee, ratio and
-- interval is computed here and handed to the agent as a fact. This eliminates
-- an entire class of confident numerical error - and is a cost decision as much
-- as a correctness one.
--
-- PRD 12.4 (founder decision D6): a 4.0 ROAS COD business with 22% cancellation
-- and 26% RTO is running at roughly 15% return on ad spend, not 300%.
-- ---------------------------------------------------------------------------

create or replace function t_advit.contribution_margin_per_delivered_order(
  p_aov_inr            numeric,
  p_gross_margin_rate  numeric,
  p_fulfilment_cost    numeric,
  p_rto_rate           numeric,
  p_return_freight     numeric
)
returns numeric
language sql
immutable
as $fn$
  -- Return freight is charged against the orders that DID deliver, so the
  -- failed deliveries are amortised at rto/(1-rto), not at rto.
  select (p_aov_inr * p_gross_margin_rate)
       - coalesce(p_fulfilment_cost, 0)
       - case
           when coalesce(p_rto_rate, 0) >= 1 then 0
           else (coalesce(p_rto_rate, 0) / (1 - coalesce(p_rto_rate, 0)))
                * coalesce(p_return_freight, 0)
         end;
$fn$;

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
  v_spend      numeric;
  v_bt         t_advit.business_truth%rowtype;
  v_confirm    numeric;
  v_rto        numeric;
  v_aov        numeric;
  v_cm         numeric;
  v_cac        numeric;
  v_mer        numeric;
begin
  select coalesce(sum(spend_inr), 0)
    into v_spend
    from t_advit.metrics_daily
   where workspace_id = p_workspace and date = p_date and level = 'account';

  select * into v_bt
    from t_advit.business_truth
   where workspace_id = p_workspace and date = p_date;

  if not found then
    -- No ground truth for the day. Record the gap rather than inventing one.
    insert into t_advit.blended_daily (date, workspace_id, computed_at)
    values (p_date, p_workspace, now())
    on conflict (workspace_id, date) do update set computed_at = now();
    return;
  end if;

  v_confirm := case when coalesce(v_bt.total_orders, 0) > 0
                    then v_bt.confirmed_orders::numeric / v_bt.total_orders end;

  v_rto     := case when coalesce(v_bt.confirmed_orders, 0) > 0
                    then coalesce(v_bt.rto_orders, 0)::numeric / v_bt.confirmed_orders end;

  v_aov     := case when coalesce(v_bt.delivered_orders, 0) > 0
                    then v_bt.delivered_revenue_inr / v_bt.delivered_orders end;

  -- Blended CAC is measured against DELIVERED orders, not leads and not
  -- platform-reported purchases: it is the money that actually reached the bank.
  v_cac     := case when coalesce(v_bt.delivered_orders, 0) > 0
                    then v_spend / v_bt.delivered_orders end;

  v_mer     := case when v_spend > 0
                    then coalesce(v_bt.delivered_revenue_inr, 0) / v_spend end;

  select coalesce(sum(
           t_advit.contribution_margin_per_delivered_order(
             v_aov,
             coalesce(p.margin_rate, 0.5),
             0, coalesce(v_rto, 0), 0
           )
         ) / nullif(count(*), 0), null)
    into v_cm
    from t_advit.catalog_products p
   where p.workspace_id = p_workspace;

  insert into t_advit.blended_daily (
    date, workspace_id, blended_cac_inr, contribution_margin_inr,
    confirm_rate, rto_rate, delivered_aov_inr, mer, computed_at
  )
  values (
    p_date, p_workspace, v_cac,
    case when v_cm is not null and v_bt.delivered_orders is not null
         then (v_cm * v_bt.delivered_orders) - v_spend end,
    v_confirm, v_rto, v_aov, v_mer, now()
  )
  on conflict (workspace_id, date) do update set
    blended_cac_inr         = excluded.blended_cac_inr,
    contribution_margin_inr = excluded.contribution_margin_inr,
    confirm_rate            = excluded.confirm_rate,
    rto_rate                = excluded.rto_rate,
    delivered_aov_inr       = excluded.delivered_aov_inr,
    mer                     = excluded.mer,
    computed_at             = now();
end;
$fn$;

-- ---------------------------------------------------------------------------
-- Row-level security
-- ---------------------------------------------------------------------------

alter table t_advit.campaigns      enable row level security;
alter table t_advit.ad_sets        enable row level security;
alter table t_advit.creatives      enable row level security;
alter table t_advit.ads            enable row level security;
alter table t_advit.metrics_daily  enable row level security;
alter table t_advit.business_truth enable row level security;
alter table t_advit.blended_daily  enable row level security;

do $policies$
declare t text;
begin
  foreach t in array array[
    'campaigns','ad_sets','creatives','ads',
    'metrics_daily','business_truth','blended_daily'
  ]
  loop
    execute format(
      'create policy %I on t_advit.%I for select to authenticated
         using (t_advit.is_workspace_member(workspace_id) or core.is_superadmin())',
      t || '_select', t
    );
    execute format(
      'create policy %I on t_advit.%I for all to authenticated
         using (t_advit.is_workspace_member(workspace_id))
         with check (t_advit.is_workspace_member(workspace_id))',
      t || '_write', t
    );
    execute format(
      'grant select, insert, update, delete on t_advit.%I to authenticated', t
    );
  end loop;
end;
$policies$;

grant all on all tables in schema t_advit to service_role;

grant execute on function t_advit.contribution_margin_per_delivered_order(
  numeric, numeric, numeric, numeric, numeric
) to authenticated;
grant execute on function t_advit.compute_blended_daily(uuid, date) to authenticated;
