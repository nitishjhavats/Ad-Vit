-- =============================================================================
-- Industries become data
--
-- `t_advit.business_type` was an enum with two labels, and it decided which
-- body of statute an advertiser is held to. Real estate is the next pack, and
-- under an enum "add real estate" means: alter the type, alter three consumers,
-- rewrite a seed, redeploy the runtime whose Pydantic Literal repeats the same
-- two labels (app/main.py:105), and hope nothing else spelled the labels out.
-- That is a release, for what is a fact about the world rather than a fact
-- about the code.
--
-- Three consumers move here, and they are not equally awkward:
--
--   t_advit.workspaces.business_type          -> industry_key, a real FK
--   t_advit.industry_patterns.business_type   -> industry_key, a real FK
--   t_advit.policy_rules.business_types[]     -> a junction table
--
-- The array is the awkward one and it is worth saying exactly why. Postgres has
-- no foreign key over array ELEMENTS. `business_types t_advit.business_type[]`
-- got its integrity from the element type, and that integrity vanishes the
-- moment the type becomes text. The alternatives were: a trigger on
-- policy_rules validating every element, plus a second trigger on industries
-- validating every referencing row on rename and on delete - that is a foreign
-- key, re-implemented badly, in two places - or a junction table that simply
-- has one. The junction table wins, and it also closes a second defect the
-- array carried:
--
--   `business_types = '{}'` meant "applies to every pack". Absence meaning
--   universality is the shape that has already cost this repo twice
--   (array_length('{}',1) is NULL; a CHECK passes on NULL). A rule that listed
--   no industry because nobody filled it in was indistinguishable from a rule
--   deliberately scoped to all of them.
--
-- So scope is now DECLARED: policy_rules.scope is 'all_industries' or
-- 'listed_industries', and a deferred constraint trigger holds the two in
-- agreement. An empty list is no longer a decision you can make by accident.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- The industries table
-- ---------------------------------------------------------------------------

create type t_advit.industry_status as enum ('draft', 'active', 'deprecated');

comment on type t_advit.industry_status is
  'draft: being authored, invisible to tenants. active: sold. deprecated: no '
  'new workspaces, existing ones keep working - a pack is never deleted while a '
  'workspace still points at it.';

create table t_advit.industries (
  key            text primary key,
  display_name   text not null,

  -- No default. `active` would be the dangerous value, and a default is what
  -- you get by not thinking - the same reasoning that removed the default from
  -- industry_patterns.status in 20260910000002.
  status         t_advit.industry_status not null,

  pack_version   integer not null default 1,

  -- workspaces.industry_pack_id used to hold this string, per workspace, by
  -- hand. It had already drifted: the general_d2c workspace carried
  -- 'general@1'. Derived here, it cannot.
  pack_id        text generated always as (key || '@' || pack_version::text) stored,

  summary        text not null,

  -- Which body of law this pack encodes, in one sentence, for the operator
  -- authoring rules against it. Not legal advice, and not machine-read.
  statutory_note text,

  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),

  -- The key is a stable identifier that appears in seeds, in API payloads and
  -- in cache keys. Constrain its shape once, here, rather than discovering a
  -- 'Real Estate' and a 'real-estate' in production.
  constraint industries_key_shape check (key ~ '^[a-z][a-z0-9_]{2,38}$'),
  constraint industries_pack_version_positive check (pack_version >= 1),
  constraint industries_pack_id_unique unique (pack_id)
);

comment on table t_advit.industries is
  'One row per industry pack. Adding an industry is an INSERT here plus rows in '
  'policy_rules; it is not a migration and not a deploy. See '
  'supabase/seeds/04_real_estate_pack.sql for the whole of a second pack.';

comment on column t_advit.industries.pack_id is
  'key@pack_version, e.g. ayurveda@1. Derived, because the hand-maintained '
  'workspaces.industry_pack_id had already disagreed with the workspace''s own '
  'business_type. Reported in every compliance verdict so an owner can tell '
  'which revision of the ruleset judged their ad.';

create trigger industries_touch
  before update on t_advit.industries
  for each row execute function core.touch_updated_at();


-- The two identities the seeds and the tests already use. They keep the exact
-- spelling of the enum labels they replace, so `business_type::text` backfills
-- cleanly and packages/saas-core-db/tests/test_policy_rules_seed.py keeps
-- asserting the same string.
insert into t_advit.industries
  (key, display_name, status, pack_version, summary, statutory_note)
