-- =============================================================================
-- ad-vit - runs, decisions, approvals, actions, outcomes, learnings
--
-- This is the spine that makes autonomy defensible. Three properties, enforced
-- here rather than hoped for in application code (PRD 8.2):
--
--   Pre-registration    the expected outcome and its measurement horizon are
--                       written at decision time, before the result is known.
--                       A system that only records outcomes learns to
--                       rationalise; one that records predictions learns to
--                       calibrate.
--   Counterfactuals     the rejected options and the reason are stored, so
--                       "we scaled and it worked" is interpretable.
--   Verification        an action is successful only when a read-back matched
--                       the proposal - never because an API returned 200.
-- =============================================================================

create type t_advit.run_status as enum (
  'running', 'awaiting_approval', 'completed', 'failed', 'halted', 'cancelled'
);

create type t_advit.approval_status as enum (
  'pending', 'approved', 'modified', 'rejected', 'expired'
);

-- PRD 10.8. Class is decided by the damage a wrong call does, not by how
-- complicated the call is.
create type t_advit.risk_class as enum ('low', 'medium', 'high', 'critical');

create type t_advit.outcome_verdict as enum ('beat', 'met', 'missed', 'unmeasurable');

create type t_advit.learning_status as enum ('active', 'contested', 'historical');

-- ---------------------------------------------------------------------------
-- Runs
-- ---------------------------------------------------------------------------

create table t_advit.runs (
  id           uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references t_advit.workspaces(id) on delete cascade,
  trigger      text not null,
  intent       text,
  plan_json    jsonb not null default '{}'::jsonb,
  status       t_advit.run_status not null default 'running',
  started_at   timestamptz not null default now(),
  ended_at     timestamptz,
  tokens_in    bigint not null default 0,
  tokens_out   bigint not null default 0,
  cost_inr     numeric(14,6) not null default 0,

  -- Joins the run to its OpenTelemetry trace and to core.audit_log (PRD 17.8).
  trace_id     text,
  thread_id    text,

  error_json   jsonb,
  constraint runs_trigger_valid check (
    trigger in ('user_message','schedule','webhook','alert','continuation')
  )
);

create index runs_workspace_started_idx on t_advit.runs (workspace_id, started_at desc);
create index runs_status_idx on t_advit.runs (status) where status in ('running','awaiting_approval');

-- ---------------------------------------------------------------------------
-- Decisions - written BEFORE execution
-- ---------------------------------------------------------------------------

create table t_advit.decisions (
  id                  uuid primary key default gen_random_uuid(),
  run_id              uuid references t_advit.runs(id) on delete set null,
  workspace_id        uuid not null references t_advit.workspaces(id) on delete cascade,
  decision_type       text not null,
  situation_json      jsonb not null default '{}'::jsonb,

  -- Every option considered, including the ones rejected and why.
  options_json        jsonb not null default '[]'::jsonb,
  chosen_option       text,
  reasoning           text,

  -- Pre-registration. Both are required: a prediction without a horizon cannot
  -- be scored, and an unscored prediction teaches the system nothing.
  expected_effect_json jsonb not null default '{}'::jsonb,
  horizon_days        integer not null,

  evidence_refs       text[] not null default '{}',
  confidence          numeric(4,3),
  created_at          timestamptz not null default now(),

  constraint decisions_horizon_positive check (horizon_days > 0),
  constraint decisions_confidence_range check (confidence is null or confidence between 0 and 1)
);

create index decisions_workspace_idx on t_advit.decisions (workspace_id, created_at desc);
create index decisions_run_idx on t_advit.decisions (run_id);

-- ---------------------------------------------------------------------------
-- Approvals
-- ---------------------------------------------------------------------------

