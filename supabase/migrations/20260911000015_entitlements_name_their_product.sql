-- =============================================================================
-- Fix: access_mode and entitlement answered per ORGANISATION, on a table keyed
-- per (organisation, product)
--
-- core.access_mode(p_org) resolved:
--
--     from core.subscriptions s
--    where s.org_id = p_org
--      and s.cancelled_at is null
--    limit 1
--
-- The missing `and s.product_id = ...` is only half of it. The function HAS NO
-- PRODUCT PARAMETER, so it is structurally incapable of answering "is this
-- organisation's ad-vit subscription live". It returns one answer per
-- organisation while core.subscriptions is keyed per (org_id, product_id) - the
-- unique index subscriptions_one_live_per_product is the schema stating that
-- grain out loud.
--
-- `limit 1` with no ORDER BY then resolves the mismatch by whatever the scan
-- yields first. Measured: the plan is a SEQ SCAN, so the winner is physical heap
-- order - and UPDATEing a row moves it to the end of the heap. A routine status
-- change on the ad-vit subscription therefore hands the answer to the
-- neighbouring product. Not a stable coin flip; a coin that flips itself when
-- you touch it. That is why `limit 1` is deleted rather than given an ORDER BY.
--
-- This is not cross-tenant - org_id is always filtered and assert_org_visible
-- still gates. It is cross-PRODUCT inside one organisation, on a cluster that
-- 20260911000007 says out loud already hosts an unrelated HRMS.
--
-- Measured, as a non-superuser, with ad-vit cancelled and a second product live:
--   core.access_mode                      -> 'full'   (must be 'denied')
--   t_advit.effective_autonomy            -> 1        (must be 0)
--   t_advit.workspaces_due                -> returns the workspace, so the
--                                            unattended scheduler keeps waking
--                                            up and spending model budget on an
--                                            account that stopped paying
--   core.assert_entitled(.,'max_ad_accounts',1) -> PASSED (must raise)
--
-- pipeline.py gates every Meta-mutating tool on exactly `access_mode != 'full'`,
-- so a churned customer keeps full write access and real spend continues on
-- automation they cancelled. And nothing shows the account as lapsed, because
-- the dashboard reads the same function.
--
-- The reverse direction bites paying customers: an ad-vit org in good standing
-- can be forced to read_only by a neighbouring product's subscription - an
-- outage whose cause is not in ad-vit's data at all.
--
-- Unreachable today, because core.products holds one row. The severity is
-- entirely about WHEN, not whether - and the caller edits are done now, while
-- there is still one row, so that the day somebody INSERTs a second product is a
-- non-event rather than an outage.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- 1. Make the product relationship declarative, before changing any resolver.
--
-- Nothing tied core.subscriptions.plan_id to core.subscriptions.product_id, so
-- an ad-vit subscription could name another product's plan. That is not
-- hypothetical: with the resolver fix applied but this constraint missing, an
-- ad-vit subscription re-pointed at a plan carrying max_ad_accounts = 99 still
-- returned 99 against ad-vit's own limit of 3, and assert_entitled(.,50) passed.
-- The predicate in the resolver alone does not close it; this does.
--
-- Verified clean before adding: zero subscriptions whose plan names a different
-- product, zero plan_features granting another product's feature. So both are
-- added VALIDATED rather than NOT VALID - if a row already violated this,
-- somebody should find out now.
-- ---------------------------------------------------------------------------

alter table core.plans
  add constraint plans_id_product_unique unique (id, product_id);

comment on constraint plans_id_product_unique on core.plans is
  'Exists only to be the target of the composite foreign key on subscriptions. '
  'A plan''s product is part of its identity as far as a subscription is concerned.';

alter table core.subscriptions
  drop constraint if exists subscriptions_plan_id_fkey;

alter table core.subscriptions
  add constraint subscriptions_plan_matches_product
  foreign key (plan_id, product_id)
  references core.plans (id, product_id)
  on delete restrict;

comment on constraint subscriptions_plan_matches_product on core.subscriptions is
  'A subscription to product X must name a plan belonging to product X. '
  'Declarative and index-backed, so no future reader has to remember a '
  '`pl.product_id = s.product_id` predicate for the rule to hold.';


