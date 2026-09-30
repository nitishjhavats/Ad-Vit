-- =============================================================================
-- The agent runtime stops being the superuser.
--
-- Today it connects as `postgres`: rolsuper, rolbypassrls, owner of every table.
-- On that connection RLS is not weakened, it is ABSENT - every one of the 73
-- policies in these migrations is dead code for the API path, and the 141
-- database tests that exercise them are testing a boundary production does not
-- stand behind.
--
-- Three roles. The split is the schema's own stated intent rather than an
-- invention: 20260903000007 grants `authenticated` SELECT-only on runs,
-- decisions, actions, outcomes and guardrail_events, UPDATE on approvals alone,
-- and nothing at all on t_advit.secrets - "written by the agent runtime under
-- the service role, so an agent cannot rewrite its own history". That sentence
-- describes two principals; until now there was one.
--
-- Deliberately NOT `grant service_role to advit_service`.
--
-- Supabase's `service_role` carries BYPASSRLS, and BYPASSRLS is a CLUSTER-wide
-- property, not a schema-scoped one. This database is shared with an unrelated
-- HRMS product under the same Postgres instance, so handing a new
-- password-holding credential service_role would make a leaked runtime password
-- full read/write over somebody else's employee records. advit_backend is a
-- plain non-BYPASSRLS group role whose reach is exactly the grants and policies
-- written below - and a t_advit table added later without a policy fails the
-- service path LOUDLY rather than silently widening it.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. advit_backend: the governance spine's privilege set.
--
--    NOLOGIN on purpose. It is a set of privileges that gets WORN, never a
--    credential that gets CONNECTED as, so the password and the privilege
--    rotate separately and `pg_stat_activity.usename` still names which process
--    is holding the connection.
-- ---------------------------------------------------------------------------
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'advit_backend') then
    create role advit_backend nologin;
  end if;
end $$;

grant usage on schema core, t_advit to advit_backend;

do $grants$
declare r record;
begin
  for r in
    select schemaname as s, tablename as t
      from pg_tables
     where schemaname in ('core', 't_advit')
       -- core.audit_log is excluded on purpose. Its ONLY writer is
       -- core.log_audit, which is SECURITY DEFINER and therefore needs no table
       -- grant to do its job. A direct INSERT grant here would let a bug - or a
       -- later convenience - write an unattributed row into the append-only
       -- trail, which is the exact property 20260907000001 exists to protect.
       and not (schemaname = 'core' and tablename = 'audit_log')
  loop
    -- No DELETE, anywhere, and no TRUNCATE. The runtime corrects rows; it never
    -- removes tenant history. A missing grant is a louder failure than a
    -- missing WHERE clause.
    execute format('grant select, insert, update on %I.%I to advit_backend', r.s, r.t);

    -- RLS is enabled on all 46 tables in these schemas and every existing
    -- policy is `to authenticated`, so WITHOUT a policy of its own the backend
    -- role would see zero rows and have every write refused - the grant above
    -- is necessary and not sufficient. An RLS policy naming a group role does
    -- apply to a login role that INHERITs it, which is what makes advit_service
    -- work without ever calling `set role`.
    execute format(
      'create policy advit_backend_all on %I.%I for all to advit_backend '
      'using (true) with check (true)', r.s, r.t);
  end loop;
end
$grants$;

-- No sequence grants, and the absence is deliberate rather than an oversight.
--
-- Both sequences in these schemas (core.audit_log_id_seq, core.usage_events_id_seq)
-- back `generated always as identity` columns, whose use is authorised by the
-- INSERT privilege on the table rather than by USAGE on the sequence. The
-- obvious loop over `information_schema.sequences` would also have granted
-- nothing while appearing to work: that view omits sequences owned by a column,
-- so it returns zero rows here.

-- 20260910000003 revoked EXECUTE from PUBLIC on every function in these
-- schemas, so the backend needs grants of its own rather than inheriting the
-- blanket one. This is what lets it call core.log_audit with actor_type
-- 'agent'/'automation' and t_advit.compute_blended_daily, both of which are
-- revoked from `authenticated` precisely so a tenant cannot reach them.
do $fns$
declare r record;
begin
  for r in
    select n.nspname s, p.proname f, pg_get_function_identity_arguments(p.oid) a
      from pg_proc p
      join pg_namespace n on n.oid = p.pronamespace
     where n.nspname in ('core', 't_advit')
       -- Functions and procedures only. `grant execute on function` raises on
       -- an aggregate or a window function, and one of those in these schemas
       -- later would fail this migration for no security reason.
       and p.prokind in ('f', 'p')
  loop
    execute format('grant execute on function %I.%I(%s) to advit_backend', r.s, r.f, r.a);
  end loop;
