-- =============================================================================
-- Common SaaS Core - products, plans, entitlements, subscriptions
--
-- Entitlements are the authorization spine, not a billing ornament. The PRD
-- independently specifies per-workspace token budgets (17.7), autonomy ceilings
-- L0-L4 (10.5) and account/spend caps (14.1); the platform brief independently
-- asks for feature and limit control. They resolve to one system, consulted at
-- step 2 of the tool-invocation pipeline (10.8) alongside the autonomy check.
--
-- Pricing is data, never code. The ad-vit default plan is seeded at
-- INR 15,000/month; additional tiers are additional rows.
-- =============================================================================

create type core.access_mode as enum ('full', 'read_only', 'denied');

create type core.feature_value_type as enum ('boolean', 'integer', 'string');

-- ---------------------------------------------------------------------------
-- Product catalogue
-- ---------------------------------------------------------------------------

create table core.products (
  id         uuid primary key default gen_random_uuid(),
  key        text not null unique,
  name       text not null,
  is_active  boolean not null default true,
  created_at timestamptz not null default now(),
  constraint products_key_format check (key ~ '^[a-z][a-z0-9_]{2,49}$')
);

comment on table core.products is
  'One row per AI OS product. Only advit exists today; HRMS, Sales and '
  'Finance reuse this control plane by adding rows, not by forking it.';

-- Declares which feature keys exist, their type, and the fallback when neither
-- an override nor a plan grant supplies a value. Also drives the superadmin
-- feature-control UI, so adding a feature never requires a UI change.
create table core.feature_definitions (
  key          text primary key,
  product_id   uuid not null references core.products(id) on delete cascade,
  name         text not null,
  description  text,
  value_type   core.feature_value_type not null,
  default_json jsonb not null,
  is_active    boolean not null default true,
  created_at   timestamptz not null default now(),
  constraint feature_key_format check (key ~ '^[a-z][a-z0-9_.]{2,63}$')
);

create index feature_definitions_product_idx on core.feature_definitions (product_id);

-- ---------------------------------------------------------------------------
-- Plans
-- ---------------------------------------------------------------------------

create table core.plans (
  id             uuid primary key default gen_random_uuid(),
  product_id     uuid not null references core.products(id) on delete cascade,
  key            text not null,
  name           text not null,
  description    text,
  price_inr      numeric(12,2) not null,
  billing_period core.billing_period not null default 'monthly',
  trial_days     integer not null default 14,
  grace_days     integer not null default 7,
  is_active      boolean not null default true,
  sort_order     integer not null default 0,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  unique (product_id, key),
  constraint plans_price_non_negative check (price_inr >= 0),
  constraint plans_trial_days_bounded  check (trial_days between 0 and 365),
  constraint plans_grace_days_bounded  check (grace_days between 0 and 365)
);

create table core.plan_features (
  plan_id     uuid not null references core.plans(id) on delete cascade,
  feature_key text not null references core.feature_definitions(key) on delete cascade,
  value_json  jsonb not null,
  primary key (plan_id, feature_key)
);

create trigger plans_touch
  before update on core.plans
  for each row execute function core.touch_updated_at();

-- ---------------------------------------------------------------------------
-- Subscriptions
-- ---------------------------------------------------------------------------

create table core.subscriptions (
  id                   uuid primary key default gen_random_uuid(),
  org_id               uuid not null references core.organisations(id) on delete cascade,
  product_id           uuid not null references core.products(id) on delete restrict,
  plan_id              uuid not null references core.plans(id) on delete restrict,
  status               core.subscription_status not null default 'trialing',
  trial_ends_at        timestamptz,
  current_period_start timestamptz not null default now(),
  current_period_end   timestamptz not null,
  grace_ends_at        timestamptz,
  cancelled_at         timestamptz,
  cancellation_reason  text,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),
  constraint subscriptions_period_ordered check (current_period_end > current_period_start)
);

-- One live subscription per organisation per product.
create unique index subscriptions_one_live_per_product
  on core.subscriptions (org_id, product_id)
  where cancelled_at is null;

create index subscriptions_status_idx on core.subscriptions (status, current_period_end);

create trigger subscriptions_touch
  before update on core.subscriptions
  for each row execute function core.touch_updated_at();

