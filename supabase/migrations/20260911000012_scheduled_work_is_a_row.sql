-- =============================================================================
-- Scheduled work leaves a row, and the row is what makes it idempotent
--
-- There is no scheduler in this repository. The 07:30 brief, the 20:30
-- business-truth prompt and the monitoring loop are a document. This is the
-- table the scheduler will be built on, and it is worth being explicit about
-- why the table comes first.
--
-- A cron-style scheduler holds its state in memory: "fire at 07:30". That is
-- wrong here in three separate ways, and each one produces a different bad day:
--
--   1. TWO REPLICAS RUN EVERY JOB TWICE. Coolify will run more than one
--      container the moment anyone scales, and two morning briefs means two
--      sets of proposals for the same account from the same data.
--
--   2. A MISSED WINDOW IS SILENTLY SKIPPED. If the process is restarting at
--      07:30 - which is exactly when a deploy is likely - the brief does not
--      run late, it does not run. Nothing records that it did not.
--
--   3. 07:30 IN WHOSE TIMEZONE? t_advit.workspaces.timezone exists and is
--      'Asia/Kolkata' for every real customer, but a scheduler firing on server
--      time sends the morning brief at 02:00 IST. This codebase has already had
--      that bug once, in the monthly cap, which compared against UTC
--      `current_date` while the workspace timezone column was read by nothing.
--
-- All three go away if the question stops being "what time is it" and becomes
-- "which workspaces have not had today's brief yet, in their own local day".
-- That is a query, its answer is the same on every replica, and a unique
-- constraint makes the second attempt a no-op rather than a duplicate.
-- =============================================================================

create table if not exists t_advit.job_runs (
  id           uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references t_advit.workspaces(id) on delete cascade,

  -- Free text rather than an enum, deliberately. A new job is a deploy, not a
  -- migration - and an enum here would mean the scheduler could not be extended
  -- without a schema change, which is the kind of friction that ends with
  -- somebody overloading an existing label.
  job          text not null,

  -- THE WORKSPACE'S OWN local date, not the server's. This is the column the
  -- unique constraint is built on, so it is also the definition of "today"
  -- that the whole scheduler runs on.
  local_date   date not null,

  started_at   timestamptz not null default now(),
  ended_at     timestamptz,
  status       text not null default 'running',
  -- What the job did, in whatever shape that job reports. A SyncReport, a run
  -- id, a refusal and its reason.
  detail_json  jsonb not null default '{}'::jsonb,

  constraint job_runs_status_valid
    check (status in ('running', 'completed', 'failed', 'skipped')),

  -- The idempotency, and the whole reason this table exists.
  --
  -- Two replicas racing to start the same job on the same local day: one
  -- INSERT wins, the other gets 23505 and stops. No lock to acquire, no lease
  -- to renew, no clock to agree on - and it holds across a restart, which an
  -- in-memory guard does not.
  constraint job_runs_once_per_local_day unique (workspace_id, job, local_date)
);

create index if not exists job_runs_workspace_idx
  on t_advit.job_runs (workspace_id, job, local_date desc);

-- Finding the work: which runs are still open, across every workspace.
create index if not exists job_runs_running_idx
  on t_advit.job_runs (status, started_at)
  where status = 'running';

comment on table t_advit.job_runs is
  'One row per (workspace, job, the workspace''s own local date). The unique '
  'constraint is the idempotency: a second replica attempting the same job gets '
  '23505 rather than running it twice, and that holds across a restart.';

comment on column t_advit.job_runs.local_date is
  'The date in the WORKSPACE''s timezone, not the server''s. A scheduler firing '
  'on server time sends the 07:30 brief at 02:00 IST - the same defect the '
  'monthly cap had before 20260911000002.';


-- ---------------------------------------------------------------------------
-- Which workspaces are due
--
-- A function rather than a query in Python, because the timezone arithmetic and
-- the "has it already run today" test have to agree exactly with the unique
-- constraint above. Two expressions of the same rule drift; one does not.
-- ---------------------------------------------------------------------------