create or replace function core.guard_plan_feature_product()
returns trigger
language plpgsql
security definer
set search_path = core, pg_catalog
as $fn$
declare
  v_plan_product    uuid;
  v_feature_product uuid;
begin
  select pl.product_id into v_plan_product
    from core.plans pl where pl.id = new.plan_id;
  select fd.product_id into v_feature_product
    from core.feature_definitions fd where fd.key = new.feature_key;

  if v_plan_product is distinct from v_feature_product then
    raise exception
      'plan % belongs to product % and cannot grant feature %, which is defined by product %',
      new.plan_id, v_plan_product, new.feature_key, v_feature_product
      using errcode = '23514', hint = 'feature_product_mismatch';
  end if;

  return new;
end;
$fn$;

comment on function core.guard_plan_feature_product() is
  'A plan may only grant features of its own product. The same shape as '
  'core.guard_entitlement_value: the constraint the design assumed, made real. '
  'A CHECK cannot express it because it spans three tables.';

drop trigger if exists plan_features_product_guard on core.plan_features;
create trigger plan_features_product_guard
  before insert or update on core.plan_features
  for each row execute function core.guard_plan_feature_product();

revoke execute on function core.guard_plan_feature_product() from public;


-- ---------------------------------------------------------------------------
-- 2. ad-vit names its own product, in ad-vit's own schema.
--
-- `core` stays product-neutral: it is the control plane for every product on
-- this cluster and must not learn the name of one of them. The product-specific
-- knowledge lives on the product side, which is also where the callers are.
-- ---------------------------------------------------------------------------
create or replace function t_advit.product_id()
returns uuid
language sql
stable
security definer
set search_path = t_advit, core, pg_catalog
as $fn$
  select p.id from core.products p where p.key = 'advit' and p.is_active;
$fn$;

comment on function t_advit.product_id() is
  'This product''s row in core.products. Returns NULL if ad-vit is not an active '
  'product, which every caller below turns into a refusal rather than a guess.';

revoke execute on function t_advit.product_id() from public;
grant execute on function t_advit.product_id() to authenticated, advit_backend;


-- ---------------------------------------------------------------------------
-- 3. The product-scoped resolver.
--
-- NO DEFAULT on p_product, and that is not a style choice. `p_product uuid
-- default null` makes both overloads accept one argument, and PostgreSQL then
-- refuses to choose: "function core.access_mode(uuid) is not unique" (42725),
-- tested. Shipping that would break effective_autonomy, workspaces_due, the
-- policy store, the dashboard and the scheduler in one migration.
-- ---------------------------------------------------------------------------
create or replace function core.access_mode(p_org uuid, p_product uuid)
returns core.access_mode
language plpgsql
stable
security definer
set search_path = core, pg_catalog
as $fn$
begin
  perform core.assert_org_visible(p_org);

  if p_product is null then
    -- The house rule. With nothing to compare against this must refuse, not
    -- pick a subscription and hope.
    raise exception 'core.access_mode needs a product; refusing rather than choosing one'
      using errcode = '42501', hint = 'product_required';
  end if;

  if not exists (
    select 1 from core.organisations o
     where o.id = p_org and o.status = 'active'
  ) then
    return 'denied'::core.access_mode;
  end if;

  return coalesce(
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
        and s.product_id = p_product
        and s.cancelled_at is null),
    'denied'::core.access_mode
  );
end;
$fn$;

comment on function core.access_mode(uuid, uuid) is
  'Access mode for one organisation''s subscription to ONE product. The bare '
  'scalar subquery has no `limit 1`: subscriptions_one_live_per_product '
  'guarantees at most one row once the product is pinned, so a second row is a '
  'broken invariant and raising 21000 is the right answer to it.';


-- ---------------------------------------------------------------------------
-- 4. The one-argument form refuses. REPLACED IN PLACE, never dropped.
--
-- Every caller is a dollar-quoted function body, so PostgreSQL tracks no
-- dependency on it: `drop function core.access_mode(uuid)` succeeds silently
-- under the default RESTRICT, the migration applies cleanly, `supabase db reset`
-- comes up green, and the first real request fails with 42883. Tested.
--
-- CREATE OR REPLACE also preserves the existing ACL, so the grants to
-- authenticated / service_role / advit_backend survive.
-- ---------------------------------------------------------------------------
create or replace function core.access_mode(p_org uuid)
returns core.access_mode
language plpgsql
stable
security definer
set search_path = core, pg_catalog
as $fn$
begin
  raise exception
    'core.access_mode(org) cannot say which product it means; call core.access_mode(org, product)'
    using errcode = '42501', hint = 'product_required';
