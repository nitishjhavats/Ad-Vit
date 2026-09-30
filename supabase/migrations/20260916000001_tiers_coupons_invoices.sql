-- =============================================================================
-- Billing: the four tiers go live, coupons exist, and an invoice is a row
--
-- Three things, in the order they depend on each other (the first lives in the seed).
--
--   1. The PRD 19.3 ladder has been seeded INACTIVE since the first schema -
--      "one is_active flip away" - with no features on any of the four. This
--      flips them and gives each its features, from the PRD's own table rather
--      than from a guess. Where the PRD is silent (seats, workspace counts
--      below Agency, token budgets) the seed says so beside the number.
--
--   2. Coupons. The operator creates them - a percentage off, a validity
--      window, a redemption cap - and names which plans each applies to. A
--      tenant sees only the coupons that apply to the plan they are looking
--      at, applies one, and the price comes down by that much. Redemption is a
--      SECURITY DEFINER function that refuses on every doubt: expired, capped,
--      wrong plan, wrong product, not active.
--
--   3. Invoices, with GST. The environment has carried GST_RATE_PERCENT, a SAC
--      code and a seller GSTIN since the first commit and nothing read them.
--      An invoice is a row with its line, its discount, its tax and its number,
--      generated once per period by a platform job. Collecting the money is a
--      payment gateway's job and is not here.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- 1. The tiers - see supabase/seeds/01_core.sql
--
-- core.products is seeded, not migrated, so nothing here can name the product.
-- The plan catalogue - plans, plan_features, feature_definitions - has always
-- been seed data in this repository, and the tier activation joins it there.
-- ---------------------------------------------------------------------------


-- ---------------------------------------------------------------------------
-- 2. Coupons
-- ---------------------------------------------------------------------------
create table core.coupons (
  id              uuid primary key default gen_random_uuid(),
  product_id      uuid not null references core.products(id) on delete cascade,
  -- What the tenant sees and what an operator types. Upper-cased on write so
  -- "diwali25" and "DIWALI25" are one coupon.
  code            text not null,
  name            text not null,
  percent_off     numeric(5,2) not null,
  valid_from      timestamptz not null default now(),
  valid_to        timestamptz,
  -- NULL is unlimited. Zero is not a valid cap.
  max_redemptions integer,
  redemptions     integer not null default 0,
  is_active       boolean not null default true,
  created_by      uuid references core.platform_users(id),
  created_at      timestamptz not null default now(),

  constraint coupons_code_unique_per_product unique (product_id, code),
  constraint coupons_code_shape check (code ~ '^[A-Z0-9_-]{3,32}$'),
  constraint coupons_percent_range check (percent_off > 0 and percent_off <= 100),
  constraint coupons_window_ordered check (valid_to is null or valid_to > valid_from),
  constraint coupons_cap_positive check (max_redemptions is null or max_redemptions > 0),
  constraint coupons_redemptions_within_cap check (
    max_redemptions is null or redemptions <= max_redemptions
  )
);

comment on table core.coupons is
  'Operator-created discounts. A coupon applies only to the plans named in '
  'core.coupon_plans, only inside its window, only while active, and only up to '
  'its cap - and core.apply_coupon refuses on every one of those, so a tenant '
  'cannot reach a discount by naming a code the dropdown did not offer.';

create table core.coupon_plans (
  coupon_id uuid not null references core.coupons(id) on delete cascade,
  plan_id   uuid not null references core.plans(id) on delete cascade,
  primary key (coupon_id, plan_id)
);

comment on table core.coupon_plans is
  'Which plans a coupon may be applied to. A coupon with no rows here applies to '
  'nothing, which is what a coupon the operator has not finished setting up '
  'should do.';

-- A coupon and the plans it names must belong to one product. A CHECK cannot
-- span the join, so it is a trigger in the shape of guard_plan_feature_product.
create or replace function core.guard_coupon_plan_product()
returns trigger
language plpgsql
security definer
set search_path = core, pg_catalog
as $fn$
declare
  v_coupon_product uuid;
  v_plan_product   uuid;
begin
  select product_id into v_coupon_product from core.coupons where id = new.coupon_id;
  select product_id into v_plan_product   from core.plans   where id = new.plan_id;
  if v_coupon_product is distinct from v_plan_product then
    raise exception 'coupon % belongs to product % and cannot apply to plan % of product %',
      new.coupon_id, v_coupon_product, new.plan_id, v_plan_product
      using errcode = '23514', hint = 'coupon_product_mismatch';
  end if;
  return new;
end;
$fn$;

create trigger coupon_plans_product_guard
  before insert or update on core.coupon_plans
  for each row execute function core.guard_coupon_plan_product();

revoke execute on function core.guard_coupon_plan_product() from public;

alter table core.subscriptions
  add column coupon_id uuid references core.coupons(id) on delete set null;

comment on column core.subscriptions.coupon_id is
  'The coupon in force on this subscription, applied through core.apply_coupon. '
  'NULL is full price.';

