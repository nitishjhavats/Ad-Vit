-- =============================================================================
-- Fix: core.log_audit allowed any tenant to forge another tenant's audit trail
--
-- The original definition is SECURITY DEFINER, granted to `authenticated`, and
-- takes p_org, p_workspace, p_actor and p_actor_type as caller-supplied
-- arguments with no membership check. Any signed-in user could therefore call
--
--     select core.log_audit('workspace', 'action.executed',
--                           p_org => '<some other tenant>',
--                           p_actor_type => 'agent');
--
-- and write an arbitrary row into another organisation's trail.
--
-- The damage is worse than an ordinary write because the table is append-only
-- by trigger: a forged row cannot be deleted by anyone, including us. The
-- integrity property was exactly inverted - history could not be corrected,
-- only fabricated.
--
-- Three changes:
--   1. The actor is taken from the session, not from the argument.
--   2. The organisation must be one the caller belongs to.
--   3. Non-human actor types (agent, automation, system) are reserved for the
--      service role, so a tenant cannot attribute their own action to an agent
--      and launder it through the trail.
-- =============================================================================

create or replace function core.log_audit(
  p_scope       core.audit_scope,
  p_event       text,
  p_org         uuid    default null,
  p_workspace   uuid    default null,
  p_actor_type  core.actor_type default 'user',
  p_actor       uuid    default auth.uid(),
  p_payload     jsonb   default '{}'::jsonb,
  p_run_id      uuid    default null,
  p_policy_id   uuid    default null,
  p_approval_id uuid    default null,
  p_ip          inet    default null,
  p_user_agent  text    default null
)
returns bigint
language plpgsql
security definer
set search_path = core, pg_catalog
as $fn$
declare
  v_id      bigint;
  v_imp_by  uuid;
  v_caller  uuid := auth.uid();
  -- Inside a SECURITY DEFINER function current_user is the OWNER, not the
  -- caller, so it cannot be used to tell a tenant from the backend. auth.uid()
  -- can: it reads the request's JWT subject, which PostgREST sets for a signed-in
  -- user and which is absent for the service role and for a direct connection.
  --
  -- So: a request carrying a JWT subject is a tenant and is constrained; a
  -- request without one is the backend, which is already inside the trust
  -- boundary because it holds the service-role credential.
  v_trusted boolean := v_caller is null;
  v_actor      uuid;
  v_actor_type core.actor_type;
begin
  if v_trusted then
    v_actor      := p_actor;
    v_actor_type := p_actor_type;
  else
    -- A tenant may only ever write as themselves, as a user.
    v_actor      := v_caller;
    v_actor_type := 'user';

    -- Platform scope is the operator's log, not a tenant's.
    if p_scope = 'platform' and not core.is_superadmin(v_caller) then
      raise exception 'only the superadmin may write a platform-scoped audit row'
        using errcode = '42501', hint = 'audit_scope_forbidden';
    end if;

    -- The organisation must be one the caller actually belongs to. This is
    -- the check whose absence made the whole trail forgeable.
    if p_org is not null
       and not core.is_org_member(p_org, v_caller)
       and not core.is_superadmin(v_caller) then
      raise exception 'cannot write an audit row for an organisation you are not a member of'
        using errcode = '42501', hint = 'audit_tenant_forbidden';
    end if;

    if p_workspace is not null and p_org is null then
      raise exception 'a workspace-scoped audit row must name its organisation'
        using errcode = '42501', hint = 'audit_org_required';
    end if;
  end if;

  select s.superadmin_id
    into v_imp_by
    from core.impersonation_sessions s
   where s.superadmin_id = v_actor
     and s.ended_at is null
     and s.expires_at > now()
   limit 1;

  insert into core.audit_log (
    scope, event, org_id, workspace_id, actor_type, actor_id, impersonated_by,
    payload_json, run_id, policy_decision_id, approval_id, ip, user_agent
  )
  values (
    p_scope, p_event, p_org, p_workspace, v_actor_type, v_actor, v_imp_by,
    coalesce(p_payload, '{}'::jsonb), p_run_id, p_policy_id, p_approval_id,
    p_ip, p_user_agent
  )
  returning id into v_id;

  return v_id;
end;
$fn$;

comment on function core.log_audit(
  core.audit_scope, text, uuid, uuid, core.actor_type, uuid, jsonb, uuid, uuid, uuid, inet, text
) is
  'Append a row to the audit trail. A tenant caller may only write as themselves, '
  'as actor_type user, and only for an organisation they belong to. Agent, '
  'automation and system attribution is reserved for the service role.';

-- ---------------------------------------------------------------------------
-- TRUNCATE bypasses a row-level trigger entirely, so the append-only guarantee
-- needed a statement-level trigger as well.
-- ---------------------------------------------------------------------------

create or replace function core.reject_audit_truncate()
returns trigger
language plpgsql
as $fn$
begin
  raise exception 'core.audit_log is append-only; TRUNCATE is not permitted'
    using errcode = '42501';
end;
$fn$;

drop trigger if exists audit_log_no_truncate on core.audit_log;
create trigger audit_log_no_truncate
  before truncate on core.audit_log
  for each statement execute function core.reject_audit_truncate();
