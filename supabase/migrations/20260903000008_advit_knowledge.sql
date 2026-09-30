-- =============================================================================
-- ad-vit - experiments, knowledge graph, shared tiers, policy rules
--
-- Shared-tier rows (industry_patterns, platform_knowledge, policy_rules) are
-- never workspace-scoped. Promotion from an account learning into the industry
-- tier is a gated, anonymised, human-approved pipeline (PRD 5.1, D7) - not an
-- ordinary write. The promotion service is the only code path permitted to
-- create an industry row, and it strips identifiers by construction.
-- =============================================================================

create type t_advit.experiment_status as enum (
  'backlog', 'selected', 'designed', 'approved', 'running', 'measured', 'concluded'
);

create type t_advit.experiment_verdict as enum (
  'winner', 'loser', 'inconclusive', 'invalidated'
);

create type t_advit.policy_rule_type as enum (
  'term_list',    -- deterministic match against a condition or claim list
  'regex',        -- pattern match over copy
  'llm_judge',    -- semantic adjudication (screened cheaply, judged expensively)
  'state_check'   -- a declared fact must be present, e.g. AI disclosure, licence
);

create type t_advit.policy_severity as enum ('block', 'warn', 'info');

-- ---------------------------------------------------------------------------
-- Experiments
-- ---------------------------------------------------------------------------

create table t_advit.experiments (
  id                uuid primary key default gen_random_uuid(),
  workspace_id      uuid not null references t_advit.workspaces(id) on delete cascade,
  hypothesis        text not null,
  variable          text not null,
  control_json      jsonb not null default '{}'::jsonb,
  variant_json      jsonb not null default '{}'::jsonb,

  -- Pre-registration is mandatory and immutable once set (PRD 9.4, FR-032).
  primary_metric    text not null,
  guardrail_metrics text[] not null default '{}',
  baseline          numeric(12,6),
  mde               numeric(12,6),
  alpha             numeric(5,4) not null default 0.05,
  power             numeric(5,4) not null default 0.80,
  required_n        integer,
  planned_days      integer,
  decision_rule     text,

  status            t_advit.experiment_status not null default 'backlog',
  meta_test_id      text,
  pre_registered_at timestamptz,
  started_at        timestamptz,
  concluded_at      timestamptz,

  -- Not every comparison is an experiment. Under 2026 consolidated delivery an
  -- in-ad-set creative test measures which creative the algorithm chose to
  -- spend on - useful, but observational, and labelled as such (PRD 9.3).
  is_observational  boolean not null default false,

  created_at        timestamptz not null default now(),
  constraint experiments_alpha_range check (alpha > 0 and alpha < 1),
  constraint experiments_power_range check (power > 0 and power < 1),
  constraint experiments_registered_before_running check (
    status not in ('running','measured','concluded') or pre_registered_at is not null
  )
);

create index experiments_workspace_idx on t_advit.experiments (workspace_id, status);

create table t_advit.experiment_results (
  id            uuid primary key default gen_random_uuid(),
  experiment_id uuid not null references t_advit.experiments(id) on delete cascade,
  arm           text not null,
  n             integer not null default 0,
  metric_value  numeric(16,6),
  ci_low        numeric(16,6),
  ci_high       numeric(16,6),
  verdict       t_advit.experiment_verdict,
  concluded_at  timestamptz,
  unique (experiment_id, arm)
);

-- ---------------------------------------------------------------------------
-- Marketing knowledge graph (PRD 8.3)
-- ---------------------------------------------------------------------------

create table t_advit.kg_nodes (
  id           uuid primary key default gen_random_uuid(),
  workspace_id uuid references t_advit.workspaces(id) on delete cascade,
  tier         t_advit.knowledge_tier not null default 'account',
  node_type    text not null,
  props_json   jsonb not null default '{}'::jsonb,
  embedding    extensions.vector(1536),
  created_at   timestamptz not null default now(),
  constraint kg_nodes_tier_scoping check (
    (tier = 'account' and workspace_id is not null)
    or (tier <> 'account' and workspace_id is null)
  )
);

create index kg_nodes_workspace_type_idx on t_advit.kg_nodes (workspace_id, node_type);

