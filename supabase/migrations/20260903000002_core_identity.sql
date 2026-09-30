-- =============================================================================
-- Common SaaS Core - identity, organisations, membership
-- Hierarchy: Superadmin -> Organisation Owner -> Admin -> product roles
-- Product-specific roles live in the product schema, beneath 'admin'.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

create table core.platform_users (
  id              uuid primary key references auth.users(id) on delete cascade,
  email           extensions.citext not null unique,
  full_name       text,
  locale          text not null default 'en-IN',
  is_superadmin   boolean not null default false,
  is_active       boolean not null default true,
  last_seen_at    timestamptz,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

comment on column core.platform_users.is_superadmin is
  'Platform owner. Read from the database on every check - never trusted from a JWT claim alone.';

create table core.organisations (
  id                   uuid primary key default gen_random_uuid(),
  name                 text not null,
  slug                 extensions.citext not null unique,
  status               core.org_status not null default 'pending_activation',

  -- Billing identity. Required before a GST invoice can be issued.
  legal_name           text,
  gstin                text,
  billing_email        extensions.citext,
  billing_address_json jsonb not null default '{}'::jsonb,
  state_code           text,

  created_by           uuid references core.platform_users(id) on delete set null,
  activated_at         timestamptz,
  suspended_at         timestamptz,
  suspension_reason    text,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),

  -- 15-character GSTIN. Nullable until the organisation supplies it.
  constraint organisations_gstin_format check (
    gstin is null or gstin ~ '^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$'
  ),
  -- GST state code drives CGST+SGST (intra-state) vs IGST (inter-state).
  constraint organisations_state_code_format check (
    state_code is null or state_code ~ '^[0-9]{2}$'
  ),
  constraint organisations_suspension_reason_required check (
    status <> 'suspended' or suspension_reason is not null
  )
);

create index organisations_status_idx on core.organisations (status);

create table core.organisation_members (
  org_id             uuid not null references core.organisations(id) on delete cascade,
  user_id            uuid not null references core.platform_users(id) on delete cascade,
  role               core.org_role not null default 'member',
  -- Rupee-limited approval authority, enforced server-side (PRD 3.5, FR-003).
  approval_limit_inr numeric(14,2),
  invited_by         uuid references core.platform_users(id) on delete set null,
  joined_at          timestamptz not null default now(),
  primary key (org_id, user_id),
  constraint org_members_approval_limit_non_negative check (
    approval_limit_inr is null or approval_limit_inr >= 0
  )
);

create index organisation_members_user_idx on core.organisation_members (user_id);

create table core.invitations (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references core.organisations(id) on delete cascade,
  email       extensions.citext not null,
  role        core.org_role not null default 'member',
  token_hash  text not null,
  invited_by  uuid references core.platform_users(id) on delete set null,
  expires_at  timestamptz not null,
  accepted_at timestamptz,
  accepted_by uuid references core.platform_users(id) on delete set null,
  revoked_at  timestamptz,
  created_at  timestamptz not null default now()
);

-- One live invitation per email per organisation.
create unique index invitations_pending_unique
  on core.invitations (org_id, email)
  where accepted_at is null and revoked_at is null;

-- Superadmin handles Admin forgot-password OTP / reset requests.
create table core.password_reset_requests (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null references core.platform_users(id) on delete cascade,
  org_id           uuid references core.organisations(id) on delete set null,
  status           core.reset_status not null default 'pending',
  otp_hash         text,
  requested_at     timestamptz not null default now(),
  expires_at       timestamptz not null,
  reviewed_by      uuid references core.platform_users(id) on delete set null,
  reviewed_at      timestamptz,
  rejection_reason text,
  used_at          timestamptz,
  attempt_count    integer not null default 0,
  constraint reset_rejection_reason_required check (
    status <> 'rejected' or rejection_reason is not null
  ),
  constraint reset_attempt_count_bounded check (attempt_count between 0 and 10)
);

create index password_reset_requests_status_idx
  on core.password_reset_requests (status, expires_at);

create trigger platform_users_touch
  before update on core.platform_users
  for each row execute function core.touch_updated_at();

create trigger organisations_touch
  before update on core.organisations
  for each row execute function core.touch_updated_at();

-- ---------------------------------------------------------------------------
-- Authorization helpers
--
-- SECURITY DEFINER so that policies which consult membership do not recurse
-- through the very policies they are evaluating. STABLE so the planner may
-- cache them per statement. search_path is pinned: a SECURITY DEFINER function
-- with a mutable search_path is a privilege-escalation vector.
-- ---------------------------------------------------------------------------

create or replace function core.is_superadmin(p_user uuid default auth.uid())
returns boolean
language sql
stable
security definer
set search_path = core, pg_catalog
as $fn$
  select coalesce(
    (select u.is_superadmin and u.is_active
       from core.platform_users u
      where u.id = p_user),
    false
  );
$fn$;

create or replace function core.is_org_member(p_org uuid, p_user uuid default auth.uid())
returns boolean
language sql
stable
security definer
set search_path = core, pg_catalog
as $fn$
  select exists (
    select 1
      from core.organisation_members m
     where m.org_id = p_org
       and m.user_id = p_user
  );
$fn$;

create or replace function core.org_role(p_org uuid, p_user uuid default auth.uid())
returns core.org_role
language sql
stable
security definer
set search_path = core, pg_catalog
as $fn$
  select m.role
    from core.organisation_members m
   where m.org_id = p_org
     and m.user_id = p_user;
