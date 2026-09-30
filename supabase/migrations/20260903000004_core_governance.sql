-- =============================================================================
-- Common SaaS Core - audit, usage metering, impersonation
--
-- One append-only audit primitive serves both the superadmin compliance log and
-- the PRD's "explain this decision" chain (14.4), scoped platform / organisation
-- / workspace, rather than two parallel substrates that drift apart.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Audit log
-- ---------------------------------------------------------------------------

create table core.audit_log (
  id                 bigint generated always as identity primary key,
  at                 timestamptz not null default now(),
  scope              core.audit_scope not null,
  org_id             uuid references core.organisations(id) on delete set null,

  -- Deliberately no foreign key: core must not depend on any product schema,
  -- or it cannot be lifted into a shared control plane later.
  workspace_id       uuid,

  actor_type         core.actor_type not null,
  actor_id           uuid,
  impersonated_by    uuid references core.platform_users(id) on delete set null,

  event              text not null,
  payload_json       jsonb not null default '{}'::jsonb,

  ip                 inet,
  user_agent         text,

  -- Correlation keys shared with the agent runtime's OpenTelemetry spans (17.8).
  run_id             uuid,
  policy_decision_id uuid,
  approval_id        uuid,

  constraint audit_event_format check (event ~ '^[a-z][a-z0-9_.]{2,99}$')
);

comment on table core.audit_log is
  'Append-only. When something goes wrong at 2 a.m. the first question is always '
  '"who did this, and under whose authority?" - a system that cannot answer that '
  'precisely cannot be trusted with a budget (PRD 3.5).';

create index audit_log_org_at_idx       on core.audit_log (org_id, at desc);
create index audit_log_scope_at_idx     on core.audit_log (scope, at desc);
create index audit_log_actor_idx        on core.audit_log (actor_id, at desc);
create index audit_log_run_idx          on core.audit_log (run_id) where run_id is not null;
create index audit_log_workspace_at_idx on core.audit_log (workspace_id, at desc)
  where workspace_id is not null;

-- Append-only is enforced in the database, not by convention. An audit trail
-- that privileged code can quietly rewrite is not an audit trail.
create or replace function core.reject_audit_mutation()
returns trigger
language plpgsql
as $fn$
begin
  raise exception 'core.audit_log is append-only; % is not permitted', tg_op
    using errcode = '42501';
end;
$fn$;

create trigger audit_log_no_update
  before update on core.audit_log
  for each row execute function core.reject_audit_mutation();

create trigger audit_log_no_delete
  before delete on core.audit_log
  for each row execute function core.reject_audit_mutation();

-- ---------------------------------------------------------------------------
-- Impersonation
-- ---------------------------------------------------------------------------

create table core.impersonation_sessions (
  id             uuid primary key default gen_random_uuid(),
  superadmin_id  uuid not null references core.platform_users(id) on delete cascade,
  org_id         uuid not null references core.organisations(id) on delete cascade,
  target_user_id uuid references core.platform_users(id) on delete set null,
  reason         text not null,
  consent_ref    text,
  started_at     timestamptz not null default now(),
  expires_at     timestamptz not null,
  ended_at       timestamptz,
  ended_reason   text,
  constraint impersonation_window_ordered check (expires_at > started_at),
  -- Hard ceiling. Support access is a short errand, not a standing key.
  constraint impersonation_max_duration check (expires_at <= started_at + interval '4 hours')
);

create index impersonation_active_idx
  on core.impersonation_sessions (superadmin_id, expires_at)
  where ended_at is null;

comment on table core.impersonation_sessions is
  'Time-boxed, reason-recorded, tenant-visible. DPDP penalties reach INR 250 crore '
  'per contravention (PRD 13.3), so unlogged support access is not an option.';

create or replace function core.active_impersonation(p_user uuid default auth.uid())
returns core.impersonation_sessions
language sql
stable
security definer
set search_path = core, pg_catalog
as $fn$
  select s.*
    from core.impersonation_sessions s
   where s.superadmin_id = p_user
     and s.ended_at is null
     and s.expires_at > now()
   order by s.started_at desc
   limit 1;
$fn$;

-- ---------------------------------------------------------------------------
-- Usage metering
--
-- Feeds the superadmin margin view: revenue minus COGS per organisation. Makes
-- PRD 21.2's "model cost under 15% of subscription revenue" a number you watch
-- rather than a target you hope for.
-- ---------------------------------------------------------------------------

create table core.usage_events (
  id            bigint generated always as identity primary key,
  org_id        uuid not null references core.organisations(id) on delete cascade,
  product_id    uuid not null references core.products(id) on delete restrict,
  metric_key    text not null,
  quantity      numeric(20,6) not null,
  unit_cost_inr numeric(16,8) not null default 0,
  cost_inr      numeric(20,6) generated always as (quantity * unit_cost_inr) stored,
  occurred_at   timestamptz not null default now(),
  workspace_id  uuid,
  run_id        uuid,
  ref_json      jsonb not null default '{}'::jsonb,
  constraint usage_metric_key_format check (metric_key ~ '^[a-z][a-z0-9_.]{2,63}$'),
  constraint usage_quantity_non_negative check (quantity >= 0)
);

