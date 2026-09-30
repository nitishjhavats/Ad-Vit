-- =============================================================================
-- Fix: workspace_org answered "who owns this workspace?" for any workspace
--
-- t_advit.workspace_org(p_workspace) was:
--
--     select w.org_id from t_advit.workspaces w where w.id = p_workspace;
--
-- SECURITY DEFINER, so it runs as the owner of t_advit.workspaces, for whom RLS
-- is not weakened but ABSENT; granted EXECUTE to `authenticated`; and its WHERE
-- clause names only the caller's own argument and never the caller. The
-- workspaces_select policy is not merely bypassed - it is never consulted. The
-- whole guard was the input.
--
-- Reproduced as the rival tenant's owner, who has no relationship to Broadmate:
-- in the same transaction in which `select ... from t_advit.workspaces where id
-- = '<Broadmate workspace>'` returned ZERO rows, workspace_org on that same id
-- returned Broadmate's organisation uuid.
--
-- Three things leak, none of them tenant DATA:
--
--   1. workspace uuid -> owning organisation uuid, for every workspace in the
--      database.
--   2. a workspace-EXISTENCE oracle: a real id returns a uuid, a fabricated one
--      returns NULL. This is precisely the oracle app/auth/scope.py gives up a
--      useful status code to avoid - "404, never 403. A 403 confirms the
--      workspace exists, which rebuilds the enumeration oracle 20260910000001
--      was written to close". The API pays that price at the front door and this
--      handed the answer back at the side door.
--   3. through effective_autonomy, a one-bit is_paused oracle per foreign
--      workspace - see below.
--
-- Worth naming plainly: workspace uuids travel. They are path segments in
-- /api/workspaces/{workspace_id}/..., so they turn up in URLs, screenshots,
-- support tickets and referrer headers. This mapping is the join key between a
-- leaked URL and a named tenant. And t_advit is in config.toml's exposed schema
-- list, so this is reachable as POST /rest/v1/rpc/workspace_org from any browser
-- session holding any tenant's JWT - it does not need the agent runtime.
--
-- -----------------------------------------------------------------------------
-- effective_autonomy: the ledger's claim about it was WRONG, and what is
-- actually wrong with it is more interesting
--
-- The autonomy VALUE does not leak. Its second CASE arm calls
-- core.access_mode(w.org_id), which since 20260910000001 calls
-- core.assert_org_visible and raises for a stranger. But CASE evaluates its WHEN
-- conditions in order and STOPS, and the first arm is
--
--     when w.is_paused then 0::smallint
--
-- so a PAUSED foreign workspace returns 0 and never reaches the guard. That is a
-- one-bit oracle on another tenant's pause state. And the refusal that protects
-- the other arms is `raise exception 'organisation % is not visible to you',
-- p_org` - which interpolates the org uuid, printing exactly the mapping
-- workspace_org hands over directly. It is a second channel for the same secret,
-- not a second secret.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- The predicate, in one place.
--
-- Two POSITIVE arms - an allowlist, not a deny-list. That distinction is the
-- whole design, and the first draft of this fix got it wrong in exactly the way
-- this repo keeps getting things wrong.
--
-- The draft said: "if the role GUC is 'anon' or 'authenticated' then untrusted,
-- otherwise trusted". That enumerates the untrusted set and permits everything
-- else, on a GUC whose entire purpose is to be set per request. `service_role`
-- is not in that list. Neither is any role a later migration might add. A guard
-- phrased that way is not a guard, it is a list somebody has to remember to
-- extend.
--
-- Both arms below name something the CALLER CANNOT SET:
--
--   session_user is the LOGIN role and does not move with `set role`. Measured:
--   advit_tenant after `set local role authenticated` reports
--   current_user='authenticated', session_user='advit_tenant'. So a tenant
--   holding the claims it controls still lands on is_workspace_member.
--
--   rolbypassrls / rolsuper are role attributes, not GUCs. And the arm is not a
--   concession: a role that bypasses RLS can read t_advit.workspaces unfiltered
--   already, so refusing it HERE protects nothing and would only break the test
--   suites, which connect as `postgres` (rolbypassrls = true, rolsuper = false
--   on this cluster).
--
-- Measured matrix, on the live catalogue:
--   TRUSTED  advit_backend, advit_jobs, advit_service   (arm 1)
--            postgres, service_role, supabase_admin     (arm 2)
--   NOT      advit_tenant, anon, authenticated, authenticator
--
-- `authenticator` is the one worth pointing at: it is PostgREST's own login
-- role, it is what session_user would be if the API is ever fronted that way,
-- and it correctly comes out FALSE.
-- ---------------------------------------------------------------------------
create or replace function t_advit.may_see_workspace(p_workspace uuid)
returns boolean
language sql
stable
security definer
set search_path = t_advit, core, pg_catalog
as $fn$
  select case
    -- The product's own backend. advit_service and advit_jobs INHERIT
    -- advit_backend and hold a database credential no browser has.
    when pg_has_role(session_user, 'advit_backend', 'USAGE') then true

    -- A role for which row-level security does not apply to this table anyway.
    -- Refusing it here would be theatre.
    when (select r.rolbypassrls or r.rolsuper
            from pg_roles r where r.rolname = session_user) then true

    -- Everyone else answers the question the product already asks everywhere
    -- else. Deliberately is_workspace_member and not the wider
    -- core.is_org_member that governs the workspaces row itself:
    -- app/auth/scope.py already declares is_workspace_member the product's
    -- boundary and 404s an organisation member who holds no workspace grant, so
    -- this aligns the helpers with the API rather than with the table policy.
    else t_advit.is_workspace_member(p_workspace)
  end;