values
  ('ayurveda', 'Ayurveda / AYUSH', 'active', 1,
   'Ayurvedic and AYUSH-licensed products sold direct to consumers in India.',
   'Drugs & Magic Remedies (Objectionable Advertisements) Act Schedule J, Ministry '
   'of AYUSH advertising guidance and the ASCI code, on top of Meta health policy.'),
  ('general_d2c', 'General D2C', 'active', 1,
   'Direct-to-consumer sellers with no category-specific statutory layer.',
   'Meta advertising policy and the DPDP Act only. Deliberately carries no India '
   'health layer: this is the pack that must NOT block "we cleared our piles of stock".');


-- Pack defaults for T1 memory. supabase/seeds/03_marketing_workspace.sql wrote
-- these by hand for the one Ayurveda workspace and labelled them
-- source = 'industry_pack' - a claim the schema could not back, because no
-- industry pack existed to source them from. Onboarding copies these rows into
-- t_advit.account_context.
create table t_advit.industry_context_defaults (
  industry_key text not null references t_advit.industries(key)
                 on update cascade on delete cascade,
  dimension    text not null,
  key          text not null,
  value_json   jsonb not null,
  confidence   numeric(4,3) not null default 0.900,
  primary key (industry_key, dimension, key),
  constraint industry_context_defaults_confidence_range check (confidence between 0 and 1)
);

comment on table t_advit.industry_context_defaults is
  'What a workspace in this industry starts out believing. Copied into '
  't_advit.account_context at onboarding with source = ''industry_pack''. '
  'These are the pack''s starting hypotheses, not facts - the OS tests them '
  '(PRD 6.2), which is why they land in a tenant table rather than being read '
  'from here at decision time.';


-- ---------------------------------------------------------------------------
-- Consumer 1: workspaces
-- ---------------------------------------------------------------------------

alter table t_advit.workspaces add column industry_key text;

update t_advit.workspaces set industry_key = business_type::text;

alter table t_advit.workspaces
  alter column industry_key set not null,
  alter column industry_key set default 'general_d2c',
  add constraint workspaces_industry_fk
    foreign key (industry_key) references t_advit.industries(key)
    on update cascade on delete restrict;

create index workspaces_industry_idx on t_advit.workspaces (industry_key);

alter table t_advit.workspaces drop column business_type;

comment on column t_advit.workspaces.industry_key is
  'The authoritative answer to "which statutory layer applies to this '
  'advertiser". The compliance node must read it from HERE. It used to be '
  'derived by scanning t_advit.account_context for a row keyed '
  '"business_type" - a table every workspace member can INSERT into, and which '
  'has never contained such a row, so a hardcoded default won instead. Chosen at '
  'onboarding and not tenant-updatable thereafter - see workspaces_industry_guard.';

-- industry_pack_id goes. Three reasons, in order of weight:
--
--   1. Nothing reads it. Grep the runtime and the web app: zero references.
--   2. It had already drifted from the column beside it - the general_d2c
--      workspace carried 'general@1'.
--   3. It looks like a per-workspace version pin, and there is nothing behind
--      it that could honour one: policy_rules carries no pack version, so there
--      is exactly one live revision of a pack, and pinning a workspace to an
--      older one would silently serve it the current rules. A stored value that
--      promises what the schema cannot deliver is worse than no value.
--
-- The pack identity a caller actually wants is t_advit.industries.pack_id,
-- joined through industry_key. Per-workspace pinning, if it is ever wanted,
-- means versioning policy_rules - which is a migration, and an honest one.
alter table t_advit.workspaces drop column industry_pack_id;


-- The audit finding this closes: `compliance()` chose its ruleset by scanning
-- t_advit.account_context, which any workspace member can write. Reproduced
-- before the fix - injecting one row valued `general_d2c` made both India-layer
-- blocks disappear, so a tenant could switch off the statutory layer that
-- governs them.
--
-- Moving that read to t_advit.workspaces is only half of it. The column has
-- to be one a tenant cannot set either, or the hole simply moves.
--
-- The two `drop if exists` lines below are deliberate no-ops in a fresh
-- database. A parallel draft guarded the old column under a different name;
-- keeping the drops means neither ordering leaves a second, differently-worded
-- guard behind - and whichever one someone read first, they would believe they
-- had read the rule.
drop trigger if exists workspaces_business_type_guard on t_advit.workspaces;
drop function if exists t_advit.guard_workspace_business_type();

