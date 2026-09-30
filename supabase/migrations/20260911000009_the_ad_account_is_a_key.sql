-- =============================================================================
-- The ad account becomes a key on the rows that describe it
--
-- t_advit.metrics_daily carries (workspace_id, date, level, entity_id) and
-- nothing else identifying. t_advit.campaigns carries (workspace_id, meta_id).
-- Neither says which AD ACCOUNT the row came from.
--
-- That is survivable for a workspace with one connection and wrong for a
-- workspace with three, which is the shape this product sells: up to three ad
-- accounts per organisation by default, more on request. The consequences are
-- not subtle:
--
--   * "spend by account" cannot be computed at all. The account-level rows are
--     distinguishable only by the convention that entity_id happens to hold the
--     ad account id, and nothing has ever enforced that convention.
--   * a campaign-level row cannot be attributed to an account even in
--     principle, because campaigns does not carry one either - so per-account
--     spend cannot be reconciled against the account-level total, which is the
--     one check that catches an ingestion that silently dropped a campaign.
--   * the daily cap is workspace-wide, so it is not wrong today. The moment
--     anyone wants a per-account ceiling - the obvious next request - there is
--     no column to hang it on.
--
-- A composite FOREIGN KEY rather than a CHECK, because
-- meta_connections(workspace_id, ad_account_id) is already unique. That makes
-- "this metric belongs to an account this workspace actually connected" a
-- property the database enforces rather than one the ingestion code remembers.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. metrics_daily
-- ---------------------------------------------------------------------------

alter table t_advit.metrics_daily
  add column if not exists ad_account_id text;

-- Backfill before the constraints, and only where the answer is KNOWN.
--
--   * an account-level row's entity_id IS the ad account id by the convention
--     this migration is about to make real;
--   * every other row can be attributed only if the workspace has exactly one
--     connection, in which case there is no ambiguity to resolve.
--
-- A workspace with several connections and pre-existing non-account rows cannot
-- be backfilled by guessing, and the NOT NULL below will refuse - which is the
-- correct outcome: somebody has to look at those rows.
update t_advit.metrics_daily m
   set ad_account_id = m.entity_id
 where m.ad_account_id is null
   and m.level = 'account';

update t_advit.metrics_daily m
   set ad_account_id = c.ad_account_id
  from t_advit.meta_connections c
 where m.ad_account_id is null
   and c.workspace_id = m.workspace_id
   and (select count(*) from t_advit.meta_connections c2
         where c2.workspace_id = m.workspace_id) = 1;

alter table t_advit.metrics_daily
  alter column ad_account_id set not null;

alter table t_advit.metrics_daily
  drop constraint if exists metrics_daily_account_row_names_itself;

alter table t_advit.metrics_daily
  add constraint metrics_daily_account_row_names_itself check (
    -- The convention, made explicit. An account-level row whose entity_id and
    -- ad_account_id disagree describes two different accounts at once, and
    -- whichever one a later query picks, half its answers are wrong.
    level <> 'account' or ad_account_id = entity_id
  );

alter table t_advit.metrics_daily
  drop constraint if exists metrics_daily_connection_fkey;

alter table t_advit.metrics_daily
  add constraint metrics_daily_connection_fkey
  foreign key (workspace_id, ad_account_id)
  references t_advit.meta_connections (workspace_id, ad_account_id)
  -- RESTRICT, not CASCADE and not SET NULL.
  --
  -- A meta_connections row is the record that this account was ever connected.
  -- Deleting it while metrics exist would either destroy the provenance of
  -- every figure derived from that account (CASCADE) or leave rows that cannot
  -- say where they came from (SET NULL). Disconnecting an account is a
  -- soft operation - `write_enabled = false`, and `health` moved to 'unhealthy'
  -- (the enum's values are healthy, degraded, unhealthy, unknown; there is no
  -- 'revoked') - and this makes that the only available one until somebody
  -- deliberately decides what should happen to the history.
  on delete restrict
  on update cascade;

create index if not exists metrics_daily_account_date_idx
  on t_advit.metrics_daily (workspace_id, ad_account_id, date desc);

comment on column t_advit.metrics_daily.ad_account_id is
  'Which connected ad account this row came from. For an account-level row it '
  'equals entity_id, enforced by a CHECK. The composite FK to meta_connections '
  'is what makes per-account spend reconcilable against the account total.';