end
$fns$;


-- ---------------------------------------------------------------------------
-- 2. advit_tenant: owns nothing, reads nothing. Its ONLY power is the ability
--    to become `authenticated`.
--
--    NOINHERIT is the whole trick, and it is the difference between failing
--    closed by construction and failing closed by discipline. On a connection
--    that forgets `set local role authenticated`, the first statement raises
--    `permission denied for schema t_advit` - loudly, in development, on the
--    first request. With INHERIT the same mistake would run silently with
--    `authenticated`'s grants and NO claims, so auth.uid() would be null; every
--    RLS policy would match nothing, the route would return an empty list, and
--    it would look like a tenant with no data rather than like a bug.
--
--    It would also carry HRMS's grants to `authenticated` onto our connection,
--    which is not ours to hold.
-- ---------------------------------------------------------------------------
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'advit_tenant') then
    create role advit_tenant nologin noinherit;
  end if;
end $$;

grant authenticated to advit_tenant;


-- ---------------------------------------------------------------------------
-- 3. advit_service / advit_jobs: INHERIT members of advit_backend.
--
--    No `set role` is needed on these connections, and that is the point rather
--    than a convenience. `current_setting('role', true)` stays 'none', which is
--    exactly the shape core.assert_org_visible (20260910000003) recognises as
--    the backend - a role of 'anon' or 'authenticated' with no subject is
--    refused, and only a request with no PostgREST role at all is trusted. And
--    auth.uid() is null, so core.log_audit takes its trusted branch and will
--    honour p_actor_type => 'agent' / 'automation'.
--
--    Two credentials rather than one, so the API and the unattended job process
--    are separately revocable and separately visible in pg_stat_activity.
-- ---------------------------------------------------------------------------
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'advit_service') then
    create role advit_service nologin inherit;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'advit_jobs') then
    create role advit_jobs nologin inherit;
  end if;
end $$;

grant advit_backend to advit_service, advit_jobs;

-- Ceilings, not reservations - a connection limit caps how many sessions a role
-- may hold open, it does not set any aside. Sized from the pools rather than
-- guessed, per API replica:
--
--   tenant pool max 10, service pool max 5, plus PostgresLockManager's
--   un-pooled direct connection (semaphore-capped at 4 concurrent mutations).
--
-- Two replicas, doubling briefly during a rolling deploy:
--   advit_tenant   2 x 10      x 2 = 40
--   advit_service  2 x (5 + 4) x 2 = 36
--   advit_jobs     1 x (3 + 2)     = 10
--
-- The point of the numbers is not the total. It is that each role's ceiling sits
-- just above what its own pools can ask for, so a connection leak in one process
-- is refused at that role's ceiling instead of consuming the cluster's
-- max_connections and taking HRMS down with it. Pool sizing, not this ceiling,
-- is what keeps the shared cluster inside its budget.
alter role advit_tenant  connection limit 40;
alter role advit_service connection limit 36;
alter role advit_jobs    connection limit 10;

comment on role advit_tenant is
  'Agent-runtime tenant connection. Owns nothing, is not BYPASSRLS, and can do '
  'nothing at all until it explicitly becomes `authenticated`. RLS is the '
  'tenancy boundary on this path - the same policies '
  'packages/saas-core-db/tests already exercise through conftest.acting_as.';

comment on role advit_backend is
  'Governance spine: the action and audit writes, t_advit.secrets, and the '
  'guardrail arithmetic. NOT a member of service_role - its reach is core + '
  't_advit only, so a leaked runtime password cannot read the unrelated HRMS '
  'product sharing this cluster.';

comment on role advit_service is
  'The API process. INHERITs advit_backend so current_setting(''role'') stays '
  '''none'' and auth.uid() is null, which is the shape core.assert_org_visible '
  'and core.log_audit recognise as the trusted backend.';

comment on role advit_jobs is
  'The unattended scheduler process. Separately revocable from advit_service so '
  'a compromised job runner can be cut off without taking the API down, and '
  'separately visible in pg_stat_activity.usename.';