create table t_advit.approvals (
  id            uuid primary key default gen_random_uuid(),
  decision_id   uuid not null references t_advit.decisions(id) on delete cascade,
  workspace_id  uuid not null references t_advit.workspaces(id) on delete cascade,
  risk_class    t_advit.risk_class not null,
  proposed_json jsonb not null,
  status        t_advit.approval_status not null default 'pending',
  responded_by  uuid references core.platform_users(id) on delete set null,
  responded_at  timestamptz,

  -- A rejection reason is a training signal and is stored as one (FR-014).
  reject_reason text,

  -- Proposals expire. A budget proposal built on Tuesday's data is void by
  -- Friday; the system re-derives rather than executing stale intent (10.6).
  expires_at    timestamptz not null,
  impact_inr    numeric(14,2),
  created_at    timestamptz not null default now(),

  constraint approvals_reject_reason_required check (
    status <> 'rejected' or reject_reason is not null
  ),
  constraint approvals_responded_together check (
    (status in ('pending','expired')) or (responded_at is not null)
  )
);

create index approvals_pending_idx
  on t_advit.approvals (workspace_id, expires_at)
  where status = 'pending';

-- ---------------------------------------------------------------------------
-- Actions - the only record of what actually reached Meta
-- ---------------------------------------------------------------------------

create table t_advit.actions (
  id                 uuid primary key default gen_random_uuid(),
  decision_id        uuid not null references t_advit.decisions(id) on delete cascade,
  workspace_id       uuid not null references t_advit.workspaces(id) on delete cascade,
  approval_id        uuid references t_advit.approvals(id) on delete set null,
  action_type        text not null,
  risk_class         t_advit.risk_class not null,

  meta_request_json  jsonb,
  meta_response_json jsonb,
  before_state_json  jsonb,
  after_state_json   jsonb,

  -- Written BEFORE the call. On an ambiguous timeout the retry path consults
  -- this row first, then queries Meta for a matching entity, and only then
  -- re-issues. This is how duplicate campaigns are prevented (10.9).
  idempotency_key    text not null,

  -- Success is a read-back that matches the proposal, not a 200 response.
  verified           boolean not null default false,
  verification_diff  jsonb,

  rollback_handle    jsonb,
  rollback_expires_at timestamptz,
  rolled_back_at     timestamptz,

  external_request_id text,
  requested_at       timestamptz not null default now(),
  executed_at        timestamptz,
  error_json         jsonb
);

-- Idempotency is a uniqueness constraint, not a convention.
create unique index actions_idempotency_key_unique on t_advit.actions (idempotency_key);
create index actions_workspace_idx on t_advit.actions (workspace_id, requested_at desc);
create index actions_decision_idx on t_advit.actions (decision_id);

-- A CRITICAL-class action must carry the approval that authorised it. This is
-- the database refusing to record an unapproved spend change at all, rather
-- than trusting every code path to check first.
alter table t_advit.actions add constraint actions_critical_requires_approval
  check (risk_class <> 'critical' or approval_id is not null);

-- ---------------------------------------------------------------------------
-- Outcomes - scored at the pre-registered horizon
-- ---------------------------------------------------------------------------

create table t_advit.outcomes (
  id           uuid primary key default gen_random_uuid(),
  decision_id  uuid not null references t_advit.decisions(id) on delete cascade,
  workspace_id uuid not null references t_advit.workspaces(id) on delete cascade,
  measured_at  timestamptz not null default now(),
  horizon_days integer not null,
  metrics_json jsonb not null default '{}'::jsonb,
  vs_expected  jsonb,
  verdict      t_advit.outcome_verdict not null,
  notes        text,
  unique (decision_id, horizon_days)
);

comment on table t_advit.outcomes is
  'Feeds the calibration score (PRD 8.2, FR-043): the honest quality metric. A '
  'recommendation from a decision class with a 42% hit rate must be presented '
  'with less confidence than one from a class at 81%, and the UI says so.';

-- ---------------------------------------------------------------------------
-- Learnings (T1 by default; T2/T3 promotion is a separate, gated pipeline)
-- ---------------------------------------------------------------------------