create or replace function t_advit.guard_workspace_industry()
returns trigger
language plpgsql
security definer
set search_path = t_advit, core, pg_catalog
as $fn$
begin
  -- `before update of industry_key` fires whenever the column appears in the
  -- SET list, changed or not. An unchanged value is not a change.
  if new.industry_key is not distinct from old.industry_key then
    return new;
  end if;

  -- The test for a tenant request is auth.uid(): PostgREST binds the JWT
  -- subject for a signed-in user and leaves it null for the service role and
  -- for a direct connection. Inside a SECURITY DEFINER function current_user is
  -- the owner, so it cannot make this distinction; auth.uid() can.
  if auth.uid() is not null then
    raise exception
      'industry_key selects the compliance ruleset and cannot be changed by a tenant'
      using errcode = '42501',
            hint = 'industry_not_self_service',
            detail = format(
              'workspace %s: %s -> %s refused. Changing the industry pack changes '
              'which statutory rules are loaded. Raise it with support.',
              old.id, old.industry_key, new.industry_key
            );
  end if;

  -- Backend-originated, and therefore permitted - but never silent. A change of
  -- applicable law is the kind of event an audit reader must be able to find
  -- later without knowing to look for it.
  perform core.log_audit(
    'workspace', 'workspace.industry_changed',
    p_org        => old.org_id,
    p_workspace  => old.id,
    p_actor_type => 'system',
    p_actor      => null,
    p_payload    => jsonb_build_object(
                      'from', old.industry_key,
                      'to',   new.industry_key
                    )
  );

  return new;
end;
$fn$;

comment on function t_advit.guard_workspace_industry() is
  'Never trust a caller-supplied value for a decision that constrains the '
  'caller. The industry selects the statutory ruleset, so re-pointing it is a '
  'steward action, not self-service. INSERT is untouched: choosing a pack when '
  'the workspace is created is the self-service step this product wants.';

create trigger workspaces_industry_guard
  before update of industry_key on t_advit.workspaces
  for each row execute function t_advit.guard_workspace_industry();


-- ---------------------------------------------------------------------------
-- Consumer 2: industry_patterns (T2 shared tier)
-- ---------------------------------------------------------------------------

alter table t_advit.industry_patterns add column industry_key text;

update t_advit.industry_patterns set industry_key = business_type::text;

alter table t_advit.industry_patterns
  alter column industry_key set not null,
  add constraint industry_patterns_industry_fk
    foreign key (industry_key) references t_advit.industries(key)
    on update cascade on delete restrict;

create index industry_patterns_industry_idx
  on t_advit.industry_patterns (industry_key, pattern_type);

alter table t_advit.industry_patterns drop column business_type;


-- ---------------------------------------------------------------------------
-- Consumer 3: policy_rules.business_types[] -> declared scope + junction
-- ---------------------------------------------------------------------------

create type t_advit.policy_scope as enum ('all_industries', 'listed_industries');

comment on type t_advit.policy_scope is
  'Whether a rule applies to every pack or to a named list. Explicit, because '
  'the array it replaces used the empty set to mean "all", which is '
  'indistinguishable from "nobody filled this in".';

alter table t_advit.policy_rules add column scope t_advit.policy_scope;

update t_advit.policy_rules
   set scope = case
                 when coalesce(array_length(business_types, 1), 0) = 0
                   then 'all_industries'
                 else 'listed_industries'
               end::t_advit.policy_scope;

alter table t_advit.policy_rules alter column scope set not null;

create table t_advit.policy_rule_industries (
  -- The code, not the id. It is unique, it is what the seeds and the tests name
  -- a rule by, and `on update cascade` means a rule can be renamed without
  -- orphaning its scope.
  rule_code    text not null references t_advit.policy_rules(code)
                 on update cascade on delete cascade,
  industry_key text not null references t_advit.industries(key)
                 on update cascade on delete restrict,
  primary key (rule_code, industry_key)
);

comment on table t_advit.policy_rule_industries is
  'Which industries a listed_industries rule applies to. A junction table '
  'rather than an array because Postgres cannot foreign-key array elements, and '
  'the integrity the enum element type used to provide had to come from '
  'somewhere.';

create index policy_rule_industries_industry_idx
  on t_advit.policy_rule_industries (industry_key);

insert into t_advit.policy_rule_industries (rule_code, industry_key)
select r.code, unnest(r.business_types)::text
  from t_advit.policy_rules r
 where coalesce(array_length(r.business_types, 1), 0) > 0;

alter table t_advit.policy_rules drop column business_types;


-- scope and the junction rows have to agree, and neither table can state the
-- rule alone. A constraint trigger can, and DEFERRABLE INITIALLY DEFERRED means
-- a seed may insert the rule and its industries in either order inside one
-- transaction - which is the whole point of a pack being seed data.
create or replace function t_advit.assert_policy_rule_scope()
returns trigger
language plpgsql
set search_path = t_advit, pg_catalog
as $fn$
declare
  v_code  text;
  v_scope t_advit.policy_scope;
  v_n     integer;