create or replace function t_advit.workspaces_due(
  p_job         text,
  p_local_hour  integer,
  p_local_minute integer default 0
)
returns table (
  workspace_id uuid,
  org_id       uuid,
  timezone     text,
  local_date   date,
  local_time   time
)
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
       -- that is not paying.
       and core.access_mode(w.org_id) = 'full'
  )
  select l.id, l.org_id, l.timezone, l.local_date, l.local_time
    from local l
   where l.local_time >= make_time(p_local_hour, p_local_minute, 0)
     -- `not exists`, not `last_run < today`. A run that is still 'running'
     -- must also count as done, or a job that takes four minutes gets started
     -- again by the next tick.
     and not exists (
           select 1 from t_advit.job_runs r
            where r.workspace_id = l.id
              and r.job = p_job
              and r.local_date = l.local_date
         )
   order by l.id;
$fn$;

revoke execute on function t_advit.workspaces_due(text, integer, integer) from public;
grant execute on function t_advit.workspaces_due(text, integer, integer) to advit_backend;

comment on function t_advit.workspaces_due(text, integer, integer) is
  'Workspaces whose own local clock has passed the target and which have no '
  'job_runs row for today. Late rather than skipped: a process that was down at '
  '07:30 runs the brief at 07:50 instead of not running it, because the '
  'question is "has today''s brief happened" rather than "is it 07:30 now".';


-- ---------------------------------------------------------------------------
-- A job that died leaves a row saying 'running' for ever
--
-- The process is killed mid-deploy, the row is never closed, and the unique
-- constraint then blocks that workspace's brief until the date rolls over.
-- Silent, and exactly one day long, which is the hardest kind of outage to
-- notice.
-- ---------------------------------------------------------------------------

create or replace function t_advit.reap_abandoned_jobs(p_older_than interval default '1 hour')
returns integer
language sql
security definer
set search_path = t_advit, pg_catalog
as $fn$
  with reaped as (
    update t_advit.job_runs
       set status = 'failed',
           ended_at = now(),
           detail_json = detail_json || jsonb_build_object(
             'reaped', true,
             'reason', 'still running after ' || p_older_than::text
                       || '; the process that started it did not finish'
           )
     where status = 'running'
       and started_at < now() - p_older_than
    returning 1
  )
  select count(*)::integer from reaped;
$fn$;

revoke execute on function t_advit.reap_abandoned_jobs(interval) from public;
grant execute on function t_advit.reap_abandoned_jobs(interval) to advit_backend;

comment on function t_advit.reap_abandoned_jobs(interval) is
  'Closes job_runs rows left ''running'' by a process that died. Marks them '
  'failed rather than deleting them: that the job was attempted and did not '
  'finish is the fact worth keeping, and deleting the row would let the next '
  'tick retry silently as though nothing had happened.';


-- ---------------------------------------------------------------------------
-- Grants and policies
--
-- 20260911000007 looped over every table that existed AT THAT MOMENT and gave
-- advit_backend a grant and a permissive policy. A table created afterwards has
-- neither, so the service path gets `permission denied for table job_runs` -
-- which is exactly what happened the first time the scheduler ticked, and
-- exactly what that migration's own comment said would happen:
--
--     "a t_advit table added later without a policy fails the service path
--      LOUDLY rather than silently widening it"
--
-- Loudly, at the first attempt, with the table name in the message. That is the
-- maintenance cost of refusing BYPASSRLS, paid here, and it is the cheap half
-- of the trade.
-- ---------------------------------------------------------------------------

alter table t_advit.job_runs enable row level security;

grant select, insert, update on t_advit.job_runs to advit_backend;

drop policy if exists advit_backend_all on t_advit.job_runs;
create policy advit_backend_all on t_advit.job_runs
  for all to advit_backend using (true) with check (true);

-- Tenants READ their own, and write none.
--
-- "Did this morning's brief run, and what did it find?" is a question an owner
-- should be able to answer from the product. Writing is another matter: a
-- tenant that could insert a job_runs row could claim today's brief had already
-- happened and suppress it, and one that could update a row could rewrite what
-- the sync reported. Both are the same shape as the SELECT-only grants on
-- runs, decisions and actions.
grant select on t_advit.job_runs to authenticated;

drop policy if exists job_runs_select on t_advit.job_runs;
create policy job_runs_select on t_advit.job_runs
  for select to authenticated
  using (t_advit.is_workspace_member(workspace_id));
