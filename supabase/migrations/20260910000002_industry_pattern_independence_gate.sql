-- =============================================================================
-- Fix: the T1 -> T2 promotion gate did not hold
--
-- PRD 5.1 makes promotion from account intelligence to industry intelligence
-- deliberately strict, because this is where systems of this kind quietly
-- corrupt themselves: one account's coincidence becomes an industry "law", and
-- then the product confidently gives every customer the same bad advice. The
-- gate requires a pattern to have been seen in at least 3 distinct workspaces
-- under at least 2 distinct owners.
--
-- The constraint that was supposed to enforce it:
--
--   check (status <> 'active' or array_length(evidence_workspaces, 1) >= 3)
--
-- with `evidence_workspaces text[] not null default '{}'`.
--
-- `array_length('{}', 1)` returns NULL, not 0. `NULL >= 3` is NULL. And a CHECK
-- constraint passes on NULL - only FALSE fails. So the default value made the
-- gate unconditionally true: a row could be inserted as `active` with zero
-- supporting evidence, and `status` defaulted to 'active', so that was also the
-- path of least resistance.
--
-- Verified against the running database before writing this:
--
--   select array_length('{}'::text[], 1) >= 3 is not false;  -- t
--
-- Two further gaps the original never expressed at all:
--
--   * Distinctness. array_length counts entries, not distinct ones, so
--     {w1, w1, w1} would have satisfied "3 distinct workspaces".
--   * Owners. The 2-distinct-owner half of the gate was simply absent. It is
--     the half that matters most - three workspaces belonging to one agency
--     running one playbook are not three independent observations.
-- =============================================================================

-- CHECK constraints cannot contain subqueries, so the distinct count needs an
-- IMMUTABLE helper. It reads only its argument, never a table.
create or replace function t_advit.distinct_count(p_values text[])
returns integer
language sql
immutable
parallel safe
set search_path = pg_catalog
as $fn$
  select count(distinct v)::integer
    from unnest(coalesce(p_values, '{}'::text[])) as v
   where v is not null and v <> '';
$fn$;

comment on function t_advit.distinct_count(text[]) is
  'Distinct, non-empty entries in an array. Exists because a CHECK constraint '
  'cannot contain a subquery, and array_length would have counted duplicates.';


-- Hashed owner identifiers, alongside the hashed workspace identifiers already
-- carried. Still reproducible from the audit trail, still naming nobody (D7).
alter table t_advit.industry_patterns
  add column if not exists evidence_owners text[] not null default '{}';

comment on column t_advit.industry_patterns.evidence_owners is
  'Hashed owner/agency ids behind the supporting observations. Three workspaces '
  'under one agency running one playbook are not three independent observations.';


alter table t_advit.industry_patterns
  drop constraint if exists industry_patterns_independence;

alter table t_advit.industry_patterns
  add constraint industry_patterns_independence check (
    status <> 'active'
    or (
      t_advit.distinct_count(evidence_workspaces) >= 3
      and t_advit.distinct_count(evidence_owners) >= 2
    )
  );


-- `active` was the column default, so the most dangerous state was also the one
-- you got by not thinking about it. Promoting a pattern to industry truth is a
-- decision; it now has to be stated.
alter table t_advit.industry_patterns
  alter column status drop default;

-- evidence_n is a denormalised count that can drift from the array it describes.
-- The array is authoritative; this keeps the two from disagreeing.
alter table t_advit.industry_patterns
  drop constraint if exists industry_patterns_evidence_n_matches;

alter table t_advit.industry_patterns
  add constraint industry_patterns_evidence_n_matches check (
    evidence_n = 0 or evidence_n >= t_advit.distinct_count(evidence_workspaces)
  );

grant all on all functions in schema t_advit to service_role;