begin
  if tg_table_name = 'policy_rules' then
    v_code := case when tg_op = 'DELETE' then old.code else new.code end;
  else
    v_code := case when tg_op = 'DELETE' then old.rule_code else new.rule_code end;
  end if;

  select r.scope into v_scope from t_advit.policy_rules r where r.code = v_code;

  -- The rule itself was deleted; its junction rows went with it by cascade.
  if v_scope is null then
    return null;
  end if;

  select count(*) into v_n
    from t_advit.policy_rule_industries i where i.rule_code = v_code;

  if v_scope = 'listed_industries' and v_n = 0 then
    raise exception
      'policy rule % is scoped to listed_industries but names none', v_code
      using errcode = '23514', hint = 'rule_scope_lists_nothing';
  end if;

  if v_scope = 'all_industries' and v_n > 0 then
    raise exception
      'policy rule % is scoped to all_industries but also names % industry(ies)',
      v_code, v_n
      using errcode = '23514', hint = 'rule_scope_lists_industries';
  end if;

  return null;
end;
$fn$;

comment on function t_advit.assert_policy_rule_scope() is
  'Holds policy_rules.scope and policy_rule_industries in agreement. A '
  'listed_industries rule that names nothing would be silently dead - the '
  'statutory layer simply would not load, which is the same outcome as the '
  'tenant-switchable business_type finding, reached from the rule side.';

create constraint trigger policy_rules_scope_consistent
  after insert or update of scope on t_advit.policy_rules
  deferrable initially deferred
  for each row execute function t_advit.assert_policy_rule_scope();

create constraint trigger policy_rule_industries_scope_consistent
  after insert or delete on t_advit.policy_rule_industries
  deferrable initially deferred
  for each row execute function t_advit.assert_policy_rule_scope();


-- ---------------------------------------------------------------------------
-- The enum goes
--
-- Nothing references it any more. Verified against the running database before
-- writing this: the only three dependents were the three columns above, and no
-- function, view or policy mentioned it.
--
--   select c.relname, a.attname, format_type(a.atttypid, a.atttypmod)
--     from pg_attribute a
--     join pg_class c on c.oid = a.attrelid
--     join pg_type t on t.oid = a.atttypid
--     left join pg_type et on et.oid = t.typelem
--    where (t.typname = 'business_type' or et.typname = 'business_type')
--      and a.attnum > 0 and not a.attisdropped;
--
-- Leaving a dead type behind would be worse than useless: `::t_advit.
-- business_type` would keep compiling, so the next migration or test that
-- reaches for the familiar name (packages/saas-core-db/tests/
-- test_promotion_gate.py already casts to it) would re-couple to a type nothing
-- maintains - and would silently reject 'real_estate'.
-- ---------------------------------------------------------------------------

drop type t_advit.business_type;


-- ---------------------------------------------------------------------------
-- Row-level security
--
-- Industries and rule scoping are shared reference data, like policy_rules and
-- platform_knowledge: readable by every signed-in user, writable by nobody
-- through the data API. The steward maintains them under the service role.
-- ---------------------------------------------------------------------------

alter table t_advit.industries                enable row level security;
alter table t_advit.industry_context_defaults enable row level security;
alter table t_advit.policy_rule_industries    enable row level security;

-- A draft pack is being authored and is nobody's business yet. A deprecated one
-- stays readable, because workspaces still point at it and still have to render
-- their own industry's name.
create policy industries_select on t_advit.industries
  for select to authenticated
  using (status <> 'draft' or core.is_superadmin());

create policy industry_context_defaults_select on t_advit.industry_context_defaults
  for select to authenticated
  using (exists (
    select 1 from t_advit.industries i
     where i.key = industry_key
       and (i.status <> 'draft' or core.is_superadmin())
  ));

create policy policy_rule_industries_select on t_advit.policy_rule_industries
  for select to authenticated using (true);


-- ---------------------------------------------------------------------------
-- Grants
--
-- `grant all ... to service_role` is a snapshot taken when it runs, so the three
-- tables created above are invisible to the service role until it is re-issued
-- here.
-- ---------------------------------------------------------------------------

grant select on t_advit.industries                to authenticated;
grant select on t_advit.industry_context_defaults to authenticated;
grant select on t_advit.policy_rule_industries    to authenticated;

grant all on all tables    in schema t_advit to service_role;
grant all on all sequences in schema t_advit to service_role;
grant all on all functions in schema t_advit to service_role;

-- Postgres grants EXECUTE to PUBLIC on every new function, which is how `anon`
-- inherited the entitlement readers (20260910000003).
revoke execute on function t_advit.guard_workspace_industry()            from public;
revoke execute on function t_advit.assert_policy_rule_scope()            from public;