comment on column core.usage_events.metric_key is
  'model.tokens_in, model.tokens_out, meta.api_points, agent.run';

create index usage_events_org_time_idx on core.usage_events (org_id, occurred_at desc);
create index usage_events_run_idx      on core.usage_events (run_id) where run_id is not null;

create table core.usage_rollup_daily (
  day         date  not null,
  org_id      uuid  not null references core.organisations(id) on delete cascade,
  product_id  uuid  not null references core.products(id) on delete cascade,
  metric_key  text  not null,
  quantity    numeric(20,6) not null default 0,
  cost_inr    numeric(20,6) not null default 0,
  computed_at timestamptz not null default now(),
  primary key (day, org_id, product_id, metric_key)
);

-- Idempotent: safe to re-run for any day without double counting.
create or replace function core.rollup_usage_for_day(p_day date)
returns integer
language plpgsql
security definer
set search_path = core, pg_catalog
as $fn$
declare
  v_rows integer;
begin
  insert into core.usage_rollup_daily (day, org_id, product_id, metric_key, quantity, cost_inr, computed_at)
  select
    p_day,
    e.org_id,
    e.product_id,
    e.metric_key,
    sum(e.quantity),
    sum(e.cost_inr),
    now()
  from core.usage_events e
  where e.occurred_at >= p_day::timestamptz
    and e.occurred_at <  (p_day + 1)::timestamptz
  group by e.org_id, e.product_id, e.metric_key
  on conflict (day, org_id, product_id, metric_key) do update
    set quantity    = excluded.quantity,
        cost_inr    = excluded.cost_inr,
        computed_at = excluded.computed_at;

  get diagnostics v_rows = row_count;
  return v_rows;
end;
$fn$;

-- ---------------------------------------------------------------------------
-- Single audit entry point. Stamps the active impersonation automatically so a
-- caller cannot forget to attribute an impersonated action.
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
  v_id     bigint;
  v_imp_by uuid;
begin
  select s.superadmin_id
    into v_imp_by
    from core.impersonation_sessions s
   where s.superadmin_id = p_actor
     and s.ended_at is null
     and s.expires_at > now()
   limit 1;

  insert into core.audit_log (
    scope, event, org_id, workspace_id, actor_type, actor_id, impersonated_by,
    payload_json, run_id, policy_decision_id, approval_id, ip, user_agent
  )
  values (
    p_scope, p_event, p_org, p_workspace, p_actor_type, p_actor, v_imp_by,
    coalesce(p_payload, '{}'::jsonb), p_run_id, p_policy_id, p_approval_id, p_ip, p_user_agent
  )
  returning id into v_id;

  return v_id;
end;
$fn$;

-- ---------------------------------------------------------------------------
-- Row-level security
-- ---------------------------------------------------------------------------

alter table core.audit_log              enable row level security;
alter table core.impersonation_sessions enable row level security;
alter table core.usage_events           enable row level security;
alter table core.usage_rollup_daily     enable row level security;

-- Read your organisation's trail; the platform trail is superadmin-only.
create policy audit_log_read on core.audit_log
  for select to authenticated
  using (
    core.is_superadmin()
    or (org_id is not null and core.is_org_member(org_id) and scope <> 'platform')
  );

-- No insert policy for authenticated: writes go through core.log_audit
-- (SECURITY DEFINER) or the service role. Update and delete are blocked by
-- trigger regardless of grants.

-- The tenant must be able to see that they are being impersonated.
create policy impersonation_read on core.impersonation_sessions
  for select to authenticated
  using (core.is_superadmin() or core.is_org_member(org_id));

create policy impersonation_write on core.impersonation_sessions
  for all to authenticated
  using (core.is_superadmin()) with check (core.is_superadmin());

create policy usage_events_read on core.usage_events
  for select to authenticated
  using (core.is_org_member(org_id) or core.is_superadmin());

create policy usage_rollup_read on core.usage_rollup_daily
  for select to authenticated
  using (core.is_org_member(org_id) or core.is_superadmin());

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

grant select                 on core.audit_log              to authenticated;
grant select                 on core.impersonation_sessions to authenticated;
grant insert, update         on core.impersonation_sessions to authenticated;
grant select                 on core.usage_events           to authenticated;
grant select                 on core.usage_rollup_daily     to authenticated;

grant all on all tables    in schema core to service_role;
grant all on all sequences in schema core to service_role;

grant execute on function core.active_impersonation(uuid) to authenticated;
grant execute on function core.log_audit(
  core.audit_scope, text, uuid, uuid, core.actor_type, uuid, jsonb, uuid, uuid, uuid, inet, text
) to authenticated;