-- Superadmin per-organisation grants: "control users/seats and available
-- features" without inventing a bespoke plan for one customer.
create table core.entitlement_overrides (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references core.organisations(id) on delete cascade,
  feature_key text not null references core.feature_definitions(key) on delete cascade,
  value_json  jsonb not null,
  reason      text not null,
  set_by      uuid references core.platform_users(id) on delete set null,
  expires_at  timestamptz,
  created_at  timestamptz not null default now(),
  unique (org_id, feature_key)
);

comment on column core.entitlement_overrides.reason is
  'Mandatory. An unexplained entitlement grant is indistinguishable from a mistake.';

-- ---------------------------------------------------------------------------
-- Entitlement resolution
--
-- Order: active override -> plan grant -> feature default -> null.
-- SECURITY DEFINER because plan and feature tables are not readable by tenants
-- directly, but their resolved entitlements must be.
-- ---------------------------------------------------------------------------

create or replace function core.entitlement(p_org uuid, p_feature text)
returns jsonb
language sql
stable
security definer
set search_path = core, pg_catalog
as $fn$
  select coalesce(
    (select o.value_json
       from core.entitlement_overrides o
      where o.org_id = p_org
        and o.feature_key = p_feature
        and (o.expires_at is null or o.expires_at > now())),
    (select pf.value_json
       from core.subscriptions s
       join core.plan_features pf on pf.plan_id = s.plan_id
      where s.org_id = p_org
        and s.cancelled_at is null
        and pf.feature_key = p_feature
      limit 1),
    (select fd.default_json
       from core.feature_definitions fd
      where fd.key = p_feature
        and fd.is_active)
  );
$fn$;

-- Subscription status decides how far an entitlement carries.
--   trialing / active / pending_payment -> full
--   past_due / grace                    -> read_only  (PRD 18: degrade to
--                                          read-only rather than fail open, so
--                                          a lapsed payment never strands live
--                                          campaigns mid-flight)
--   expired / suspended / no sub        -> denied
create or replace function core.access_mode(p_org uuid)
returns core.access_mode
language sql
stable
security definer
set search_path = core, pg_catalog
as $fn$
  select case
    when not exists (
      select 1 from core.organisations o
       where o.id = p_org and o.status = 'active'
    ) then 'denied'::core.access_mode
    else coalesce(
      (select case s.status
                when 'trialing'        then 'full'
                when 'active'          then 'full'
                when 'pending_payment' then 'full'
                when 'past_due'        then 'read_only'
                when 'grace'           then 'read_only'
                else 'denied'
              end::core.access_mode
         from core.subscriptions s
        where s.org_id = p_org
          and s.cancelled_at is null
        limit 1),
      'denied'::core.access_mode
    )
  end;
$fn$;

create or replace function core.can(p_org uuid, p_feature text)
returns boolean
language sql
stable
security definer
set search_path = core, pg_catalog
as $fn$
  select coalesce((core.entitlement(p_org, p_feature))::text::boolean, false)
     and core.access_mode(p_org) <> 'denied';
$fn$;

create or replace function core.limit_int(p_org uuid, p_feature text)
returns integer
language sql
stable
security definer
set search_path = core, pg_catalog
as $fn$
  select nullif(core.entitlement(p_org, p_feature)::text, 'null')::integer;
$fn$;

-- Raises on denial so no caller can accidentally ignore a false return value.
-- 42501 (insufficient_privilege) maps cleanly to HTTP 403 in the API layer.
create or replace function core.assert_entitled(
  p_org       uuid,
  p_feature   text,
  p_requested integer default null
)
returns void
language plpgsql
stable
security definer
set search_path = core, pg_catalog
as $fn$
declare
  v_mode  core.access_mode := core.access_mode(p_org);
  v_value jsonb            := core.entitlement(p_org, p_feature);
  v_limit integer;
begin
  if v_mode = 'denied' then
    raise exception 'Organisation % has no active access (subscription denied)', p_org
      using errcode = '42501', hint = 'subscription_denied';
  end if;

  if v_value is null then
    raise exception 'Feature % is not granted to organisation %', p_feature, p_org
      using errcode = '42501', hint = 'feature_not_granted';
  end if;

  if jsonb_typeof(v_value) = 'boolean' and v_value::text::boolean is false then
    raise exception 'Feature % is disabled for organisation %', p_feature, p_org
      using errcode = '42501', hint = 'feature_disabled';
  end if;

  if p_requested is not null and jsonb_typeof(v_value) = 'number' then
    v_limit := v_value::text::integer;
    if p_requested > v_limit then
      raise exception 'Requested % exceeds the % limit of % for organisation %',
        p_requested, p_feature, v_limit, p_org
        using errcode = '42501', hint = 'limit_exceeded';
    end if;
  end if;
