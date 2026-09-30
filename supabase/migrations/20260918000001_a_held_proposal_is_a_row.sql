-- =============================================================================
-- A held proposal is a row
--
-- The CTA gate (app/orchestrator/cta_gate.py) holds any proposal that would
-- build a campaign or an ad set while no owner-asserted destination is on
-- file. The turn ends ASKED with one argued question, no decision row is
-- written, and nothing reaches the approval gate. That is right: a campaign
-- built before the owner has said where it sends people has spent its first
-- budget on a question nobody answered.
--
-- What was wrong is where the hold lived. It existed in the run state of the
-- turn that raised it and in that turn's chat response, and nowhere else. The
-- reports route answered `held: null, reason: "not persisted"` - honestly, but
-- an owner who closed the chat had no way to find the question again, and
-- the settings page could not say "answering this releases a proposal".
--
-- A question the owner has not answered is a fact about the account, not
-- about one chat turn. So it is a row here: what was proposed, the question
-- that held it, why, and what the CTA model would recommend - and, when the
-- owner answers, how it was resolved.
--
-- Who writes it, and why that is the backend and not the tenant:
--
--   The row is the system's record of its own question. `authenticated`
--   holds SELECT so a member can read it on the Suggestions tab, and nothing
--   else - the same grant shape as t_advit.decisions - so a tenant cannot edit,
--   invent or close the system's record of what it asked. Resolution follows
--   from the owner's own act (PUT .../cta writes account_context on the tenant
--   connection), and the backend records that consequence, in the same way
--   compute_blended_daily records the consequence of a business-truth report.
-- =============================================================================

create table t_advit.held_proposals (
  id                  uuid primary key default gen_random_uuid(),
  workspace_id        uuid not null references t_advit.workspaces(id) on delete cascade,
  -- The run that raised the question. Nullable and set-null on delete: the
  -- question outlives the turn, which is the point of the table.
  run_id              uuid references t_advit.runs(id) on delete set null,
  -- The strategy model's proposal, as it said it: goal, assumptions, options
  -- with their actions, the recommended option. Not the executed shape - the
  -- destination is exactly what it lacks - so nothing downstream may build
  -- from it directly. When the owner answers, the next turn re-proposes with
  -- the destination written into the action.
  proposal_json       jsonb not null,
  question            text not null,
  reason              text not null,
  -- The CTA model's own recommendation and its reasoning, so the owner is
  -- choosing between argued options rather than answering a blank.
  recommendation_json jsonb,
  held_at             timestamptz not null default now(),
  resolved_at         timestamptz,
  -- 'cta_set': the owner answered. 'superseded': a newer hold replaced it -
  -- the owner asked again without answering, and the newest question is the
  -- live one. 'expired' is in the vocabulary so a row closed by a scheduled
  -- sweep stays distinguishable from one the owner answered; no sweep is part
  -- of this migration.
  resolved_by         text check (resolved_by in ('cta_set', 'superseded', 'expired')),

  constraint held_proposals_resolution_complete check (
    (resolved_at is null) = (resolved_by is null)
  )
);

-- One open question per workspace. The newest hold supersedes: the writer
-- resolves the previous open row as 'superseded' before inserting, and this
-- index refuses the insert if it did not.
create unique index held_proposals_one_open_per_workspace
  on t_advit.held_proposals (workspace_id)
  where resolved_at is null;

create index held_proposals_workspace_held_at
  on t_advit.held_proposals (workspace_id, held_at desc);

comment on table t_advit.held_proposals is
  'A proposal the CTA gate held because no owner-asserted destination was on '
  'file. A question the owner has not answered is a fact about the account, '
  'not about one chat turn, so it is a row: written by the orchestrator, read '
  'by workspace members, resolved by the backend when the owner answers.';

comment on column t_advit.held_proposals.proposal_json is
  'The strategy model''s proposal as it said it, without a destination. Not '
  'buildable as-is; the next turn after the owner answers re-proposes with the '
  'CTA written into the action.';

comment on column t_advit.held_proposals.resolved_by is
  'cta_set: the owner answered. superseded: a newer hold replaced this one. '
  'expired: closed by a sweep rather than by an answer.';

comment on index t_advit.held_proposals_one_open_per_workspace is
  'At most one open question per workspace. The newest supersedes; the writer '
  'resolves the previous row first and the index decides if it did not.';

-- ---------------------------------------------------------------------------
-- Row-level security and grants, mirroring the decisions spine
-- (20260903000007, 20260911000007): members read their own workspace's rows;
-- the backend writes and resolves; a tenant cannot write at all.
-- ---------------------------------------------------------------------------

alter table t_advit.held_proposals enable row level security;

create policy advit_backend_all on t_advit.held_proposals
  for all to advit_backend using (true) with check (true);

create policy held_proposals_select on t_advit.held_proposals
  for select to authenticated
  using (t_advit.is_workspace_member(workspace_id) or core.is_superadmin());

grant select on t_advit.held_proposals to authenticated;
grant select, insert, update on t_advit.held_proposals to advit_backend;

comment on policy held_proposals_select on t_advit.held_proposals is
  'A member may read the question the system is holding for their workspace. '
  'SELECT is the only grant authenticated holds: the row is the system''s '
  'record of its own question, and answering it is done through account_context, '
  'not by editing this table.';