end;
$fn$;

comment on function core.access_mode(uuid) is
  'Refuses. Kept with its original signature rather than dropped, because every '
  'caller lives in a function body that PostgreSQL tracks no dependency on - a '
  'DROP would apply cleanly and fail at the first request instead.';


-- ---------------------------------------------------------------------------
-- 5. The entitlement resolvers. No new parameter: the FEATURE already names its
--    product, through core.feature_definitions.product_id, which was populated
--    all along and simply never consulted.
--
-- Three products must now agree - the subscription's, the plan's and the
-- feature's. The plan join is what closes the variant where an ad-vit
-- subscription names another product's plan; the constraint in section 1 makes
-- it unreachable, and the predicate stays so the function is correct on its own
-- terms rather than only in the presence of a constraint somewhere else.
-- ---------------------------------------------------------------------------
create or replace function core.entitlement(p_org uuid, p_feature text)
returns jsonb
language plpgsql
stable
security definer
set search_path = core, pg_catalog
as $fn$
begin
  perform core.assert_org_visible(p_org);

  return coalesce(
    (select o.value_json
       from core.entitlement_overrides o
      where o.org_id = p_org
        and o.feature_key = p_feature
        and (o.expires_at is null or o.expires_at > now())),
    (select pf.value_json
       from core.subscriptions s
       join core.plans pl             on pl.id = s.plan_id
       join core.plan_features pf     on pf.plan_id = s.plan_id
       join core.feature_definitions fd on fd.key = pf.feature_key
      where s.org_id = p_org
        and s.cancelled_at is null
        and pf.feature_key = p_feature
        and s.product_id = fd.product_id
        and pl.product_id = s.product_id),
    (select fd.default_json
       from core.feature_definitions fd
      where fd.key = p_feature
        and fd.is_active)
  );
end;
$fn$;


create or replace function core.org_entitlements(p_org uuid)
returns table(feature_key text, value_json jsonb, value_type core.feature_value_type, source text)
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
        -- The identical three-way join. Without it this reported a mis-granted
        -- value as source='plan', which makes a wrong number look authoritative
        -- on the superadmin screen and on the tenant's own plan page.
        select 1
          from core.subscriptions s
          join core.plans pl         on pl.id = s.plan_id
          join core.plan_features pf on pf.plan_id = s.plan_id
         where s.org_id = p_org
           and s.cancelled_at is null
           and pf.feature_key = fd.key
           and s.product_id = fd.product_id
           and pl.product_id = s.product_id
      ) then 'plan'
      else 'default'
    end
  from core.feature_definitions fd
  where fd.is_active
  order by fd.key;
$fn$;


-- ---------------------------------------------------------------------------
-- 6. The two gates resolve the product from the feature, so neither changes
--    signature and no caller of theirs has to move.
-- ---------------------------------------------------------------------------
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
  v_mode    core.access_mode;
  v_value   jsonb;
  v_limit   integer;
  v_product uuid;
begin
  perform core.assert_org_visible(p_org);

  select fd.product_id into v_product
    from core.feature_definitions fd where fd.key = p_feature;

  if v_product is null then
    -- An undefined feature cannot name a product, so there is no subscription
    -- to check. Refusing is the same answer 20260910000004 gives a feature with
    -- no definition, arrived at from the other side.
    raise exception 'Feature % is not defined', p_feature
      using errcode = '42501', hint = 'feature_undefined';
  end if;

  v_mode  := core.access_mode(p_org, v_product);
  v_value := core.entitlement(p_org, p_feature);

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

  if p_requested is not null then
    if jsonb_typeof(v_value) <> 'number' then
      raise exception
        'The % limit for organisation % is not a number (%); refusing rather than assuming',
        p_feature, p_org, v_value
        using errcode = '42501', hint = 'limit_unresolvable';
    end if;

    v_limit := v_value::text::integer;
    if p_requested > v_limit then
      raise exception 'Requested % exceeds the % limit of % for organisation %',
        p_requested, p_feature, v_limit, p_org
        using errcode = '42501', hint = 'limit_exceeded';
    end if;
  end if;