end;
$fn$;

-- Every resolved entitlement for an organisation, with its provenance. Backs
-- the superadmin feature-control screen and the tenant plan page.
create or replace function core.org_entitlements(p_org uuid)
returns table (
  feature_key text,
  value_json  jsonb,
  value_type  core.feature_value_type,
  source      text
)
language sql
stable
security definer
set search_path = core, pg_catalog
as $fn$
  select
    fd.key,
    core.entitlement(p_org, fd.key),
    fd.value_type,
    case
      when exists (
        select 1 from core.entitlement_overrides o
         where o.org_id = p_org and o.feature_key = fd.key
           and (o.expires_at is null or o.expires_at > now())
      ) then 'override'
      when exists (
        select 1 from core.subscriptions s
          join core.plan_features pf on pf.plan_id = s.plan_id
         where s.org_id = p_org and s.cancelled_at is null and pf.feature_key = fd.key
      ) then 'plan'
      else 'default'
    end
  from core.feature_definitions fd
  where fd.is_active
  order by fd.key;
$fn$;

-- ---------------------------------------------------------------------------
-- Row-level security
-- ---------------------------------------------------------------------------

alter table core.products              enable row level security;
alter table core.feature_definitions   enable row level security;
alter table core.plans                 enable row level security;
alter table core.plan_features         enable row level security;
alter table core.subscriptions         enable row level security;
alter table core.entitlement_overrides enable row level security;

-- Catalogue is world-readable to signed-in users (a pricing page needs it);
-- only the superadmin may change it.
create policy products_read on core.products
  for select to authenticated using (is_active or core.is_superadmin());
create policy products_write on core.products
  for all to authenticated using (core.is_superadmin()) with check (core.is_superadmin());

create policy feature_definitions_read on core.feature_definitions
  for select to authenticated using (true);
create policy feature_definitions_write on core.feature_definitions
  for all to authenticated using (core.is_superadmin()) with check (core.is_superadmin());

create policy plans_read on core.plans
  for select to authenticated using (is_active or core.is_superadmin());
create policy plans_write on core.plans
  for all to authenticated using (core.is_superadmin()) with check (core.is_superadmin());

create policy plan_features_read on core.plan_features
  for select to authenticated using (true);
create policy plan_features_write on core.plan_features
  for all to authenticated using (core.is_superadmin()) with check (core.is_superadmin());

-- Subscriptions and overrides: the organisation reads, only superadmin writes.
create policy subscriptions_read on core.subscriptions
  for select to authenticated
  using (core.is_org_member(org_id) or core.is_superadmin());
create policy subscriptions_write on core.subscriptions
  for all to authenticated using (core.is_superadmin()) with check (core.is_superadmin());

create policy entitlement_overrides_read on core.entitlement_overrides
  for select to authenticated
  using (core.is_org_member(org_id) or core.is_superadmin());
create policy entitlement_overrides_write on core.entitlement_overrides
  for all to authenticated using (core.is_superadmin()) with check (core.is_superadmin());

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

grant select                         on core.products              to authenticated;
grant select                         on core.feature_definitions   to authenticated;
grant select                         on core.plans                 to authenticated;
grant select                         on core.plan_features         to authenticated;
grant select                         on core.subscriptions         to authenticated;
grant select                         on core.entitlement_overrides to authenticated;
grant insert, update, delete         on core.products              to authenticated;
grant insert, update, delete         on core.feature_definitions   to authenticated;
grant insert, update, delete         on core.plans                 to authenticated;
grant insert, update, delete         on core.plan_features         to authenticated;
grant insert, update, delete         on core.subscriptions         to authenticated;
grant insert, update, delete         on core.entitlement_overrides to authenticated;

grant all on all tables in schema core to service_role;

grant execute on function core.entitlement(uuid, text)             to authenticated;
grant execute on function core.access_mode(uuid)                   to authenticated;
grant execute on function core.can(uuid, text)                     to authenticated;
grant execute on function core.limit_int(uuid, text)               to authenticated;
grant execute on function core.assert_entitled(uuid, text, integer) to authenticated;
grant execute on function core.org_entitlements(uuid)              to authenticated;
