-- =============================================================================
-- Local development seed
--
-- Deterministic UUIDs so integration tests can reference fixtures by constant.
-- Runs automatically after migrations on `supabase db reset`.
--
-- NEVER run this against a hosted environment: it creates known passwords.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Auth users
--
-- The core.handle_new_auth_user trigger provisions the matching
-- core.platform_users row, so we only patch is_superadmin afterwards.
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- The four token columns below are written as '' rather than left to default.
--
-- GoTrue scans `confirmation_token`, `recovery_token`, `email_change_token_new`
-- and `email_change` into Go `string`s, which cannot hold NULL, and only some
-- of the token columns in auth.users carry a `''` default. Leaving the rest
-- NULL makes every password grant fail with
--
--   500 {"msg":"Database error querying schema"}
--   error finding user: sql: Scan error on column index 3, name
--   "confirmation_token": converting NULL to string is unsupported
--
-- which names a schema problem and is actually a data one. This seed has been
-- in the repository since the beginning and nobody hit it, because nothing in
-- the product needed a session until authentication landed: it is not a
-- regression, it is the first time anyone tried to sign in.
-- ---------------------------------------------------------------------------
insert into auth.users (
  instance_id, id, aud, role, email, encrypted_password,
  email_confirmed_at, created_at, updated_at,
  raw_app_meta_data, raw_user_meta_data, is_super_admin,
  confirmation_token, recovery_token, email_change_token_new, email_change
)
values
  (
    '00000000-0000-0000-0000-000000000000',
    '00000000-0000-4000-8000-000000000001',
    'authenticated', 'authenticated',
    'superadmin@advit.local',
    extensions.crypt('superadmin-dev-password', extensions.gen_salt('bf')),
    now(), now(), now(),
    '{"provider":"email","providers":["email"]}'::jsonb,
    '{"full_name":"Platform Superadmin"}'::jsonb,
    false,
    '', '', '', ''
  ),
  (
    '00000000-0000-0000-0000-000000000000',
    '00000000-0000-4000-8000-000000000002',
    'authenticated', 'authenticated',
    'owner@advit.local',
    extensions.crypt('owner-dev-password', extensions.gen_salt('bf')),
    now(), now(), now(),
    '{"provider":"email","providers":["email"]}'::jsonb,
    '{"full_name":"Demo Owner"}'::jsonb,
    false,
    '', '', '', ''
  ),
  (
    '00000000-0000-0000-0000-000000000000',
    '00000000-0000-4000-8000-000000000003',
    'authenticated', 'authenticated',
    'buyer@broadmate.local',
    extensions.crypt('buyer-dev-password', extensions.gen_salt('bf')),
    now(), now(), now(),
    '{"provider":"email","providers":["email"]}'::jsonb,
    '{"full_name":"Media Buyer"}'::jsonb,
    false,
    '', '', '', ''
  ),
  -- Belongs to a different organisation. Exists so the cross-tenant leakage
  -- tests have a real outsider to assert against.
  (
    '00000000-0000-0000-0000-000000000000',
    '00000000-0000-4000-8000-000000000004',
    'authenticated', 'authenticated',
    'outsider@rival.local',
    extensions.crypt('outsider-dev-password', extensions.gen_salt('bf')),
    now(), now(), now(),
    '{"provider":"email","providers":["email"]}'::jsonb,
    '{"full_name":"Rival Org Owner"}'::jsonb,
    false,
    '', '', '', ''
  ),
  -- An ORGANISATION member with no workspace grant at all.
  --
  -- This user exists for one asymmetry, and it is the asymmetry the API's
  -- workspace resolver has to respect. `workspaces_select` uses the wider
  -- core.is_org_member, so this account CAN read the workspace row. Every other
  -- product policy - ad_sets, actions, approvals - uses
  -- t_advit.is_workspace_member, which also admits org owners and admins, and
  -- this account is none of those. So it passes the SELECT and fails membership.
  --
  -- Without it there is nobody in the fixture set who can demonstrate that, and
  -- a resolver treating "the SELECT returned a row" as proof of membership would
  -- pass every test in the suite while letting any member of an organisation
  -- drive the tool pipeline against a sibling workspace they cannot read.
  (
    '00000000-0000-0000-0000-000000000000',
    '00000000-0000-4000-8000-000000000005',
    'authenticated', 'authenticated',
    'analyst@broadmate.local',
    extensions.crypt('analyst-dev-password', extensions.gen_salt('bf')),
    now(), now(), now(),
    '{"provider":"email","providers":["email"]}'::jsonb,
    '{"full_name":"Org Analyst"}'::jsonb,
    false,
    '', '', '', ''
  )
