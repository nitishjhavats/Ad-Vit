-- =============================================================================
-- Common SaaS Core — foundation
-- Product-agnostic control plane. Nothing in the `core` schema may reference
-- the `marketing` schema; the dependency runs one way only, so the core can be
-- lifted into a shared control plane for future products without a rewrite.
-- =============================================================================

create extension if not exists pgcrypto with schema extensions;
create extension if not exists citext   with schema extensions;

create schema if not exists core;

comment on schema core is
  'Common SaaS Core: identity, organisations, RBAC, plans, entitlements, billing, '
  'usage metering and audit. Product-agnostic — must not depend on any product schema.';

-- ---------------------------------------------------------------------------
-- Enumerated types
-- ---------------------------------------------------------------------------

create type core.org_status as enum (
  'pending_activation',  -- created, not yet activated by a superadmin
  'active',
  'suspended'
);

-- Organisation-level roles. Product-specific roles live in the product schema,
-- beneath 'admin' (see t_advit.workspace_members).
create type core.org_role as enum ('owner', 'admin', 'member');

create type core.subscription_status as enum (
  'trialing',
  'pending_payment',   -- trial ended or period rolled; awaiting payment
  'active',
  'past_due',          -- payment window elapsed without submission
  'grace',             -- read-only degradation before hard expiry
  'expired',
  'suspended'          -- superadmin action
);

create type core.payment_status as enum (
  'draft',
  'awaiting_payment',  -- request issued; 4-hour window running
  'submitted',         -- reference supplied by the organisation
  'approved',          -- superadmin approved; subscription may activate
  'rejected',
  'expired'            -- window elapsed
);

create type core.billing_period as enum ('monthly', 'quarterly', 'yearly');

create type core.audit_scope as enum ('platform', 'organisation', 'workspace');

-- The AI is a named actor, never an implicit superuser (PRD §3.5).
create type core.actor_type as enum (
  'user',
  'agent',
  'automation',
  'superadmin',
  'system'
);

create type core.reset_status as enum (
  'pending',
  'approved',
  'rejected',
  'used',
  'expired'
);

-- ---------------------------------------------------------------------------
-- Shared trigger: maintain updated_at
-- ---------------------------------------------------------------------------

create or replace function core.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;
