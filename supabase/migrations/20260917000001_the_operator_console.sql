-- =============================================================================
-- The operator console: three acts only a person at the platform may perform
--
-- apps/superadmin is being built against the runtime's /api/admin/ routes, and
-- three of the things it has to do had no door in the database:
--
--   1. Activate or suspend an ORGANISATION. core.access_mode returns 'denied'
--      unless organisations.status = 'active' - and organisations_update_admin
--      let the organisation's own owner UPDATE that column. A suspended
--      customer could un-suspend itself through the data API. The verdict was
--      readable by the party it was about, and writable too.
--
--   2. RE-VERIFY a compliance rule. Platform Watch (20260914000001) reports a
--      rule whose as_of has aged past its window, and the finding says a human
--      must re-read the source. When they have, the only honest record of that
--      is a new as_of - and `authenticated` held SELECT only on policy_rules,
--      so there was no way to write one short of a migration.
--
--   3. The same for a platform_knowledge row.
--
-- What stays closed, and why: the rule's PATTERN. The Platform Watch contract
-- is that it detects and never acts, because "a compliance rule that a website
-- edit could rewrite is a compliance gate that a website edit could open". A
-- console edit is a website edit. The column grant below names as_of and
-- nothing else, so the console cannot spell a pattern change at all - the
-- PostgreSQL column-privilege check refuses the statement before any policy
-- runs.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- 1. Organisation status is the operator's.
--
-- Column-level narrowing for the tenant's own grant: owners and admins keep
-- the billing identity (which the GST invoice needs them to fill in) and lose
-- status and its three stamps. The status change itself goes through a
-- SECURITY DEFINER function with the superadmin check INSIDE it - the shape
-- 20260911000008 settled on, because a grant a policy body depends on cannot
-- be revoked without breaking the policy.
--
-- Column privileges are per role and a superadmin is `authenticated` like
-- everyone else, which is why this cannot be a second column grant: there is no
-- role to give it to. The function runs as its owner and is the one door.
-- ---------------------------------------------------------------------------
revoke update on core.organisations from authenticated;
grant update (name, legal_name, gstin, billing_email, billing_address_json, state_code)
  on core.organisations to authenticated;

create or replace function core.set_organisation_status(
  p_org    uuid,
  p_status core.org_status,
  p_reason text default null
)
returns core.organisations
language plpgsql
security definer
set search_path = core, pg_catalog
as $fn$
declare
  v_actor uuid := auth.uid();
  v_row   core.organisations;
begin
  if not core.is_superadmin() then
    -- 404-shaped on purpose, matching authorized_superadmin: a tenant probing
    -- this function learns that it exists and nothing else.
    raise exception 'not found' using errcode = '42501', hint = 'not_superadmin';
  end if;

  if p_status = 'suspended' and nullif(btrim(coalesce(p_reason, '')), '') is null then
    raise exception 'a suspension needs a reason' using errcode = '23514', hint = 'reason_required';
  end if;

  update core.organisations o
     set status            = p_status,
         activated_at      = case when p_status = 'active' then coalesce(o.activated_at, now()) else o.activated_at end,
         suspended_at      = case when p_status = 'suspended' then now() else null end,
         suspension_reason = case when p_status = 'suspended' then btrim(p_reason) else null end
   where o.id = p_org
   returning o.* into v_row;

  if not found then
    raise exception 'not found' using errcode = '42501', hint = 'org_unknown';
  end if;

  perform core.log_audit(
    'organisation'::core.audit_scope,
    case p_status
      when 'active'    then 'organisation.activated'
      when 'suspended' then 'organisation.suspended'
      else 'organisation.status_changed'
    end,
    p_org        => p_org,
    p_actor_type => 'superadmin'::core.actor_type,
    p_actor      => v_actor,
    p_payload    => jsonb_build_object('status', p_status, 'reason', p_reason)
  );

  return v_row;
end;
$fn$;

revoke execute on function core.set_organisation_status(uuid, core.org_status, text) from public;
grant  execute on function core.set_organisation_status(uuid, core.org_status, text) to authenticated;

comment on function core.set_organisation_status(uuid, core.org_status, text) is
  'The one way an organisation''s status changes from a session. Superadmin '
  'only, checked inside; a suspension carries a reason; every change is in the '
  'trail. The tenant''s own UPDATE grant no longer names the column.';


-- ---------------------------------------------------------------------------
-- 2 and 3. A rule or a knowledge row is re-verified by a person.
--
-- One column, one policy, one trigger. The trigger is the sanity check on the
-- DATE - it cannot be in the future and it cannot make the row older - and the
-- trail entry, so "who said this rule was still current, and when" has an
-- answer whichever path wrote it.
-- ---------------------------------------------------------------------------
grant update (as_of) on t_advit.policy_rules       to authenticated;
grant update (as_of) on t_advit.platform_knowledge to authenticated;