create table t_advit.kg_edges (
  id          uuid primary key default gen_random_uuid(),
  src_id      uuid not null references t_advit.kg_nodes(id) on delete cascade,
  dst_id      uuid not null references t_advit.kg_nodes(id) on delete cascade,
  edge_type   text not null,
  props_json  jsonb not null default '{}'::jsonb,
  observed_at timestamptz not null default now(),
  evidence_n  integer not null default 1,
  effect_size numeric(12,6),
  confidence  numeric(4,3),
  tier        t_advit.knowledge_tier not null default 'account',
  valid_from  timestamptz not null default now(),
  valid_to    timestamptz,
  constraint kg_edges_confidence_range check (confidence is null or confidence between 0 and 1)
);

create index kg_edges_src_idx on t_advit.kg_edges (src_id, edge_type);
create index kg_edges_dst_idx on t_advit.kg_edges (dst_id, edge_type);

-- ---------------------------------------------------------------------------
-- Shared tiers
-- ---------------------------------------------------------------------------

create table t_advit.industry_patterns (
  id                  uuid primary key default gen_random_uuid(),
  business_type       t_advit.business_type not null,
  pattern_type        text not null,
  statement           text not null,
  conditions_json     jsonb not null default '{}'::jsonb,
  effect_direction    text,
  effect_size         numeric(12,6),
  confidence          numeric(4,3) not null default 0.500,

  -- Hashed workspace identifiers only. The promoted record is reproducible
  -- from its supporting observations without naming any of them (PRD D7).
  evidence_workspaces text[] not null default '{}',
  evidence_n          integer not null default 0,

  approved_by         uuid references core.platform_users(id) on delete set null,
  approved_at         timestamptz,
  valid_from          timestamptz not null default now(),
  valid_to            timestamptz,
  status              t_advit.learning_status not null default 'active',
  created_at          timestamptz not null default now(),

  -- Independence gate: at least 3 distinct workspaces (PRD 5.1).
  constraint industry_patterns_independence check (
    status <> 'active' or array_length(evidence_workspaces, 1) >= 3
  ),
  constraint industry_patterns_requires_approval check (
    status <> 'active' or approved_by is not null
  )
);

comment on table t_advit.industry_patterns is
  'The dangerous version of this product is one that becomes confidently wrong. '
  'A pattern from 11 conversions in a festival week, promoted to industry truth, '
  'would steer every account into a bad decision. The constraints here exist to '
  'make that outcome expensive rather than automatic (PRD 8.5).';

create table t_advit.platform_knowledge (
  id         uuid primary key default gen_random_uuid(),
  topic      text not null,
  statement  text not null,
  source_url text,
  as_of      date not null,
  severity   text,
  status     t_advit.learning_status not null default 'active',
  created_at timestamptz not null default now()
);

create index platform_knowledge_topic_idx on t_advit.platform_knowledge (topic, as_of desc);

-- ---------------------------------------------------------------------------
-- Policy rules - the compliance gate's ruleset (PRD 13)
-- ---------------------------------------------------------------------------

create table t_advit.policy_rules (
  id              uuid primary key default gen_random_uuid(),
  code            text not null unique,
  jurisdiction    text not null,
  instrument      text not null,
  gate_stage      smallint not null,
  rule_type       t_advit.policy_rule_type not null,
  title           text not null,

  pattern         text,
  terms           text[],

  severity        t_advit.policy_severity not null,
  explanation     text not null,
  remedy_template text,

  -- "Blocked because policy" is unacceptable. Every block cites the clause,
  -- the source and the effective date (PRD 13.5).
  source_url      text not null,
  as_of           date not null,

  -- Empty means the rule applies to every pack.
  business_types  t_advit.business_type[] not null default '{}',
  is_active       boolean not null default true,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),

  constraint policy_rules_jurisdiction_valid check (jurisdiction in ('meta', 'in')),
  constraint policy_rules_gate_stage_range check (gate_stage between 1 and 9),
  constraint policy_rules_has_matcher check (
    (rule_type = 'regex'      and pattern is not null)
    or (rule_type = 'term_list'  and terms is not null)
    or (rule_type in ('llm_judge','state_check'))
  )
);

create index policy_rules_active_idx
  on t_advit.policy_rules (gate_stage, jurisdiction)
  where is_active;

