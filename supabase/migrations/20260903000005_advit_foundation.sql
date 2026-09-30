-- =============================================================================
-- ad-vit - workspaces, connections, account context
--
-- The product schema depends on core; core must never depend on it. Tenancy
-- nests: core.organisations -> t_advit.workspaces -> Meta ad accounts.
-- The workspace remains the isolation boundary the PRD specifies (3.5, 17.4).
-- =============================================================================

create extension if not exists vector with schema extensions;

create schema if not exists t_advit;

comment on schema t_advit is
  'ad-vit product schema. Sits on the Common SaaS Core for identity, '
  'RBAC, entitlements and billing - it does not reimplement any of them.';

-- ---------------------------------------------------------------------------
-- Types
-- ---------------------------------------------------------------------------

create type t_advit.business_type as enum ('ayurveda', 'general_d2c');

-- Product roles beneath core's Admin (PRD 3.5 role matrix).
create type t_advit.workspace_role as enum (
  'marketing_manager',
  'media_buyer',
  'analyst',
  'platform_steward'
);

create type t_advit.connection_health as enum ('healthy', 'degraded', 'unhealthy', 'unknown');

create type t_advit.entity_status as enum ('active', 'paused', 'deleted', 'archived', 'pending');

-- Memory tiers. T1 account / T2 industry / T3 global (PRD 5.1).
create type t_advit.knowledge_tier as enum ('account', 'industry', 'global');

create type t_advit.compliance_verdict as enum ('pass', 'warn', 'block', 'not_evaluated');

comment on type t_advit.compliance_verdict is
  'not_evaluated exists so an unimplemented gate stage is visibly unimplemented. '
  'Silently reporting a skipped check as a pass is the failure mode PRD 4.5 warns against.';

-- ---------------------------------------------------------------------------
-- Workspaces
-- ---------------------------------------------------------------------------

create table t_advit.workspaces (
  id               uuid primary key default gen_random_uuid(),
  org_id           uuid not null references core.organisations(id) on delete cascade,
  name             text not null,
  business_type    t_advit.business_type not null default 'general_d2c',
  industry_pack_id text,

  -- PRD D4: every new workspace starts at L1, and climbing is earned from
  -- outcome data. The plan entitlement caps how high it may ever go; see
  -- t_advit.effective_autonomy below.
  autonomy_level   smallint not null default 1,

  -- Caps are mandatory. PRD 6.1 step 8: there is no "unlimited".
  daily_cap_inr    numeric(14,2) not null,
  monthly_cap_inr  numeric(14,2) not null,
  cac_ceiling_inr  numeric(14,2),

  timezone         text not null default 'Asia/Kolkata',
  is_paused        boolean not null default false,
  paused_reason    text,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),

  constraint workspaces_autonomy_range check (autonomy_level between 0 and 4),
  constraint workspaces_daily_cap_positive check (daily_cap_inr > 0),
  constraint workspaces_monthly_cap_positive check (monthly_cap_inr > 0),
  constraint workspaces_monthly_cap_covers_daily check (monthly_cap_inr >= daily_cap_inr)
);

create index workspaces_org_idx on t_advit.workspaces (org_id);

create table t_advit.workspace_members (
  workspace_id       uuid not null references t_advit.workspaces(id) on delete cascade,
  user_id            uuid not null references core.platform_users(id) on delete cascade,
  role               t_advit.workspace_role not null default 'analyst',
  approval_limit_inr numeric(14,2),
  added_by           uuid references core.platform_users(id) on delete set null,
  joined_at          timestamptz not null default now(),
  primary key (workspace_id, user_id),
  constraint workspace_members_limit_non_negative check (
    approval_limit_inr is null or approval_limit_inr >= 0
  )
);

create index workspace_members_user_idx on t_advit.workspace_members (user_id);

create trigger workspaces_touch
  before update on t_advit.workspaces
  for each row execute function core.touch_updated_at();

-- ---------------------------------------------------------------------------
-- Authorization helpers
-- ---------------------------------------------------------------------------