end;
$fn$;


-- plpgsql rather than sql, deliberately. SQL does not promise to evaluate the
-- arms of an `and` left to right, so the undefined-feature case would reach
-- core.access_mode with a NULL product and RAISE where it should return false.
create or replace function core.can(p_org uuid, p_feature text)
returns boolean
language plpgsql
stable
security definer
set search_path = core, pg_catalog
as $fn$
declare
  v_product uuid;
  v_value   jsonb;
begin
  select fd.product_id into v_product
    from core.feature_definitions fd where fd.key = p_feature;

  -- An undefined feature is not granted. A question about nothing is answered
  -- `false`, not with an exception - `can()` is the soft form and its callers
  -- treat it as a boolean.
  if v_product is null then
    return false;
  end if;

  v_value := core.entitlement(p_org, p_feature);
  if v_value is null or jsonb_typeof(v_value) <> 'boolean' then
    return false;
  end if;

  return v_value::text::boolean
     and core.access_mode(p_org, v_product) <> 'denied';
end;
$fn$;


-- ---------------------------------------------------------------------------
-- 7. The two t_advit callers, in the same migration - there is no moment at
--    which the one-argument form is refusing and something still calls it.
-- ---------------------------------------------------------------------------
create or replace function t_advit.effective_autonomy(p_workspace uuid)
returns smallint
language sql
stable
security definer
set search_path = t_advit, core, pg_catalog
as $fn$
  select case
    when w.is_paused then 0::smallint
    when core.access_mode(w.org_id, t_advit.product_id()) <> 'full' then 0::smallint
    else least(
      w.autonomy_level,
      coalesce(core.limit_int(w.org_id, 'max_autonomy_level'), 0)
    )::smallint
  end
  from t_advit.workspaces w
  where w.id = p_workspace
    and t_advit.may_see_workspace(p_workspace);
$fn$;


create or replace function t_advit.workspaces_due(
  p_job          text,
  p_local_hour   integer,
  p_local_minute integer default 0
)
returns table(workspace_id uuid, org_id uuid, timezone text, local_date date, local_time time without time zone)
language sql
stable
security definer
set search_path = t_advit, core, pg_catalog
as $fn$
  with local as (
    select w.id, w.org_id, w.timezone,
           (now() at time zone w.timezone)::date as local_date,
           (now() at time zone w.timezone)::time as local_time
      from t_advit.workspaces w
     where not w.is_paused
       -- A lapsed subscription stops the unattended work too. The tool
       -- pipeline would refuse the mutations anyway, but running the agents to
       -- produce proposals nobody may act on spends model budget on an account
       -- that is not paying. Now asked about THIS product, so a live
       -- subscription to a neighbouring one no longer keeps the scheduler awake.
       and core.access_mode(w.org_id, t_advit.product_id()) = 'full'
  )
  select l.id, l.org_id, l.timezone, l.local_date, l.local_time
    from local l
   where l.local_time >= make_time(p_local_hour, p_local_minute, 0)
     and not exists (
           select 1 from t_advit.job_runs r
            where r.workspace_id = l.id
              and r.job = p_job
              and r.local_date = l.local_date
         )
   order by l.id;
$fn$;


-- ---------------------------------------------------------------------------
-- 8. Grants on the new function.
--
-- 20260911000007 granted EXECUTE to advit_backend by enumerating pg_proc at the
-- moment it ran; nothing re-runs it, so a function created afterwards has no
-- grant at all and the whole service path gets "permission denied for function
-- access_mode" on every request. And PostgreSQL grants EXECUTE to PUBLIC on
-- creation, so the revoke is mandatory - that half is already enforced by
-- tests/test_function_privileges.py, which fails the build on omission.
-- ---------------------------------------------------------------------------
revoke execute on function core.access_mode(uuid, uuid) from public;
grant execute on function core.access_mode(uuid, uuid)
   to authenticated, service_role, advit_backend;