-- ---------------------------------------------------------------------------
-- 2. campaigns
--
-- Without this, a campaign-level metric cannot be attributed to an account even
-- by joining, so the reconciliation above would have nothing to reconcile.
-- ---------------------------------------------------------------------------

alter table t_advit.campaigns
  add column if not exists ad_account_id text;

update t_advit.campaigns c
   set ad_account_id = mc.ad_account_id
  from t_advit.meta_connections mc
 where c.ad_account_id is null
   and mc.workspace_id = c.workspace_id
   and (select count(*) from t_advit.meta_connections c2
         where c2.workspace_id = c.workspace_id) = 1;

alter table t_advit.campaigns
  alter column ad_account_id set not null;

alter table t_advit.campaigns
  drop constraint if exists campaigns_connection_fkey;

alter table t_advit.campaigns
  add constraint campaigns_connection_fkey
  foreign key (workspace_id, ad_account_id)
  references t_advit.meta_connections (workspace_id, ad_account_id)
  on delete restrict
  on update cascade;

create index if not exists campaigns_account_idx
  on t_advit.campaigns (workspace_id, ad_account_id, status);


-- ---------------------------------------------------------------------------
-- 3. How many ad accounts an organisation may connect
--
-- The product sells three by default, and a superadmin raises it per
-- organisation. Both halves already exist - core.feature_definitions holds the
-- default, core.entitlement_overrides holds the per-org exception, and
-- core.assert_entitled enforces a limit. Nothing connected them to
-- meta_connections, so the limit was documentation.
-- ---------------------------------------------------------------------------

-- core.feature_definitions.max_ad_accounts stays at 1 and is NOT raised here.
-- That number is the floor an organisation gets with NO plan grant and no
-- override, and the seed says so in its own comment: deliberately conservative.
-- The three the product sells come from core.plan_features on the `standard`
-- plan, which is where a commercial decision belongs - raising the floor would
-- also raise it for an organisation whose subscription has lapsed.

create or replace function core.count_org_ad_accounts(p_org uuid)
returns integer
language sql
stable
security definer
set search_path = core, pg_catalog
as $fn$
  -- Per ORGANISATION, not per workspace: the entitlement is sold to the
  -- organisation, and counting per workspace would let one org connect three
  -- accounts per workspace and as many workspaces as it liked.
  --
  -- DISTINCT because the same ad account may legitimately be connected to two
  -- workspaces in one organisation - a shared account being managed by two
  -- teams is one account, and charging for it twice would be a bug the customer
  -- notices.
  select count(distinct c.ad_account_id)::integer
    from t_advit.meta_connections c
    join t_advit.workspaces w on w.id = c.workspace_id
   where w.org_id = p_org;
$fn$;

revoke execute on function core.count_org_ad_accounts(uuid) from public;
grant execute on function core.count_org_ad_accounts(uuid) to authenticated, advit_backend;


create or replace function t_advit.guard_ad_account_limit()
returns trigger
language plpgsql
security definer
set search_path = t_advit, core, pg_catalog
as $fn$
declare
  v_org  uuid;
  v_have integer;
begin
  select w.org_id into v_org
    from t_advit.workspaces w where w.id = new.workspace_id;

  if v_org is null then
    raise exception 'workspace % does not exist', new.workspace_id
      using errcode = '23503';
  end if;

  -- A transaction-scoped advisory lock keyed on the ORGANISATION.
  --
  -- Without it the count and the insert are two statements with a gap between
  -- them, and two connections arriving together both count two, both pass a
  -- limit of three, and the organisation ends with four. It is the same shape
  -- as the spend-cap race in the tool pipeline, and it fails the same way:
  -- quietly, with both requests succeeding.
  --
  -- Transaction-scoped (pg_advisory_xact_lock) rather than session-scoped, so
  -- it is released by COMMIT or ROLLBACK and cannot be leaked by a connection
  -- returning to a pool mid-transaction.
  perform pg_advisory_xact_lock(hashtext('advit.ad_accounts:' || v_org::text));

  select core.count_org_ad_accounts(v_org) into v_have;

  -- Re-connecting an account this organisation already holds is not a new
  -- account. Checked after the lock so the count and this test see the same
  -- state.
  if exists (
    select 1
      from t_advit.meta_connections c
      join t_advit.workspaces w on w.id = c.workspace_id
     where w.org_id = v_org
       and c.ad_account_id = new.ad_account_id
       and c.id is distinct from new.id
  ) then
    return new;
  end if;

  -- core.assert_entitled raises 42501 with a hint. It also refuses when the
  -- limit cannot be resolved to a number at all, which is the behaviour
  -- 20260910000004 exists to guarantee: an unresolvable allowance is not
  -- evidence of permission.
  perform core.assert_entitled(v_org, 'max_ad_accounts', v_have + 1);

  return new;