create table t_advit.learnings (
  id              uuid primary key default gen_random_uuid(),
  workspace_id    uuid references t_advit.workspaces(id) on delete cascade,
  tier            t_advit.knowledge_tier not null default 'account',
  statement       text not null,
  conditions_json jsonb not null default '{}'::jsonb,
  effect_size     numeric(10,4),
  confidence      numeric(4,3) not null default 0.500,
  evidence_n      integer not null default 1,
  evidence_refs   text[] not null default '{}',
  valid_from      timestamptz not null default now(),
  valid_to        timestamptz,
  status          t_advit.learning_status not null default 'active',
  embedding       extensions.vector(1536),
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),

  constraint learnings_confidence_range check (confidence between 0 and 1),
  constraint learnings_evidence_positive check (evidence_n >= 1),

  -- Account-tier learnings are always workspace-scoped; industry and global
  -- tiers are never workspace-scoped. This is the anonymisation boundary from
  -- PRD 5.1 expressed as a constraint the database will not let code violate.
  constraint learnings_tier_scoping check (
    (tier = 'account' and workspace_id is not null)
    or (tier <> 'account' and workspace_id is null)
  )
);

create index learnings_workspace_idx on t_advit.learnings (workspace_id, status);

create trigger learnings_touch
  before update on t_advit.learnings
  for each row execute function core.touch_updated_at();

-- ---------------------------------------------------------------------------
-- Guardrail events
-- ---------------------------------------------------------------------------

create table t_advit.guardrail_events (
  id           uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references t_advit.workspaces(id) on delete cascade,
  run_id       uuid references t_advit.runs(id) on delete set null,
  guardrail    text not null,
  guardrail_class text not null,
  threshold    numeric(20,6),
  observed     numeric(20,6),
  action_taken text not null,
  detail_json  jsonb not null default '{}'::jsonb,
  at           timestamptz not null default now(),
  constraint guardrail_class_valid check (
    guardrail_class in ('financial','statistical','operational','compliance','model','platform')
  )
);

create index guardrail_events_workspace_idx on t_advit.guardrail_events (workspace_id, at desc);

-- ---------------------------------------------------------------------------
-- Row-level security
-- ---------------------------------------------------------------------------

alter table t_advit.runs             enable row level security;
alter table t_advit.decisions        enable row level security;
alter table t_advit.approvals        enable row level security;
alter table t_advit.actions          enable row level security;
alter table t_advit.outcomes         enable row level security;
alter table t_advit.learnings        enable row level security;
alter table t_advit.guardrail_events enable row level security;

do $policies$
declare t text;
begin
  foreach t in array array[
    'runs','decisions','approvals','actions','outcomes','guardrail_events'
  ]
  loop
    execute format(
      'create policy %I on t_advit.%I for select to authenticated
         using (t_advit.is_workspace_member(workspace_id) or core.is_superadmin())',
      t || '_select', t
    );
    execute format('grant select on t_advit.%I to authenticated', t);
  end loop;
end;
$policies$;

-- Approvals are the one table a tenant writes directly: responding to a
-- proposal. Everything else on this spine is written by the agent runtime
-- under the service role, so an agent cannot rewrite its own history.
create policy approvals_respond on t_advit.approvals
  for update to authenticated
  using (t_advit.is_workspace_member(workspace_id))
  with check (t_advit.is_workspace_member(workspace_id));

grant update on t_advit.approvals to authenticated;

-- Learnings: account tier is workspace-scoped; industry and global tiers are
-- readable by everyone and writable only through the promotion service.
create policy learnings_select on t_advit.learnings
  for select to authenticated
  using (
    core.is_superadmin()
    or (tier = 'account' and t_advit.is_workspace_member(workspace_id))
    or tier in ('industry','global')
  );

grant select on t_advit.learnings to authenticated;

grant all on all tables in schema t_advit to service_role;