create trigger policy_rules_touch
  before update on t_advit.policy_rules
  for each row execute function core.touch_updated_at();

-- Compliance verdicts, kept so the OS can correlate its own calls against real
-- Meta outcomes and tune the classifier. A false-block rate that is too high is
-- as much a defect as a miss, because it teaches owners to override the gate
-- (PRD 13.4).
create table t_advit.compliance_checks (
  id            uuid primary key default gen_random_uuid(),
  workspace_id  uuid not null references t_advit.workspaces(id) on delete cascade,
  creative_id   uuid references t_advit.creatives(id) on delete set null,
  run_id        uuid references t_advit.runs(id) on delete set null,
  bundle_json   jsonb not null,
  verdict       t_advit.compliance_verdict not null,
  findings_json jsonb not null default '[]'::jsonb,
  overall_risk  numeric(4,3),
  stages_evaluated smallint[] not null default '{}',
  stages_skipped   smallint[] not null default '{}',

  -- Recorded when Meta actually rules, so precision and recall are measured
  -- against real outcomes rather than a synthetic set (PRD 21.2).
  meta_outcome  text,
  meta_outcome_at timestamptz,

  overridden_by      uuid references core.platform_users(id) on delete set null,
  override_reason    text,
  overridden_at      timestamptz,

  created_at    timestamptz not null default now(),
  constraint compliance_override_requires_reason check (
    overridden_by is null or override_reason is not null
  )
);

create index compliance_checks_workspace_idx on t_advit.compliance_checks (workspace_id, created_at desc);

-- ---------------------------------------------------------------------------
-- Row-level security
-- ---------------------------------------------------------------------------

alter table t_advit.experiments        enable row level security;
alter table t_advit.experiment_results enable row level security;
alter table t_advit.kg_nodes           enable row level security;
alter table t_advit.kg_edges           enable row level security;
alter table t_advit.industry_patterns  enable row level security;
alter table t_advit.platform_knowledge enable row level security;
alter table t_advit.policy_rules       enable row level security;
alter table t_advit.compliance_checks  enable row level security;

do $policies$
declare t text;
begin
  foreach t in array array['experiments','compliance_checks']
  loop
    execute format(
      'create policy %I on t_advit.%I for select to authenticated
         using (t_advit.is_workspace_member(workspace_id) or core.is_superadmin())',
      t || '_select', t
    );
    execute format('grant select on t_advit.%I to authenticated', t);
  end loop;
end;
$policies$;

create policy experiment_results_select on t_advit.experiment_results
  for select to authenticated
  using (exists (
    select 1 from t_advit.experiments e
     where e.id = experiment_id
       and (t_advit.is_workspace_member(e.workspace_id) or core.is_superadmin())
  ));

create policy kg_nodes_select on t_advit.kg_nodes
  for select to authenticated
  using (
    core.is_superadmin()
    or (tier = 'account' and t_advit.is_workspace_member(workspace_id))
    or tier in ('industry','global')
  );

create policy kg_edges_select on t_advit.kg_edges
  for select to authenticated
  using (exists (
    select 1 from t_advit.kg_nodes n
     where n.id = src_id
       and (core.is_superadmin()
            or (n.tier = 'account' and t_advit.is_workspace_member(n.workspace_id))
            or n.tier in ('industry','global'))
  ));

-- Shared tiers are readable by every signed-in user and writable by nobody
-- through the data API. Industry promotion runs in the promotion service under
-- the service role; policy rules are maintained by the steward.
create policy industry_patterns_select on t_advit.industry_patterns
  for select to authenticated using (status = 'active' or core.is_superadmin());

create policy platform_knowledge_select on t_advit.platform_knowledge
  for select to authenticated using (true);

create policy policy_rules_select on t_advit.policy_rules
  for select to authenticated using (true);

grant select on t_advit.experiment_results to authenticated;
grant select on t_advit.kg_nodes           to authenticated;
grant select on t_advit.kg_edges           to authenticated;
grant select on t_advit.industry_patterns  to authenticated;
grant select on t_advit.platform_knowledge to authenticated;
grant select on t_advit.policy_rules       to authenticated;

grant all on all tables in schema t_advit to service_role;