on conflict (id) do nothing;

insert into auth.identities (
  id, user_id, provider_id, identity_data, provider, last_sign_in_at, created_at, updated_at
)
select
  gen_random_uuid(), u.id, u.id::text,
  jsonb_build_object('sub', u.id::text, 'email', u.email, 'email_verified', true),
  'email', now(), now(), now()
from auth.users u
where u.id in (
  '00000000-0000-4000-8000-000000000001',
  '00000000-0000-4000-8000-000000000002',
  '00000000-0000-4000-8000-000000000003',
  '00000000-0000-4000-8000-000000000004',
  '00000000-0000-4000-8000-000000000005'
)
on conflict do nothing;

update core.platform_users
   set is_superadmin = true, full_name = 'Platform Superadmin'
 where id = '00000000-0000-4000-8000-000000000001';

-- ---------------------------------------------------------------------------
-- Organisations
-- ---------------------------------------------------------------------------

insert into core.organisations (
  id, name, slug, status, legal_name, billing_email,
  state_code, billing_address_json, created_by, activated_at
)
values
  (
    '00000000-0000-4000-8000-000000000010',
    'Broadmate Global',
    'broadmate-global',
    'active',
    'Broadmate Global',
    'owner@advit.local',
    -- 09 = Uttar Pradesh. Seller and buyer in the same state means CGST+SGST
    -- rather than IGST; see the invoicing migration.
    '09',
    jsonb_build_object(
      'line1', 'Greater Noida',
      'city',  'Greater Noida',
      'state', 'Uttar Pradesh',
      'pincode', '201310',
      'country', 'IN'
    ),
    '00000000-0000-4000-8000-000000000001',
    now()
  ),
  (
    '00000000-0000-4000-8000-000000000011',
    'Rival Wellness',
    'rival-wellness',
    'active',
    'Rival Wellness Pvt Ltd',
    'outsider@rival.local',
    '27',
    jsonb_build_object('city', 'Mumbai', 'state', 'Maharashtra', 'country', 'IN'),
    '00000000-0000-4000-8000-000000000001',
    now()
  )
on conflict (id) do nothing;

insert into core.organisation_members (org_id, user_id, role, approval_limit_inr)
values
  ('00000000-0000-4000-8000-000000000010', '00000000-0000-4000-8000-000000000002', 'owner',  null),
  ('00000000-0000-4000-8000-000000000010', '00000000-0000-4000-8000-000000000003', 'member', 5000.00),
  ('00000000-0000-4000-8000-000000000011', '00000000-0000-4000-8000-000000000004', 'owner',  null),
  -- Deliberately `member` and deliberately given no t_advit.workspace_members
  -- row in 03_advit_workspace.sql. See the comment on the auth.users row above.
  ('00000000-0000-4000-8000-000000000010', '00000000-0000-4000-8000-000000000005', 'member', 0.00)
on conflict do nothing;

-- ---------------------------------------------------------------------------
-- The product catalogue - products, feature definitions, plans, plan features
-- - is NOT here any more. It is platform data and lives in
-- migrations/20260917000002_the_catalogue_is_platform_data.sql, which runs
-- before this file and reaches production the same way every other migration
-- does. This file holds only what a hosted database must never receive:
-- fixture users with known passwords, and the two demo organisations.
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- Subscriptions
--
-- Broadmate is active (so the Marketing spine is exercisable end to end).
-- Rival is trialing (so cross-tenant tests run against a live-but-separate org).
-- ---------------------------------------------------------------------------

insert into core.subscriptions (
  id, org_id, product_id, plan_id, status,
  trial_ends_at, current_period_start, current_period_end
)
values
  (
    '00000000-0000-4000-8000-000000000040',
    '00000000-0000-4000-8000-000000000010',
    '00000000-0000-4000-8000-000000000020',
    '00000000-0000-4000-8000-000000000030',
    'active',
    null,
    date_trunc('month', now()),
    date_trunc('month', now()) + interval '1 month'
  ),
  (
    '00000000-0000-4000-8000-000000000041',
    '00000000-0000-4000-8000-000000000011',
    '00000000-0000-4000-8000-000000000020',
    '00000000-0000-4000-8000-000000000030',
    'trialing',
    now() + interval '14 days',
    now(),
    now() + interval '14 days'
  )
on conflict (id) do nothing;

select core.log_audit(
  'platform', 'seed.applied',
  p_actor_type => 'system',
  p_actor      => null,
  p_payload    => jsonb_build_object('environment', 'local')
);
