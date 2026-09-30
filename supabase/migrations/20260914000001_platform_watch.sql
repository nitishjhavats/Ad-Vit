-- =============================================================================
-- Platform Watch: the agent that checks whether the rules are still true
--
-- PRD 13.5. Every compliance rule and every platform-knowledge row carries a
-- source_url and an as_of date, and app/policy/rules.py already reports a rule
-- as STALE once its as_of falls outside a freshness window - 90 days for Meta,
-- whose advertising policy changed materially in January and again in March
-- 2026, and 365 for a primary statute, which does not.
--
-- Nothing ever refreshed one. Today, 2026-09-14, every Meta rule in the seed is
-- dated 2026-03-01: six and a half months old, past its window since the start
-- of June, and the staleness was visible only to somebody reading the loader's
-- return value. The comment in rules.py says "Platform Watch is what should keep
-- it fresh; until that agent exists, the staleness is at least visible." This is
-- that agent.
--
-- What it does, and what it deliberately does not:
--
--   It DETECTS. It fetches each source page and compares a content hash against
--   the last one it saw; it reports rules and knowledge past their windows; it
--   records what it could not reach. Each of those becomes a FINDING for a
--   human.
--
--   It NEVER ACTS. A changed source page becomes a row a superadmin reads, not
--   an edit to the rule. A compliance rule that a website edit could rewrite is
--   a compliance gate that a website edit could open, and BLOCK rules carry
--   statutory weight under the Drugs and Magic Remedies Act and Schedule J.
--   That is the suggest-do-not-implement contract, applied to the platform's
--   own knowledge.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- 1. A job that has no workspace.
--
-- The scheduler is strictly per-workspace: job_runs.workspace_id is NOT NULL
-- with a foreign key, and `workspaces_due` is the only thing that decides what
-- runs. Platform Watch is about the platform's own rules, which belong to no
-- tenant. Rather than a second table with a second claim mechanism, the column
-- becomes nullable and a partial unique index gives platform jobs the same
-- once-per-day guarantee the INSERT-as-claim design already provides - decided
-- by the constraint, identically on every replica, surviving a restart.
-- ---------------------------------------------------------------------------
alter table t_advit.job_runs alter column workspace_id drop not null;

create unique index job_runs_platform_once_per_day
  on t_advit.job_runs (job, local_date)
  where workspace_id is null;

comment on index t_advit.job_runs_platform_once_per_day is
  'The once-per-day claim for jobs that belong to the platform rather than to a '
  'workspace. Same mechanism as job_runs_once_per_local_day: the INSERT is the '
  'claim, and the index decides.';


-- ---------------------------------------------------------------------------
-- 2. What each source looked like the last time anybody checked.
--
-- One row per distinct source_url across policy_rules and platform_knowledge.
-- The hash is of the page's text with scripts, styles and whitespace stripped -
-- crude, and stated as crude. A page that changes daily will produce a finding
-- daily; deduplication on the finding side (below) means it produces ONE open
-- finding, and a human who acknowledges it and sees it return tomorrow has
-- learned that the page is dynamic, which is itself worth knowing.
-- ---------------------------------------------------------------------------
create table t_advit.watched_sources (
  url             text primary key,
  content_hash    text,
  content_bytes   integer,
  last_status     integer,
  last_error      text,
  last_fetched_at timestamptz,
  last_changed_at timestamptz,
  first_seen_at   timestamptz not null default now()
);

comment on table t_advit.watched_sources is
  'The last observed state of every source_url a rule or a knowledge row cites. '
  'Backend-only: no tenant has any business knowing when the platform last '
  'checked its own sources.';

alter table t_advit.watched_sources enable row level security;
create policy advit_backend_all on t_advit.watched_sources
  for all to advit_backend using (true) with check (true);
grant select, insert, update on t_advit.watched_sources to advit_backend;


-- ---------------------------------------------------------------------------
-- 3. The findings: the superadmin's inbox.
--
-- There is no notification infrastructure in this repository - no email, no
-- push - and inventing one for this would be a second product. A table the
-- superadmin console reads IS the notification, and core.log_audit at platform
-- scope puts the same fact in the trail.
--
-- Deduplicated on (kind, subject) while unacknowledged, so a stale rule is one
-- open finding rather than one per day, and acknowledging it closes it until
-- the condition recurs.
-- ---------------------------------------------------------------------------
create table t_advit.watch_findings (
  id              uuid primary key default gen_random_uuid(),
  detected_at     timestamptz not null default now(),
  kind            text not null,
  -- The rule code, the knowledge row id, or the URL - whatever the finding is
  -- ABOUT. Free text rather than a foreign key because the three kinds point at
  -- three different things, and a finding about a URL that no rule cites any
  -- more is still a finding.
  subject         text not null,
  source_url      text,
  severity        text not null,
  detail_json     jsonb not null default '{}'::jsonb,
  acknowledged_by uuid references core.platform_users(id),
  acknowledged_at timestamptz,

  constraint watch_findings_kind_valid check (
    kind in ('source_changed', 'rule_stale', 'knowledge_stale', 'fetch_failed')
  ),
  constraint watch_findings_severity_valid check (
    severity in ('info', 'review', 'urgent')
  ),
  -- Acknowledging is one act, not two halves.
  constraint watch_findings_ack_complete check (
    (acknowledged_by is null) = (acknowledged_at is null)
  )
);

create unique index watch_findings_open_unique
  on t_advit.watch_findings (kind, subject)
  where acknowledged_at is null;

comment on table t_advit.watch_findings is
  'What Platform Watch found and a human has not yet looked at. One open row per '
  '(kind, subject). Written by the jobs process; read and acknowledged by a '
  'superadmin; visible to no tenant.';

alter table t_advit.watch_findings enable row level security;

create policy advit_backend_all on t_advit.watch_findings
  for all to advit_backend using (true) with check (true);

-- Superadmins read and acknowledge. Nothing else: a finding is written by the
-- watcher and closed by a person, and neither side gets to do the other's job.
create policy watch_findings_superadmin_read on t_advit.watch_findings
  for select to authenticated using (core.is_superadmin());

create policy watch_findings_superadmin_ack on t_advit.watch_findings
  for update to authenticated
  using (core.is_superadmin())
  with check (core.is_superadmin() and acknowledged_by = auth.uid());

grant select, update (acknowledged_by, acknowledged_at) on t_advit.watch_findings to authenticated;
grant select, insert, update on t_advit.watch_findings to advit_backend;