$fn$;

-- Superadmin satisfies any role requirement.
create or replace function core.has_org_role(
  p_org   uuid,
  p_roles core.org_role[],
  p_user  uuid default auth.uid()
)
returns boolean
language sql
stable
security definer
set search_path = core, pg_catalog
as $fn$
  select core.is_superadmin(p_user)
      or exists (
           select 1
             from core.organisation_members m
            where m.org_id = p_org
              and m.user_id = p_user
              and m.role = any(p_roles)
         );
$fn$;

-- Every organisation the caller belongs to. Product schemas use this to scope
-- their own RLS without duplicating membership logic.
create or replace function core.my_org_ids(p_user uuid default auth.uid())
returns setof uuid
language sql
stable
security definer
set search_path = core, pg_catalog
as $fn$
  select m.org_id
    from core.organisation_members m
   where m.user_id = p_user;
$fn$;

-- ---------------------------------------------------------------------------
-- Provision a platform_users row whenever Supabase Auth creates a user.
-- ---------------------------------------------------------------------------

create or replace function core.handle_new_auth_user()
returns trigger
language plpgsql
security definer
set search_path = core, pg_catalog
as $fn$
begin
  insert into core.platform_users (id, email, full_name)
  values (
    new.id,
    new.email,
    nullif(new.raw_user_meta_data ->> 'full_name', '')
  )
  on conflict (id) do nothing;
  return new;
end;
$fn$;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function core.handle_new_auth_user();

-- ---------------------------------------------------------------------------
-- Row-level security
--
-- RLS is the tenancy boundary, not an application convention. A forgotten
-- filter in a new code path must return nothing, never the wrong thing.
-- ---------------------------------------------------------------------------

alter table core.platform_users          enable row level security;
alter table core.organisations           enable row level security;
alter table core.organisation_members    enable row level security;
alter table core.invitations             enable row level security;
alter table core.password_reset_requests enable row level security;

-- platform_users: see yourself; superadmin sees everyone.
create policy platform_users_select_self on core.platform_users
  for select to authenticated
  using (id = auth.uid() or core.is_superadmin());

create policy platform_users_update_self on core.platform_users
  for update to authenticated
  using (id = auth.uid())
  with check (id = auth.uid());

-- organisations: members read their own; only superadmin creates or deletes.
create policy organisations_select_member on core.organisations
  for select to authenticated
  using (core.is_org_member(id) or core.is_superadmin());

create policy organisations_update_admin on core.organisations
  for update to authenticated
  using (core.has_org_role(id, array['owner','admin']::core.org_role[]))
  with check (core.has_org_role(id, array['owner','admin']::core.org_role[]));

create policy organisations_insert_superadmin on core.organisations
  for insert to authenticated
  with check (core.is_superadmin());

create policy organisations_delete_superadmin on core.organisations
  for delete to authenticated
  using (core.is_superadmin());

-- organisation_members: fellow members are visible; owners and admins manage.
create policy org_members_select on core.organisation_members
  for select to authenticated
  using (core.is_org_member(org_id) or core.is_superadmin());

create policy org_members_write on core.organisation_members
  for all to authenticated
  using (core.has_org_role(org_id, array['owner','admin']::core.org_role[]))
  with check (core.has_org_role(org_id, array['owner','admin']::core.org_role[]));

-- invitations: owners and admins only.
create policy invitations_manage on core.invitations
  for all to authenticated
  using (core.has_org_role(org_id, array['owner','admin']::core.org_role[]))
  with check (core.has_org_role(org_id, array['owner','admin']::core.org_role[]));

-- password_reset_requests: raise your own; superadmin adjudicates.
create policy password_reset_select on core.password_reset_requests
  for select to authenticated
  using (user_id = auth.uid() or core.is_superadmin());

create policy password_reset_insert on core.password_reset_requests
  for insert to authenticated
  with check (user_id = auth.uid());

create policy password_reset_review on core.password_reset_requests
  for update to authenticated
  using (core.is_superadmin())
  with check (core.is_superadmin());

-- ---------------------------------------------------------------------------
-- Grants. Custom schemas are not auto-exposed; PostgREST needs these.
-- ---------------------------------------------------------------------------

grant usage on schema core to anon, authenticated, service_role;

-- Column-level UPDATE. A blanket grant here would let any signed-in user set
-- their own is_superadmin flag through the platform_users_update_self policy,
-- which checks WHICH row you may touch but not WHICH columns.
grant select on core.platform_users to authenticated;
grant update (full_name, locale, last_seen_at) on core.platform_users to authenticated;
grant select, insert, update, delete on core.organisations           to authenticated;
grant select, insert, update, delete on core.organisation_members    to authenticated;
grant select, insert, update, delete on core.invitations             to authenticated;
grant select, insert, update         on core.password_reset_requests to authenticated;

grant all on all tables    in schema core to service_role;
grant all on all sequences in schema core to service_role;
grant all on all functions in schema core to service_role;

grant execute on function core.is_superadmin(uuid)                       to authenticated;
grant execute on function core.is_org_member(uuid, uuid)                 to authenticated;
grant execute on function core.org_role(uuid, uuid)                      to authenticated;
grant execute on function core.has_org_role(uuid, core.org_role[], uuid) to authenticated;
grant execute on function core.my_org_ids(uuid)                          to authenticated;
