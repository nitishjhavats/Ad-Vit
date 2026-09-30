-- =============================================================================
-- Fix: the identity helpers answered about any user, not only the caller
--
-- core.my_org_ids, is_org_member, org_role, has_org_role and is_superadmin all
-- take `p_user uuid default auth.uid()`. The default is what every RLS policy
-- passes, and it is safe. The ARGUMENT is what is not: any signed-in user could
-- name somebody else. Verified against the running database before this was
-- written - as an outsider with no relationship to the organisation at all:
--
--     select core.org_role('<Broadmate>', '<owner>');   -- returned 'owner'
--
-- Not merely a membership oracle. A membership AND ROLE oracle: it answers
-- which organisations a stranger belongs to and, in each, whether they are the
-- owner - which is precisely the question an attacker asks before deciding whom
-- to phish. core.is_superadmin(p_user) answers the same question about the
-- operators.
--
-- The PUBLIC revoke in 20260910000003 closed this for `anon` and left the
-- explicit `to authenticated` grants standing underneath it, so it remained
-- open to every tenant.
--
-- -----------------------------------------------------------------------------
-- Why this is not fixed by revoking the grant, which was the obvious move
--
-- There is only ONE function per name. `core.is_org_member(p_org)` in a policy
-- body is not a narrower second function - it is this same two-argument
-- function with its default applied. Revoking EXECUTE therefore does not remove
-- the argument; it removes the function, from the policies too.
--
-- That is not a deduction. It was tried first, and `supabase db reset` came up
-- with every tenant read failing on `permission denied for function
-- has_org_role` - because an RLS policy expression is evaluated with the
-- privileges of the QUERYING user, so the caller needs EXECUTE on every
-- function a policy body names, SECURITY DEFINER or not.
--
-- Splitting the signature into a one-argument form for callers and a
-- two-argument form for the backend does not work either: PostgreSQL refuses to
-- remove a parameter default through CREATE OR REPLACE ("cannot remove
-- parameter defaults from existing function"), and DROP would cascade through
-- seventeen policies that depend on these by OID.
--
-- So the guard goes INSIDE. The function keeps its shape and stops answering
-- the question it should never have answered.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- The predicate, in one place.
--
-- SECURITY DEFINER because it reads core.platform_users, which a tenant cannot.
-- That costs inlining - the planner will not inline a definer function - but
-- these five are all SECURITY DEFINER already and therefore were never inlined,
-- so the hot path gains one function call and one GUC read, not a query.
--
-- CASE rather than OR, deliberately. PostgreSQL does not promise to evaluate
-- the arms of an OR left to right and is free to reorder them, which would run
-- the platform_users lookup on every single row of every RLS-filtered read.
-- CASE does evaluate its conditions in order and stops, so the common path -
-- the one every policy takes - costs the first comparison and nothing else.
-- ---------------------------------------------------------------------------
create or replace function core.may_ask_about(p_user uuid)
returns boolean
language sql
stable
security definer
set search_path = core, pg_catalog
as $fn$
  select case
           -- Asking about yourself. This is the defaulted argument, so it is
           -- the branch every RLS policy in both schemas takes.
           --
           -- `is not distinct from` rather than `=`: on the service connection
           -- both sides are NULL, and `null = null` is NULL, which a CASE
           -- treats as not-matched and would send the backend down the
           -- platform_users lookup on every row.
           when p_user is not distinct from auth.uid() then true

           -- The backend. Same test core.assert_org_visible uses since
           -- 20260910000003, and for the same reason: a caller with no subject
           -- is only trusted if it also has no PostgREST role. `anon` and a
           -- subjectless `authenticated` are anonymous, not privileged.
           when auth.uid() is null
                and coalesce(current_setting('role', true), 'none')
                    not in ('anon', 'authenticated') then true

           -- A superadmin, who legitimately answers "who owns this account" in
           -- the support console. Read from platform_users on every call rather
           -- than trusted from a claim - the rule core.platform_users.is_superadmin
           -- already carries in its own comment.
           --
           -- Written out rather than calling core.is_superadmin(), which is
           -- itself guarded by this function below. The indirection would
           -- terminate, but only because of an argument about which branch runs
           -- first, and a guard whose correctness rests on that is one edit away
           -- from infinite recursion.
           else exists (
             select 1 from core.platform_users u
              where u.id = auth.uid() and u.is_superadmin and u.is_active
           )
         end;
$fn$;

comment on function core.may_ask_about(uuid) is
  'May the current caller ask an identity question about p_user? True for the '
  'caller themselves (the defaulted argument every RLS policy passes), for the '
  'backend, and for an active superadmin. Everyone else is refused rather than '
  'answered.';


create or replace function core.refuse_identity_probe(p_fn text)
returns boolean
language plpgsql
stable
as $fn$
begin
  -- The message names the function and not the subject. "User X is not a member
  -- of org Y" would answer the question by refusing it.
  raise exception '% may only be asked about yourself', p_fn
    using errcode = '42501', hint = 'identity_probe_refused';
end;
$fn$;

comment on function core.refuse_identity_probe(text) is
  'Raises. Exists so the guarded helpers can stay `language sql` - a CASE needs '
  'an expression in its ELSE arm, and a plain SQL body cannot raise.';


-- ---------------------------------------------------------------------------
-- The two in the hot path keep `language sql`.
--
-- is_org_member and has_org_role are named by seventeen RLS policies and run
-- once per candidate row. They stay a single SELECT expression so the only
-- thing added per row is the CASE.
-- ---------------------------------------------------------------------------

create or replace function core.is_org_member(p_org uuid, p_user uuid default auth.uid())
returns boolean
language sql
stable
security definer
set search_path = core, pg_catalog
as $fn$
  select case when core.may_ask_about(p_user) then
    exists (
      select 1
        from core.organisation_members m
       where m.org_id = p_org
         and m.user_id = p_user
    )
  else core.refuse_identity_probe('core.is_org_member') end;
$fn$;


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
  select case when core.may_ask_about(p_user) then
    core.is_superadmin(p_user)
      or exists (
           select 1
             from core.organisation_members m
            where m.org_id = p_org
              and m.user_id = p_user
              and m.role = any(p_roles)
         )
  else core.refuse_identity_probe('core.has_org_role') end;
$fn$;


-- ---------------------------------------------------------------------------
-- The two outside it become plpgsql.
--
-- org_role returns core.org_role and my_org_ids returns SETOF uuid, neither of
-- which fits a boolean ELSE arm without a cast nobody would enjoy reading.
-- Neither is named by any policy - org_role is called only from
-- t_advit.is_workspace_member, my_org_ids by nothing in either schema - so the
-- per-call cost of a plpgsql context lands nowhere hot.
--
-- For my_org_ids the guard must be a statement rather than a WHERE clause: as a
-- predicate it would be evaluated per row, so probing a user who belongs to no
-- organisation would return empty WITHOUT raising, and "empty" is an answer.
-- ---------------------------------------------------------------------------

create or replace function core.org_role(p_org uuid, p_user uuid default auth.uid())
returns core.org_role
language plpgsql
stable
security definer
set search_path = core, pg_catalog
as $fn$
begin
  if not core.may_ask_about(p_user) then
    perform core.refuse_identity_probe('core.org_role');
  end if;

  return (
    select m.role
      from core.organisation_members m
     where m.org_id = p_org
       and m.user_id = p_user
  );
end;
$fn$;


create or replace function core.my_org_ids(p_user uuid default auth.uid())
returns setof uuid
language plpgsql
stable
security definer
set search_path = core, pg_catalog
as $fn$
begin
  if not core.may_ask_about(p_user) then
    perform core.refuse_identity_probe('core.my_org_ids');
  end if;

  return query
    select m.org_id
      from core.organisation_members m
     where m.user_id = p_user;
end;
$fn$;


-- ---------------------------------------------------------------------------
-- And the one that names the operators.
--
-- core.is_superadmin(p_user) is the same oracle pointed at the staff list: it
-- tells any tenant which accounts can see every tenant. 20260910000003 named it
-- when revoking PUBLIC and then left `authenticated` holding it.
--
-- The zero-argument call - is_superadmin(), which is what every `or
-- core.is_superadmin()` branch in every policy uses - is unaffected, because
-- p_user defaults to auth.uid() and takes the first branch of may_ask_about.
-- ---------------------------------------------------------------------------
create or replace function core.is_superadmin(p_user uuid default auth.uid())
returns boolean
language sql
stable
security definer
set search_path = core, pg_catalog
as $fn$
  select case when core.may_ask_about(p_user) then
    coalesce(
      (select u.is_superadmin and u.is_active
         from core.platform_users u
        where u.id = p_user),
      false
    )
  else core.refuse_identity_probe('core.is_superadmin') end;
$fn$;


-- ---------------------------------------------------------------------------
-- Grants. Every function here is new or re-issued, and PostgreSQL grants
-- EXECUTE to PUBLIC on a newly created function - including on the replacement
-- half of CREATE OR REPLACE when the function did not previously exist. So the
-- revoke from 20260910000003 has to be re-applied to the two new ones, or this
-- migration hands `anon` back the branch that one closed.
-- ---------------------------------------------------------------------------
revoke execute on function core.may_ask_about(uuid)         from public;
revoke execute on function core.refuse_identity_probe(text) from public;

grant execute on function core.may_ask_about(uuid)         to authenticated, advit_backend;
grant execute on function core.refuse_identity_probe(text) to authenticated, advit_backend;

-- The guarded five keep the grants they already had; the guard, not the grant,
-- is now what decides. Re-stated rather than assumed, because CREATE OR REPLACE
-- preserves the ACL and a reader should not have to know that.
grant execute on function core.is_org_member(uuid, uuid)                    to authenticated, advit_backend;
grant execute on function core.has_org_role(uuid, core.org_role[], uuid)    to authenticated, advit_backend;
grant execute on function core.org_role(uuid, uuid)                         to authenticated, advit_backend;
grant execute on function core.my_org_ids(uuid)                             to authenticated, advit_backend;
grant execute on function core.is_superadmin(uuid)                          to authenticated, advit_backend;