$fn$;

comment on function t_advit.may_see_workspace(uuid) is
  'May the current caller be told anything about this workspace? True for the '
  'product backend, for a role that bypasses RLS anyway, and for a member of the '
  'workspace. An allowlist on properties the caller cannot set per request - '
  'session_user does not move with SET ROLE, and rolbypassrls is not a GUC.';

revoke execute on function t_advit.may_see_workspace(uuid) from public;
grant execute on function t_advit.may_see_workspace(uuid) to authenticated, advit_backend;


-- ---------------------------------------------------------------------------
-- The guard is a WHERE conjunct, not a CASE in the target list.
--
-- That is load-bearing rather than stylistic. The qual is evaluated before the
-- projection, so for a stranger the ROW IS FILTERED OUT and the select list -
-- including core.access_mode(w.org_id), whose exception text carries the org
-- uuid - is never reached. One change closes the value, the is_paused arm and
-- the org-uuid-in-the-error-message channel together.
--
-- And the refusal is an ABSENT ROW, so a stranger gets NULL - which is what a
-- nonexistent workspace already returned. That removes the existence oracle
-- rather than relocating it: the two cases are now indistinguishable, which is
-- the point.
-- ---------------------------------------------------------------------------
create or replace function t_advit.workspace_org(p_workspace uuid)
returns uuid
language sql
stable
security definer
set search_path = t_advit, core, pg_catalog
as $fn$
  select w.org_id
    from t_advit.workspaces w
   where w.id = p_workspace
     and t_advit.may_see_workspace(p_workspace);
$fn$;


create or replace function t_advit.effective_autonomy(p_workspace uuid)
returns smallint
language sql
stable
security definer
set search_path = t_advit, core, pg_catalog
as $fn$
  select case
    when w.is_paused then 0::smallint
    when core.access_mode(w.org_id) <> 'full' then 0::smallint
    else least(
      w.autonomy_level,
      coalesce(core.limit_int(w.org_id, 'max_autonomy_level'), 0)
    )::smallint
  end
  from t_advit.workspaces w
  where w.id = p_workspace
    and t_advit.may_see_workspace(p_workspace);
$fn$;


-- ---------------------------------------------------------------------------
-- Grants, restated rather than assumed.
--
-- CREATE OR REPLACE preserves the ACL, so these two lines change nothing today.
-- They are here because the privilege picture should be readable next to the
-- function, the way 20260911000008 restates its five - and because the obvious
-- alternative fix, revoking EXECUTE, is a trap that has already been sprung once
-- in this repository.
--
-- t_advit.workspace_org is named in the USING and WITH CHECK of two live
-- policies (workspace_members_write and meta_connections_write, both `to
-- authenticated`). An RLS predicate is evaluated with the privileges of the
-- QUERYING user, so the tenant needs EXECUTE on every function a policy body
-- names, SECURITY DEFINER or not. Revoking it was measured: as the Broadmate
-- owner, `select from t_advit.workspace_members` and `select from
-- t_advit.meta_connections` both failed with "permission denied for function
-- workspace_org", exactly as revoking has_org_role broke every tenant read in
-- 20260911000008.
--
-- effective_autonomy is named by no policy, but app/main.py calls it through the
-- tenant connection, which does `set local role authenticated` - so revoking
-- there fails GET /api/workspaces/{id}/connections/health instead.
-- ---------------------------------------------------------------------------
grant execute on function t_advit.workspace_org(uuid)      to authenticated, advit_backend;
grant execute on function t_advit.effective_autonomy(uuid) to authenticated, advit_backend;
