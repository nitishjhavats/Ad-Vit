-- =============================================================================
-- Bring-your-own-key, per organisation, and a model tier per function
--
-- The product is sold on the customer supplying their own OpenRouter key: they
-- see their own spend, they set their own rate limits, and this business does
-- not resell tokens. None of that exists. `ModelRouter` reads ONE key from
-- `Settings.openrouter_api_key`, so every organisation's calls are billed to
-- whichever key the process was started with - which is both the wrong
-- commercial model and, at scale, one revoked key away from taking every tenant
-- down at once.
--
-- t_advit.secrets already exists for envelope-encrypted material, and is
-- WORKSPACE-scoped. The OpenRouter key is not: an organisation buys one and
-- uses it across every workspace it owns, which is how the customer thinks
-- about it and how their OpenRouter dashboard reports it. So this is a new
-- table in `core` rather than a new `kind` in the old one.
-- =============================================================================

create table if not exists core.org_secrets (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references core.organisations(id) on delete cascade,

  -- 'openrouter_api_key' today. Free text so a second provider does not need a
  -- migration, and constrained by the writer rather than by an enum nobody can
  -- extend without one.
  kind        text not null,

  -- AES-256-GCM. The nonce is stored beside the ciphertext because it must be
  -- unique per encryption and is not secret; the AAD is not stored at all,
  -- because it is DERIVED from (org_id, kind) at decryption time. That is the
  -- important part: a row copied from one organisation to another will not
  -- decrypt, so somebody with write access to this table cannot make tenant B
  -- spend tenant A's OpenRouter credit.
  nonce       bytea not null,
  ciphertext  bytea not null,

  -- Which master key encrypted it. Rotation writes new rows at version+1 and
  -- leaves the old ones readable until they are re-encrypted; without this a
  -- rotation is a migration that must not be interrupted.
  key_version integer not null default 1,

  -- The last four characters, in clear, so the UI can show `sk-or-…bc61` and
  -- the owner can tell which of their keys this is without the system ever
  -- having to decrypt one to render a settings page.
  hint        text,

  -- Whether the key has been used successfully since it was stored. A key that
  -- has never worked and a key that has stopped working are different problems
  -- and the owner needs to be told which.
  last_verified_at timestamptz,
  last_error       text,

  created_at  timestamptz not null default now(),
  created_by  uuid references core.platform_users(id) on delete set null,
  rotated_at  timestamptz,

  constraint org_secrets_one_per_kind unique (org_id, kind),
  constraint org_secrets_hint_is_short check (hint is null or length(hint) <= 8)
);

comment on table core.org_secrets is
  'Per-organisation credentials the customer supplies. Encrypted with AES-256-GCM '
  'whose AAD is derived from (org_id, kind), so a row moved between '
  'organisations does not decrypt - which is what stops a write to this table '
  'becoming a way to spend another tenant''s OpenRouter credit.';

comment on column core.org_secrets.hint is
  'The last few characters, in clear. A settings page can show which key is '
  'stored without the server ever decrypting one to render it.';

alter table core.org_secrets enable row level security;

-- NOBODY reads the ciphertext over PostgREST. Not the owner, not a superadmin.
--
-- There is no legitimate reason for a browser to receive it: the key is used by
-- the runtime on the service connection and shown to humans only as `hint`. A
-- SELECT policy with a column list is not a thing PostgreSQL has, so the
-- correct answer is no tenant-facing SELECT at all, and a view below that
-- exposes exactly the columns that are safe.
grant select, insert, update on core.org_secrets to advit_backend;

drop policy if exists advit_backend_all on core.org_secrets;
create policy advit_backend_all on core.org_secrets
  for all to advit_backend using (true) with check (true);

create or replace view core.org_secret_status
with (security_invoker = true) as
  select s.org_id,
         s.kind,
         s.hint,
         s.key_version,
         s.created_at,
         s.rotated_at,
         s.last_verified_at,
         s.last_error,
         (s.last_verified_at is not null) as is_working
    from core.org_secrets s;

comment on view core.org_secret_status is
  'What a settings page may see: that a key exists, its last few characters, '
  'and whether it has ever worked. Never the key. `security_invoker` so the '
  'caller''s own RLS decides which organisations appear.';

-- The view is security_invoker, so it needs a policy on the base table for the
-- caller. One that exposes no ciphertext is impossible in PostgreSQL, so the
-- view is granted directly and the base table stays unreadable: a grant on a
-- security_invoker view still requires the caller to pass the base table's RLS,
-- which is why a policy is needed - it just must never be used to SELECT *.
drop policy if exists org_secrets_status_read on core.org_secrets;
create policy org_secrets_status_read on core.org_secrets
  for select to authenticated
  using (core.is_org_member(org_id));