-- Upper-case the code on the way in so lookups are one comparison.
create or replace function core.normalise_coupon_code()
returns trigger language plpgsql as $fn$
begin
  new.code := upper(trim(new.code));
  return new;
end;
$fn$;
create trigger coupons_normalise_code
  before insert or update of code on core.coupons
  for each row execute function core.normalise_coupon_code();
revoke execute on function core.normalise_coupon_code() from public;

-- -- RLS -------------------------------------------------------------------
alter table core.coupons      enable row level security;
alter table core.coupon_plans enable row level security;

create policy advit_backend_all on core.coupons      for all to advit_backend using (true) with check (true);
create policy advit_backend_all on core.coupon_plans for all to advit_backend using (true) with check (true);

-- Operators manage. Tenants SEE only what could apply to them right now: active,
-- inside the window, not exhausted. Not the redemption count, not the cap, not
-- who made it - a tenant sees an offer, not the operator's books.
create policy coupons_superadmin_all on core.coupons
  for all to authenticated
  using (core.is_superadmin()) with check (core.is_superadmin());

create policy coupons_tenant_sees_live_offers on core.coupons
  for select to authenticated
  using (
    is_active
    and valid_from <= now()
    and (valid_to is null or valid_to > now())
    and (max_redemptions is null or redemptions < max_redemptions)
  );

create policy coupon_plans_superadmin_all on core.coupon_plans
  for all to authenticated
  using (core.is_superadmin()) with check (core.is_superadmin());

create policy coupon_plans_read on core.coupon_plans
  for select to authenticated using (true);

grant select, insert, update, delete on core.coupons      to authenticated;
grant select, insert, update, delete on core.coupon_plans to authenticated;
grant select, insert, update on core.coupons      to advit_backend;
grant select, insert, update on core.coupon_plans to advit_backend;
-- Column-level narrowing: a tenant's grant set says "select" above; the policy
-- says which rows. Tenants get no INSERT/UPDATE/DELETE row through the policies,
-- so the table-level grant above is inert for them - stated so nobody reads the
-- grant as the boundary. The policy is the boundary.


-- -- Applying one -----------------------------------------------------------
--
-- Every reason to refuse is checked, in order, and the message says which.
-- A tenant applying a coupon is a tenant asking for a discount; the answer to
-- doubt is no.
create or replace function core.apply_coupon(p_org uuid, p_code text)
returns uuid
language plpgsql
volatile
security definer
set search_path = core, pg_catalog
as $fn$
declare
  v_sub     core.subscriptions%rowtype;
  v_coupon  core.coupons%rowtype;
  v_code    text := upper(trim(p_code));
begin
  perform core.assert_org_visible(p_org);

  -- Only an owner or admin of the organisation spends its money.
  if not core.has_org_role(p_org, array['owner','admin']::core.org_role[]) then
    raise exception 'only an owner or admin may apply a coupon for organisation %', p_org
      using errcode = '42501', hint = 'not_billing_admin';
  end if;

  select * into v_sub from core.subscriptions s
   where s.org_id = p_org and s.cancelled_at is null
     and s.product_id = (select id from core.products where key = 'advit')
   for update;
  if not found then
    raise exception 'organisation % has no live subscription to apply a coupon to', p_org
      using errcode = '42501', hint = 'no_subscription';
  end if;

  select * into v_coupon from core.coupons c
   where c.product_id = v_sub.product_id and c.code = v_code
   for update;
  if not found then
    raise exception 'no such coupon' using errcode = '42501', hint = 'coupon_unknown';
  end if;

  -- Idempotent, and checked BEFORE the refusals. Applying the coupon already
  -- on the subscription is a no-op, not a second redemption - and the first
  -- draft of this function checked the cap first, so an owner re-clicking the
  -- coupon they already held was told it was exhausted. The test caught it.
  if v_sub.coupon_id = v_coupon.id then
    return v_coupon.id;
  end if;

  if not v_coupon.is_active then
    raise exception 'coupon % is not active', v_code using errcode = '42501', hint = 'coupon_inactive';
  end if;
  if v_coupon.valid_from > now() or (v_coupon.valid_to is not null and v_coupon.valid_to <= now()) then
    raise exception 'coupon % is outside its validity window', v_code
      using errcode = '42501', hint = 'coupon_expired';
  end if;
  if v_coupon.max_redemptions is not null and v_coupon.redemptions >= v_coupon.max_redemptions then
    raise exception 'coupon % has been fully redeemed', v_code
      using errcode = '42501', hint = 'coupon_exhausted';
  end if;
  if not exists (select 1 from core.coupon_plans cp
                  where cp.coupon_id = v_coupon.id and cp.plan_id = v_sub.plan_id) then
    raise exception 'coupon % does not apply to this plan', v_code
      using errcode = '42501', hint = 'coupon_wrong_plan';
  end if;
  -- One coupon per subscription. Applying a new one replaces the old; the old
  -- one's redemption is not given back, because it was used.
  update core.subscriptions set coupon_id = v_coupon.id, updated_at = now()
   where id = v_sub.id;
  update core.coupons set redemptions = redemptions + 1 where id = v_coupon.id;

  perform core.log_audit('organisation', 'coupon.applied',
                         p_org => p_org, p_actor_type => 'user', p_actor => auth.uid(),
                         p_payload => jsonb_build_object('code', v_code, 'percent_off', v_coupon.percent_off));
  return v_coupon.id;
