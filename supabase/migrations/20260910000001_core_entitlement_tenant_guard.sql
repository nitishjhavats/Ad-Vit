-- =============================================================================
-- Fix: the entitlement functions were cross-tenant oracles
--
-- core.entitlement, core.access_mode, core.can, core.limit_int,
-- core.assert_entitled and core.org_entitlements are all SECURITY DEFINER,
-- granted to `authenticated`, and take the organisation as a caller-supplied
-- argument with no membership check. So any signed-in user could call
--
--     select * from core.org_entitlements('<some other tenant>');
--
-- and read that organisation's plan, seat count, ad-account allowance,
-- autonomy ceiling, token budget and any superadmin override granted to them.
--
-- Nothing is written, so this is not the audit-forgery class of bug. It is
-- worse in one specific way: it is silent. A forged row is at least visible
-- afterwards; an enumerated competitor's plan tier leaves no trace at all. On a
-- shared database serving several products, "which plan is my competitor on"
-- is a commercially valuable question, and it should not have an answer.
--
-- `can`, `limit_int` and `assert_entitled` all delegate to `entitlement` and
-- `access_mode`, and `org_entitlements` calls `entitlement` per feature. So
-- guarding those two guards all six, and there is exactly one place to audit.
--
-- Both were `language sql`; they become plpgsql so the guard can raise. Neither
-- is used inside an RLS policy (checked), so a raise here cannot turn a
-- correctly-filtered row into a query error.
-- =============================================================================

create or replace function core.assert_org_visible(p_org uuid)
returns void
language plpgsql
stable
security definer
set search_path = core, pg_catalog
as $fn$
declare
  v_caller uuid := auth.uid();
begin
  -- Inside a SECURITY DEFINER function current_user is the OWNER, so it cannot
  -- tell a tenant from the backend. auth.uid() can: it reads the request's JWT
  -- subject, which PostgREST sets for a signed-in user and which is absent for
  -- the service role and for a direct connection. A request with no subject is
  -- the backend, already inside the trust boundary because it holds the
  -- service-role credential. Same reasoning as core.log_audit.
  if v_caller is null then
    return;
  end if;

  -- A null organisation identifies nobody, so there is nothing to leak. The
  -- callers resolve it to the product default.
  if p_org is null then
    return;
  end if;

  if core.is_org_member(p_org, v_caller) or core.is_superadmin(v_caller) then
    return;
  end if;

  raise exception 'organisation % is not visible to you', p_org
    using errcode = '42501', hint = 'org_not_visible';
end;
$fn$;

comment on function core.assert_org_visible(uuid) is
  'Raise unless the calling tenant belongs to p_org. A caller with no JWT '
  'subject is the backend and passes. The single tenant check behind every '
  'entitlement lookup.';


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
end;
$fn$;


create or replace function core.access_mode(p_org uuid)
returns core.access_mode
language plpgsql
stable
security definer
set search_path = core, pg_catalog
as $fn$
begin
  perform core.assert_org_visible(p_org);

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
        and s.cancelled_at is null
      limit 1),
    'denied'::core.access_mode
  );
end;
$fn$;

-- Re-issued: `create or replace function` keeps existing grants, but these are
-- restated so a reader of this file can see the full privilege picture without
-- cross-referencing migration 3.
grant execute on function core.assert_org_visible(uuid) to authenticated;
grant execute on function core.entitlement(uuid, text)  to authenticated;
grant execute on function core.access_mode(uuid)        to authenticated;

grant all on all functions in schema core to service_role;