grant select (org_id, kind, hint, key_version, created_at, rotated_at,
              last_verified_at, last_error)
  on core.org_secrets to authenticated;
grant select on core.org_secret_status to authenticated;


-- ---------------------------------------------------------------------------
-- Which model tier each function uses, per organisation
--
-- config/routing.yaml routes by TASK CLASS, which is right and is not what the
-- customer asked for. They want to choose per FUNCTION - video analysis,
-- brainstorming, strategy, net search, memory, daily learning, chat, campaign
-- and ad set creation - between a best option, a value option and a cheap one,
-- with the recommendation marked.
--
-- The catalogue of what each tier resolves to stays in routing.yaml, where it
-- can be corrected against OpenRouter's /models endpoint without a migration.
-- This table holds only the CHOICE.
-- ---------------------------------------------------------------------------

do $$ begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                  where n.nspname = 't_advit' and t.typname = 'model_tier') then
    create type t_advit.model_tier as enum ('best', 'value', 'cheap');
  end if;
end $$;

create table if not exists t_advit.model_preferences (
  org_id     uuid not null references core.organisations(id) on delete cascade,

  -- The role names in config/routing.yaml. Free text for the same reason
  -- job_runs.job is: a new agent is a deploy, not a migration.
  role       text not null,
  tier       t_advit.model_tier not null,

  set_at     timestamptz not null default now(),
  set_by     uuid references core.platform_users(id) on delete set null,

  primary key (org_id, role)
);

comment on table t_advit.model_preferences is
  'Which tier an organisation chose for each function. ABSENT means "use the '
  'recommended tier from routing.yaml" - deliberately, so the recommendation '
  'can be improved for every customer who has not overridden it without '
  'touching a single row.';

alter table t_advit.model_preferences enable row level security;

grant select, insert, update on t_advit.model_preferences to advit_backend;
grant select, insert, update, delete on t_advit.model_preferences to authenticated;

drop policy if exists advit_backend_all on t_advit.model_preferences;
create policy advit_backend_all on t_advit.model_preferences
  for all to advit_backend using (true) with check (true);

-- Owners and admins choose; a member reads. Spending more per call is a
-- commercial decision about the organisation's own OpenRouter bill.
drop policy if exists model_preferences_read on t_advit.model_preferences;
create policy model_preferences_read on t_advit.model_preferences
  for select to authenticated
  using (core.is_org_member(org_id));

drop policy if exists model_preferences_write on t_advit.model_preferences;
create policy model_preferences_write on t_advit.model_preferences
  for all to authenticated
  using (core.has_org_role(org_id, array['owner','admin']::core.org_role[]))
  with check (core.has_org_role(org_id, array['owner','admin']::core.org_role[]));


-- ---------------------------------------------------------------------------
-- Both writes name who made them
-- ---------------------------------------------------------------------------

create or replace function core.stamp_org_secret()
returns trigger
language plpgsql
security definer
set search_path = core, pg_catalog
as $fn$
begin
  -- From the session, not the payload. Same rule as approvals.responded_by,
  -- business_truth.entered_by and entitlement_overrides.set_by.
  if tg_op = 'INSERT' then
    new.created_by := auth.uid();
  else
    new.rotated_at := now();
  end if;

  perform core.log_audit(
    'organisation'::core.audit_scope,
    case when tg_op = 'INSERT' then 'org_secret.stored' else 'org_secret.rotated' end,
    p_org        => new.org_id,
    p_actor_type => (case when auth.uid() is null then 'system' else 'user' end)::core.actor_type,
    p_actor      => auth.uid(),
    -- The hint, never the key, and never the ciphertext. An audit trail that
    -- carried the material would be a second copy of the secret in a table
    -- designed to be read widely and never deleted.
    p_payload    => jsonb_build_object('kind', new.kind, 'hint', new.hint)
  );
  return new;
end;
$fn$;

revoke execute on function core.stamp_org_secret() from public;

drop trigger if exists org_secrets_stamp on core.org_secrets;
create trigger org_secrets_stamp
  before insert or update on core.org_secrets
  for each row execute function core.stamp_org_secret();


create or replace function t_advit.stamp_model_preference()
returns trigger
language plpgsql
security definer
set search_path = t_advit, core, pg_catalog
as $fn$
begin
  new.set_by := auth.uid();
  new.set_at := now();
  return new;
end;
$fn$;

revoke execute on function t_advit.stamp_model_preference() from public;

drop trigger if exists model_preferences_stamp on t_advit.model_preferences;
create trigger model_preferences_stamp
  before insert or update on t_advit.model_preferences
  for each row execute function t_advit.stamp_model_preference();
