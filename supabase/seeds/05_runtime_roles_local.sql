-- =============================================================================
-- LOCAL ONLY: give the runtime roles a way to log in.
--
-- 20260911000007 creates advit_tenant / advit_service / advit_jobs as NOLOGIN
-- and grants them their privileges. It deliberately stops there, because a
-- forward-only migration is committed to git and must never carry a secret.
--
-- Seeds run only on `supabase db reset`, against the local Docker Postgres on
-- 54322, which is bound to 127.0.0.1 and holds nothing but fixture data. These
-- passwords are therefore not credentials; they are the local equivalent of
-- `postgres:postgres`, and they are in git for the same reason that one is.
--
-- PRODUCTION uses the same three `alter role` statements from a Coolify
-- pre-deploy step reading generated secrets. If this file is ever the thing
-- that granted LOGIN in production, something has gone wrong upstream of it.
-- =============================================================================

alter role advit_tenant  login password 'advit_tenant_local';
alter role advit_service login password 'advit_service_local';
alter role advit_jobs    login password 'advit_jobs_local';