-- policy_rules_has_matcher (20260911000004) calls t_advit.distinct_count, and
-- a CHECK expression runs with the QUERYING user's privileges - the same rule
-- 20260911000008 learned about policy bodies. 20260910000003 revoked the
-- function from PUBLIC, so the first UPDATE of as_of by a session was refused
-- with "permission denied for function distinct_count": the constraint is
-- re-evaluated on every UPDATE, whichever column moved. The function is
-- IMMUTABLE and reads only its argument; granting it is harmless, and without
-- the grant the column grant above is decorative.
grant execute on function t_advit.distinct_count(text[]) to authenticated;

create policy policy_rules_superadmin_reverify on t_advit.policy_rules
  for update to authenticated
  using (core.is_superadmin())
  with check (core.is_superadmin());

create policy platform_knowledge_superadmin_reverify on t_advit.platform_knowledge
  for update to authenticated
  using (core.is_superadmin())
  with check (core.is_superadmin());

create or replace function t_advit.guard_reverify()
returns trigger
language plpgsql
security definer
set search_path = t_advit, core, pg_catalog
as $fn$
declare
  v_actor   uuid := auth.uid();
  v_subject text;
begin
  if new.as_of is not distinct from old.as_of then
    return new;
  end if;

  -- "Today" on the operator's clock, not the server's. The database runs on
  -- UTC and the operators are in India; between 00:00 and 05:30 IST the two
  -- disagree on the date, and a rule re-verified at 09:00 IST on the 16th was
  -- refused as "in the future" because Postgres still thought it was the
  -- 15th. Every real customer and every platform job is on IST
  -- (t_advit.workspaces.timezone, app/jobs/runner.py::PlatformJob.timezone),
  -- so that is the clock this date is checked against.
  if new.as_of > (now() at time zone 'Asia/Kolkata')::date then
    raise exception 'as_of % is in the future', new.as_of
      using errcode = '23514', hint = 'as_of_future';
  end if;

  if new.as_of < old.as_of then
    -- Re-verification says "I read the source today and the rule still
    -- holds". A date earlier than the one already on the row is not that; it
    -- is either a typo or an attempt to make a fresh reading look stale.
    raise exception 'as_of % is earlier than the current %', new.as_of, old.as_of
      using errcode = '23514', hint = 'as_of_moves_forward';
  end if;

  v_subject := case tg_table_name
                 when 'policy_rules' then to_jsonb(new) ->> 'code'
                 else (to_jsonb(new) ->> 'id')
               end;

  perform core.log_audit(
    'platform'::core.audit_scope,
    case tg_table_name
      when 'policy_rules' then 'rule.reverified'
      else 'knowledge.reverified'
    end,
    p_actor_type => (case when v_actor is null then 'system' else 'superadmin' end)::core.actor_type,
    p_actor      => v_actor,
    p_payload    => jsonb_build_object(
      'table',          tg_table_name,
      'subject',        v_subject,
      'previous_as_of', old.as_of,
      'as_of',          new.as_of,
      'source_url',     to_jsonb(new) ->> 'source_url'
    )
  );

  return new;
end;
$fn$;

revoke execute on function t_advit.guard_reverify() from public;

drop trigger if exists policy_rules_reverify on t_advit.policy_rules;
create trigger policy_rules_reverify
  before update of as_of on t_advit.policy_rules
  for each row execute function t_advit.guard_reverify();

drop trigger if exists platform_knowledge_reverify on t_advit.platform_knowledge;
create trigger platform_knowledge_reverify
  before update of as_of on t_advit.platform_knowledge
  for each row execute function t_advit.guard_reverify();

comment on policy policy_rules_superadmin_reverify on t_advit.policy_rules is
  'A superadmin may UPDATE, and the column grant says only as_of. The pattern, '
  'the severity and the source stay data a migration writes, never a console.';


-- ---------------------------------------------------------------------------
-- 4. The trail names an operator as an operator.
--
-- core.log_audit (20260907000001) takes the actor from the session and
-- overrules the caller-supplied actor_type to 'user' for anyone with a JWT
-- subject. Right for tenants - that is the forgery guard. But it made every
-- act from this console read as a user's, and the entitlement stamp trigger
-- has carried a comment saying so since 20260911000009. The type is now
-- derived from core.platform_users for the caller, the same row
-- authorized_superadmin reads; the argument is still ignored, so nothing a
-- caller sends can change how they are described. Identical body otherwise.
-- ---------------------------------------------------------------------------
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
    -- A caller with a session may only ever write as themselves. The TYPE is
    -- derived from a row, never from the argument: 'superadmin' when
    -- core.platform_users says so, 'user' otherwise. Before 20260917000001
    -- this was 'user' unconditionally, so every operator act in the trail -
    -- a suspension, a coupon, an entitlement override - read as an ordinary
    -- user's, and "under whose authority" had to be answered by joining the
    -- actor id back to the staff list. Agent, automation and system remain
    -- reserved for the backend.
    v_actor      := v_caller;
    v_actor_type := case when core.is_superadmin(v_caller) then 'superadmin' else 'user' end;

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
