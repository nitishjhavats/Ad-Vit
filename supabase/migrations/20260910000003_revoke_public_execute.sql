-- =============================================================================
-- Fix: every SECURITY DEFINER function was executable by PUBLIC, including anon
--
-- PostgreSQL grants EXECUTE on a new function to PUBLIC by default, and nothing
-- in this repo ever revoked it. So the explicit `grant execute ... to
-- authenticated` lines throughout these migrations were describing an intent
-- the database did not enforce: 20 SECURITY DEFINER functions in `core` and
-- `marketing` were callable by *any* role, `anon` included.
--
-- That is worse than an over-broad grant, because of how the tenant guards are
-- written. core.assert_org_visible (and core.log_audit before it) treat a
-- caller with no JWT subject as the backend - the reasoning being that
-- PostgREST sets a subject for a signed-in user, and its absence therefore
-- means the service role or a direct connection, both already inside the trust
-- boundary.
--
-- `anon` has no JWT subject either. So an UNAUTHENTICATED caller took the
-- trusted branch. Verified against the running database before writing this:
--
--     set local role anon;
--     select core.entitlement('<any org>', 'max_seats');   -- returned 5
--
-- The same reasoning exposed core.my_org_ids / is_org_member / org_role /
-- has_org_role (which take an arbitrary p_user, so any user's tenancy was
-- readable), core.is_superadmin (is this person an operator?), and
-- t_advit.compute_blended_daily, which does not merely read - it WRITES
-- computed economics into any workspace's blended_daily.
--
-- Two layers, because the guard reasoning and the privilege layer each covered
-- for the other's absence:
--
--   1. Revoke EXECUTE from PUBLIC, and set the default so a future migration
--      cannot silently reintroduce it.
--   2. Teach the guard that "no subject" is not sufficient evidence of being
--      the backend - `anon` says so explicitly.
--
-- The explicit grants to authenticated/service_role already exist and are left
-- untouched; this only removes the blanket one underneath them.
-- =============================================================================

do $revoke$
declare
  r record;
begin
  for r in
    select n.nspname, p.proname,
           pg_get_function_identity_arguments(p.oid) as args
      from pg_proc p
      join pg_namespace n on n.oid = p.pronamespace
     where n.nspname in ('core', 't_advit')
  loop
    execute format(
      'revoke execute on function %I.%I(%s) from public',
      r.nspname, r.proname, r.args
    );
  end loop;
end
$revoke$;

-- No `alter default privileges` here, deliberately, and it is worth writing down
-- why so the next person does not spend the afternoon I did:
--
--   * The schema-scoped form - `alter default privileges in schema core revoke
--     execute on functions from public` - is accepted and silently does
--     NOTHING. It records no pg_default_acl row, and a function created
--     afterwards still comes out with the default PUBLIC grant. Verified.
--   * The global form (no `in schema`) does work. But default privileges are
--     per-role, not per-schema, so on the shared database this deploys to it
--     would change how `hrms`, `t_dailykpi` and every other product's functions
--     are created. Not our call to make.
--
-- So the enforcement is the standing test in
-- packages/saas-core-db/tests/test_function_privileges.py, which fails the
-- build the moment any function in these schemas is PUBLIC-executable. A test
-- that fails is a better reminder than a comment nobody reads.

-- A compute job, not a query. It writes t_advit.blended_daily for a
-- caller-supplied workspace, so a tenant holding it can overwrite the very
-- economics the scaling verdict reads. The scheduler runs as the service role.
revoke execute on function t_advit.compute_blended_daily(uuid, date) from authenticated;

-- Likewise: rolls up billing usage for an arbitrary day, platform-wide.
revoke execute on function core.rollup_usage_for_day(date) from authenticated;


-- ---------------------------------------------------------------------------
-- Layer 2: "no subject" is not proof of being the backend.
-- ---------------------------------------------------------------------------

create or replace function core.assert_org_visible(p_org uuid)
returns void
language plpgsql
stable
security definer
set search_path = core, pg_catalog
as $fn$
declare
  v_caller uuid := auth.uid();
  -- Survives into a SECURITY DEFINER body: `current_user` becomes the function
  -- owner here, but the `role` GUC still reports the role PostgREST switched
  -- to for this request. Verified empirically - under `set local role anon` a
  -- definer function sees current_user=postgres, role=anon.
  v_role text := coalesce(current_setting('role', true), '');
begin
  -- Checked BEFORE the no-subject branch below, because an anonymous caller has
  -- no subject either and would otherwise be waved through as the backend. This
  -- is the specific hole this migration exists to close; the revoke above is
  -- the primary fix and this is the one that still holds if someone re-grants.
  --
  -- Both PostgREST tenant roles are covered, not just `anon`. A request that
  -- arrived under `authenticated` but carries no subject is anonymous in every
  -- sense that matters here - the test conftest constructs exactly that shape
  -- and calls it an anonymous caller. Only a request with no PostgREST role at
  -- all is the backend.
  if v_caller is null and v_role in ('anon', 'authenticated') then
    raise exception 'an anonymous caller cannot resolve organisation entitlements'
      using errcode = '42501', hint = 'org_not_visible';
  end if;

  -- A request with no JWT subject and no PostgREST role is the service role or
  -- a direct connection: the backend, already inside the trust boundary
  -- because it holds the service-role credential.
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

grant execute on function core.assert_org_visible(uuid) to authenticated;
grant all on all functions in schema core      to service_role;
grant all on all functions in schema t_advit to service_role;