-- Access comes from an explicit workspace grant, or from being an owner/admin
-- of the owning organisation, or from being the superadmin.
create or replace function t_advit.is_workspace_member(
  p_workspace uuid,
  p_user      uuid default auth.uid()
)
returns boolean
language sql
stable
security definer
set search_path = t_advit, core, pg_catalog
as $fn$
  select exists (
    select 1
      from t_advit.workspaces w
     where w.id = p_workspace
       and (
            core.has_org_role(w.org_id, array['owner','admin']::core.org_role[], p_user)
         or exists (
              select 1
                from t_advit.workspace_members m
               where m.workspace_id = w.id
                 and m.user_id = p_user
            )
       )
  );
$fn$;

create or replace function t_advit.workspace_org(p_workspace uuid)
returns uuid
language sql
stable
security definer
set search_path = t_advit, pg_catalog
as $fn$
  select w.org_id from t_advit.workspaces w where w.id = p_workspace;
$fn$;

-- The integration point between billing and the PRD's safety ladder.
--
-- A workspace set to L3 on a plan capped at L2 behaves as L2. Neither number
-- alone is authoritative: the workspace expresses intent, the entitlement
-- expresses what was actually sold, and the lower of the two governs. An
-- organisation whose access is denied or read-only cannot act autonomously at
-- all, so it collapses to L0.
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
  where w.id = p_workspace;
$fn$;

comment on function t_advit.effective_autonomy(uuid) is
  'min(workspace intent, plan entitlement), floored to L0 when the workspace is '
  'paused or the organisation is not in full access. Consulted at step 2 of the '
  'tool-invocation pipeline (PRD 10.8) - never bypassed by an agent.';

-- ---------------------------------------------------------------------------
-- Meta connections and secrets
-- ---------------------------------------------------------------------------

create table t_advit.meta_connections (
  id              uuid primary key default gen_random_uuid(),
  workspace_id    uuid not null references t_advit.workspaces(id) on delete cascade,
  business_id     text,
  ad_account_id   text not null,
  page_id         text,
  ig_id           text,
  dataset_id      text,
  token_ref       uuid,
  scopes          text[] not null default '{}',
  expires_at      timestamptz,
  health          t_advit.connection_health not null default 'unknown',
  health_detail   jsonb not null default '{}'::jsonb,
  last_checked_at timestamptz,
  currency        text not null default 'INR',

  -- Guards the driver: an account absent from the allowlist is read-only no
  -- matter what an agent proposes.
  write_enabled   boolean not null default false,

  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  unique (workspace_id, ad_account_id)
);

create index meta_connections_workspace_idx on t_advit.meta_connections (workspace_id);

create trigger meta_connections_touch
  before update on t_advit.meta_connections
  for each row execute function core.touch_updated_at();

-- Envelope-encrypted material. Plaintext exists only in process memory for the
-- duration of a call: never logged, never traced (PRD 17.5).
create table t_advit.secrets (
  id           uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references t_advit.workspaces(id) on delete cascade,
  kind         text not null,
  ciphertext   bytea not null,
  key_version  integer not null default 1,
  created_at   timestamptz not null default now(),
  rotated_at   timestamptz
);

create index secrets_workspace_kind_idx on t_advit.secrets (workspace_id, kind);

-- ---------------------------------------------------------------------------
-- Account context (T1 semantic memory) and the advertiser's catalogue
-- ---------------------------------------------------------------------------

create table t_advit.account_context (
  id           uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references t_advit.workspaces(id) on delete cascade,
  dimension    text not null,
  key          text not null,
  value_json   jsonb not null,
  confidence   numeric(4,3) not null default 0.500,
  source       text not null,
  asserted_by  uuid references core.platform_users(id) on delete set null,
  valid_from   timestamptz not null default now(),
  valid_to     timestamptz,
  embedding    extensions.vector(1536),
  created_at   timestamptz not null default now(),
  constraint account_context_confidence_range check (confidence between 0 and 1)
);

create index account_context_workspace_dim_idx
  on t_advit.account_context (workspace_id, dimension, key);

comment on column t_advit.account_context.confidence is
  'Owner-asserted history is stored at low confidence: the OS tests it rather '
  'than trusting it (PRD 6.2).';