end;
$fn$;

revoke execute on function t_advit.guard_ad_account_limit() from public;

drop trigger if exists meta_connections_ad_account_limit on t_advit.meta_connections;
create trigger meta_connections_ad_account_limit
  before insert on t_advit.meta_connections
  for each row execute function t_advit.guard_ad_account_limit();

comment on trigger meta_connections_ad_account_limit on t_advit.meta_connections is
  'Enforces core entitlement max_ad_accounts per ORGANISATION, under a '
  'transaction advisory lock so two concurrent connects cannot both pass the '
  'same limit. Default 3; a superadmin raises it with an entitlement override.';


-- ---------------------------------------------------------------------------
-- 4. An entitlement override says who set it, and leaves a trail
--
-- `set_by` is a uuid column on a row that decides how much an organisation may
-- spend and how autonomous its agents may be, and it was whatever the caller
-- typed. Changing an organisation's entitlements wrote no audit row at all, so
-- "who raised this account's cap, and when" was unanswerable.
-- ---------------------------------------------------------------------------

create or replace function core.stamp_entitlement_override()
returns trigger
language plpgsql
security definer
set search_path = core, pg_catalog
as $fn$
declare
  v_actor uuid := auth.uid();
begin
  -- From the session, never from the payload - the same rule the approval
  -- signature and business_truth.entered_by now follow. On a backend
  -- connection auth.uid() is null, and a null here is honest: it says the
  -- change came from the service rather than naming a person who did not make
  -- it.
  new.set_by := v_actor;

  -- Explicit casts: p_scope is core.audit_scope and p_actor_type is
  -- core.actor_type, and PostgreSQL will not resolve an unknown literal to an
  -- enum through a named-argument call.
  perform core.log_audit(
    'organisation'::core.audit_scope,
    case when tg_op = 'INSERT' then 'entitlement.granted' else 'entitlement.changed' end,
    p_org        => new.org_id,
    -- 'superadmin' is what this SHOULD be, and core.log_audit will overrule it
    -- to 'user' on a tenant connection - verified, not assumed. That is the
    -- audit-forgery guard from 20260907000001 doing exactly its job: a caller
    -- with a JWT subject does not get to choose how it is described in the
    -- append-only trail, whoever it is. The row still names the right actor_id,
    -- which is the part that answers "who raised this cap".
    p_actor_type => (case when v_actor is null then 'system' else 'superadmin' end)::core.actor_type,
    p_actor      => v_actor,
    p_payload    => jsonb_build_object(
      'feature_key', new.feature_key,
      'value',       new.value_json,
      'previous',    case when tg_op = 'UPDATE' then old.value_json else null end,
      'reason',      new.reason,
      'expires_at',  new.expires_at
    )
  );

  return new;
end;
$fn$;

revoke execute on function core.stamp_entitlement_override() from public;

drop trigger if exists entitlement_overrides_stamp on core.entitlement_overrides;
create trigger entitlement_overrides_stamp
  -- Order against the value guard from 20260910000004 does not matter, and it
  -- is worth saying why rather than arranging the names to make it look like it
  -- does. Triggers of the same timing fire alphabetically, so this one runs
  -- FIRST - but if the value guard then rejects the write, the statement
  -- raises and the whole transaction rolls back, taking the audit row with it.
  -- A rejected override leaves no trail claiming it was granted either way.
  before insert or update on core.entitlement_overrides
  for each row execute function core.stamp_entitlement_override();

comment on column core.entitlement_overrides.set_by is
  'Who granted this override, written from auth.uid() by a trigger. It was '
  'caller-supplied on a row that decides how much an organisation may spend.';