end;
$fn$;

revoke execute on function core.apply_coupon(uuid, text) from public;
grant execute on function core.apply_coupon(uuid, text) to authenticated, advit_backend;

-- What it costs, after the coupon. One place, so the plan page, the invoice and
-- the audit trail cannot disagree about a number.
create or replace function core.effective_price(p_plan uuid, p_coupon uuid)
returns table(list_price_inr numeric, percent_off numeric, discount_inr numeric, price_inr numeric)
language sql
stable
security definer
set search_path = core, pg_catalog
as $fn$
  select p.price_inr,
         coalesce(c.percent_off, 0),
         round(p.price_inr * coalesce(c.percent_off, 0) / 100, 2),
         round(p.price_inr - p.price_inr * coalesce(c.percent_off, 0) / 100, 2)
    from core.plans p
    left join core.coupons c on c.id = p_coupon
   where p.id = p_plan;
$fn$;

revoke execute on function core.effective_price(uuid, uuid) from public;
grant execute on function core.effective_price(uuid, uuid) to authenticated, advit_backend;


-- ---------------------------------------------------------------------------
-- 3. Invoices
-- ---------------------------------------------------------------------------
create type core.invoice_status as enum ('draft', 'issued', 'paid', 'void');

create sequence core.invoice_numbers;

create table core.invoices (
  id                 uuid primary key default gen_random_uuid(),
  org_id             uuid not null references core.organisations(id) on delete restrict,
  subscription_id    uuid not null references core.subscriptions(id) on delete restrict,
  -- BMG-2026-000001. The sequence is taken at ISSUE, not at draft, so a
  -- voided draft never burns a number - the GST rules want a gapless series.
  number             text unique,
  status             core.invoice_status not null default 'draft',
  period_start       date not null,
  period_end         date not null,
  plan_key           text not null,
  plan_name          text not null,
  list_price_inr     numeric(12,2) not null,
  coupon_code        text,
  percent_off        numeric(5,2) not null default 0,
  discount_inr       numeric(12,2) not null default 0,
  taxable_inr        numeric(12,2) not null,
  -- Snapshotted, because the rate can change and the invoice must not.
  gst_rate_percent   numeric(5,2) not null,
  sac_code           text not null,
  -- CGST+SGST for an intra-state buyer, IGST otherwise. Decided from the
  -- seller's and buyer's state codes at issue and written down.
  gst_split          text not null,
  cgst_inr           numeric(12,2) not null default 0,
  sgst_inr           numeric(12,2) not null default 0,
  igst_inr           numeric(12,2) not null default 0,
  total_inr          numeric(12,2) not null,
  buyer_legal_name   text,
  buyer_gstin        text,
  buyer_state_code   text,
  seller_gstin       text,
  seller_legal_name  text,
  issued_at          timestamptz,
  due_at             timestamptz,
  paid_at            timestamptz,
  voided_at          timestamptz,
  created_at         timestamptz not null default now(),

  constraint invoices_period_ordered check (period_end > period_start),
  constraint invoices_one_per_period unique (subscription_id, period_start),
  constraint invoices_issued_have_number check ((status = 'draft') = (number is null)),
  constraint invoices_split_valid check (gst_split in ('cgst_sgst', 'igst')),
  constraint invoices_arithmetic check (
    taxable_inr = list_price_inr - discount_inr
    and total_inr = taxable_inr + cgst_inr + sgst_inr + igst_inr
  ),
  constraint invoices_split_arithmetic check (
    (gst_split = 'cgst_sgst' and igst_inr = 0 and cgst_inr = sgst_inr)
    or (gst_split = 'igst' and cgst_inr = 0 and sgst_inr = 0)
  )
);

comment on table core.invoices is
  'One row per subscription per billing period, generated by the platform jobs '
  'process. Every rate and every party detail is snapshotted at issue so the '
  'row still says what was invoiced after the plan, the coupon or the GST rate '
  'has changed. Collecting the money is a payment gateway''s job, not this '
  'table''s.';

alter table core.invoices enable row level security;
create policy advit_backend_all on core.invoices for all to advit_backend using (true) with check (true);
create policy invoices_superadmin_all on core.invoices
  for all to authenticated using (core.is_superadmin()) with check (core.is_superadmin());
create policy invoices_org_read on core.invoices
  for select to authenticated
  -- Owners and admins. A media buyer does not see what the company is billed.
  using (core.has_org_role(org_id, array['owner','admin']::core.org_role[]));

grant select on core.invoices to authenticated;
grant select, insert, update on core.invoices to advit_backend;
grant usage on sequence core.invoice_numbers to advit_backend;
