-- =============================================================================
-- Fix: an unresolvable entitlement authorised the request instead of refusing
--
-- core.assert_entitled is the server-side authorisation gate for every limited
-- feature. Its limit check reads:
--
--     if p_requested is not null and jsonb_typeof(v_value) = 'number' then
--       ... raise if p_requested > v_limit ...
--     end if;
--
-- so when the stored value is NOT a number the whole check is skipped and the
-- function returns normally - which means authorised. Nothing validates what
-- gets stored, either: core.entitlement_overrides.value_json is never checked
-- against core.feature_definitions.value_type, so a superadmin typing "three"
-- where 3 belongs is accepted silently.
--
-- Reproduced against the running database before writing this:
--
--     insert into core.entitlement_overrides (org_id, feature_key, value_json, reason)
--     values ('<org>', 'max_ad_accounts', '"three"'::jsonb, 'typo');
--     select core.assert_entitled('<org>', 'max_ad_accounts', 99);   -- PASSED
--
-- Ninety-nine ad accounts against a limit of three, authorised, because the
-- limit could not be parsed. A limit that cannot be resolved must refuse.
--
-- The asymmetry made it worse rather than better: core.limit_int on the same
-- value raises 22P02 (invalid input syntax for integer). So one typo takes down
-- every read that resolves the feature - loudly, on every request - while the
-- authorisation path fails open silently. Exactly backwards.
--
-- Three changes, in the order they matter:
--
--   1. assert_entitled RAISES when a limit was requested and the value is not a
--      number. Authorisation refuses on doubt.
--   2. limit_int returns NULL rather than raising, so a malformed value reads as
--      an unknown allowance. Its callers already handle that safely -
--      t_advit.effective_autonomy does `coalesce(core.limit_int(...), 0)`,
--      which floors the workspace to L0. Unknown allowance degrades; it does not
--      crash and does not permit.
--   3. A write-time guard, so the typo cannot be stored in the first place.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. The authorisation path refuses what it cannot resolve.
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
  v_mode  core.access_mode;
  v_value jsonb;
  v_limit integer;
begin
  perform core.assert_org_visible(p_org);

  v_mode  := core.access_mode(p_org);
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
    -- The branch that used to be `and jsonb_typeof(v_value) = 'number'`. When
    -- the value was anything else the caller fell through this block and was
    -- authorised, so a malformed limit granted MORE than a well-formed one.
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

comment on function core.assert_entitled(uuid, text, integer) is
  'Server-side authorisation for a limited feature. Refuses when the subscription '
  'denies access, when the feature is not granted, when it is explicitly disabled, '
  'when the request exceeds the limit, and - deliberately - when the limit cannot '
  'be resolved to a number at all.';


-- ---------------------------------------------------------------------------
-- 2. The read path degrades instead of crashing.
-- ---------------------------------------------------------------------------

create or replace function core.limit_int(p_org uuid, p_feature text)
returns integer
language plpgsql
stable
security definer
set search_path = core, pg_catalog
as $fn$
declare
  v_value jsonb;
begin
  perform core.assert_org_visible(p_org);
  v_value := core.entitlement(p_org, p_feature);

  -- NULL means "no resolvable allowance", which every caller already treats as
  -- the safe floor - t_advit.effective_autonomy coalesces it to 0, dropping
  -- the workspace to L0. Raising here instead put a 22P02 inside the
  -- authorisation path of every request that touched the feature.
  if v_value is null or jsonb_typeof(v_value) <> 'number' then
    return null;
  end if;

  return v_value::text::integer;
end;
$fn$;

comment on function core.limit_int(uuid, text) is
  'Resolve a numeric entitlement, or NULL when there is none and when the stored '
  'value is not a number. Callers must treat NULL as an unknown allowance and '
  'floor accordingly; core.assert_entitled refuses outright rather than degrading.';


-- ---------------------------------------------------------------------------
-- 3. The typo cannot be stored.
--
-- core.feature_definitions.value_type has always declared what each feature
-- holds; nothing ever checked a written value against it. This is the same
-- shape as the log_audit fix - the constraint the design assumed, made real.
-- ---------------------------------------------------------------------------

create or replace function core.guard_entitlement_value()
returns trigger
language plpgsql
security definer
set search_path = core, pg_catalog
as $fn$
declare
  v_type text;
  v_got  text := jsonb_typeof(new.value_json);
begin
  select fd.value_type::text into v_type
    from core.feature_definitions fd
   where fd.key = new.feature_key;

  if v_type is null then
    raise exception 'Feature % is not defined', new.feature_key
      using errcode = '23514', hint = 'feature_undefined';
  end if;

  if (v_type = 'integer' and v_got <> 'number')
     or (v_type = 'boolean' and v_got <> 'boolean')
     or (v_type = 'string'  and v_got <> 'string') then
    raise exception 'Feature % is %-valued; got % (%)',
      new.feature_key, v_type, v_got, new.value_json
      using errcode = '23514', hint = 'value_type_mismatch';
  end if;

  if v_type = 'integer' then
    -- 2.5 ad accounts is not a stricter limit, it is a broken one, and
    -- value::text::integer would truncate it without saying so.
    if (new.value_json)::text::numeric <> trunc((new.value_json)::text::numeric) then
      raise exception 'Feature % is integer-valued; got the fractional value %',
        new.feature_key, new.value_json
        using errcode = '23514', hint = 'value_not_integer';
    end if;
    if (new.value_json)::text::numeric < 0 then
      raise exception 'Feature % cannot be negative; got %',
        new.feature_key, new.value_json
        using errcode = '23514', hint = 'value_out_of_range';
    end if;
  end if;

  return new;
end;
$fn$;

drop trigger if exists entitlement_overrides_value_guard on core.entitlement_overrides;
create trigger entitlement_overrides_value_guard
  before insert or update on core.entitlement_overrides
  for each row execute function core.guard_entitlement_value();

-- The same guard on the plan catalogue: a plan feature is written by the
-- operator too, and a typo there is worse - it applies to every organisation on
-- the plan rather than to one.
drop trigger if exists plan_features_value_guard on core.plan_features;
create trigger plan_features_value_guard
  before insert or update on core.plan_features
  for each row execute function core.guard_entitlement_value();


-- Every new function is PUBLIC-executable on creation; see
-- 20260910000003_revoke_public_execute.sql for why that matters here.
revoke execute on function core.guard_entitlement_value()          from public;
revoke execute on function core.assert_entitled(uuid, text, integer) from public;
revoke execute on function core.limit_int(uuid, text)                from public;

grant execute on function core.assert_entitled(uuid, text, integer) to authenticated;
grant execute on function core.limit_int(uuid, text)                to authenticated;

grant all on all tables    in schema core to service_role;
grant all on all sequences in schema core to service_role;
grant all on all functions in schema core to service_role;