-- The advertiser's SKUs. Named catalog_products to keep it unambiguous against
-- core.products, which is the AI OS product catalogue.
create table t_advit.catalog_products (
  id               uuid primary key default gen_random_uuid(),
  workspace_id     uuid not null references t_advit.workspaces(id) on delete cascade,
  sku              text not null,
  name             text not null,
  mrp_inr          numeric(12,2),
  price_inr        numeric(12,2),
  cogs_inr         numeric(12,2),
  margin_rate      numeric(5,4),

  -- Misclassification is the most common root cause of an unfixable rejection
  -- (PRD 13.3): the legal class decides which claim set is even available.
  classification   text,
  ayush_licence_no text,
  cleared_claims   text[] not null default '{}',

  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  unique (workspace_id, sku),
  constraint catalog_products_margin_rate_range check (
    margin_rate is null or margin_rate between 0 and 1
  )
);

create trigger catalog_products_touch
  before update on t_advit.catalog_products
  for each row execute function core.touch_updated_at();

-- ---------------------------------------------------------------------------
-- Row-level security
-- ---------------------------------------------------------------------------

alter table t_advit.workspaces        enable row level security;
alter table t_advit.workspace_members enable row level security;
alter table t_advit.meta_connections  enable row level security;
alter table t_advit.secrets           enable row level security;
alter table t_advit.account_context   enable row level security;
alter table t_advit.catalog_products  enable row level security;

create policy workspaces_select on t_advit.workspaces
  for select to authenticated
  using (core.is_org_member(org_id) or core.is_superadmin());

-- Creating or reconfiguring a workspace is an organisation-admin action; caps
-- and autonomy are not self-service for ordinary members.
create policy workspaces_write on t_advit.workspaces
  for all to authenticated
  using (core.has_org_role(org_id, array['owner','admin']::core.org_role[]))
  with check (core.has_org_role(org_id, array['owner','admin']::core.org_role[]));

create policy workspace_members_select on t_advit.workspace_members
  for select to authenticated
  using (t_advit.is_workspace_member(workspace_id) or core.is_superadmin());

create policy workspace_members_write on t_advit.workspace_members
  for all to authenticated
  using (core.has_org_role(t_advit.workspace_org(workspace_id),
                           array['owner','admin']::core.org_role[]))
  with check (core.has_org_role(t_advit.workspace_org(workspace_id),
                                array['owner','admin']::core.org_role[]));

create policy meta_connections_select on t_advit.meta_connections
  for select to authenticated
  using (t_advit.is_workspace_member(workspace_id) or core.is_superadmin());

create policy meta_connections_write on t_advit.meta_connections
  for all to authenticated
  using (core.has_org_role(t_advit.workspace_org(workspace_id),
                           array['owner','admin']::core.org_role[]))
  with check (core.has_org_role(t_advit.workspace_org(workspace_id),
                                array['owner','admin']::core.org_role[]));

-- Secrets are never readable through the data API by any tenant role. The
-- agent runtime reads them with the service role and decrypts in memory.
create policy secrets_no_tenant_access on t_advit.secrets
  for all to authenticated
  using (false) with check (false);

create policy account_context_select on t_advit.account_context
  for select to authenticated
  using (t_advit.is_workspace_member(workspace_id) or core.is_superadmin());

create policy account_context_write on t_advit.account_context
  for all to authenticated
  using (t_advit.is_workspace_member(workspace_id))
  with check (t_advit.is_workspace_member(workspace_id));

create policy catalog_products_select on t_advit.catalog_products
  for select to authenticated
  using (t_advit.is_workspace_member(workspace_id) or core.is_superadmin());

create policy catalog_products_write on t_advit.catalog_products
  for all to authenticated
  using (t_advit.is_workspace_member(workspace_id))
  with check (t_advit.is_workspace_member(workspace_id));

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

grant usage on schema t_advit to anon, authenticated, service_role;

grant select, insert, update, delete on t_advit.workspaces        to authenticated;
grant select, insert, update, delete on t_advit.workspace_members to authenticated;
grant select, insert, update, delete on t_advit.meta_connections  to authenticated;
grant select, insert, update, delete on t_advit.account_context   to authenticated;
grant select, insert, update, delete on t_advit.catalog_products  to authenticated;
-- No grant on t_advit.secrets for tenant roles, by design.

grant all on all tables    in schema t_advit to service_role;
grant all on all sequences in schema t_advit to service_role;
grant all on all functions in schema t_advit to service_role;

grant execute on function t_advit.is_workspace_member(uuid, uuid) to authenticated;
grant execute on function t_advit.workspace_org(uuid)             to authenticated;
grant execute on function t_advit.effective_autonomy(uuid)        to authenticated;
