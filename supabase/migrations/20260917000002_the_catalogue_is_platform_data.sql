-- =============================================================================
-- The catalogue is platform data, not a local fixture
--
-- core.products, core.feature_definitions, core.plans and core.plan_features
-- lived in supabase/seeds/01_core.sql from the first schema, beside five
-- auth.users with git-committed dev passwords - in a file whose own header
-- says NEVER to run it against a hosted environment. That was fine while the
-- only database was the one `supabase db reset` builds. It stopped being fine
-- on 2026-09-11, when the seed was applied to production once and the
-- catalogue froze at that moment: the four PRD 19.3 tiers were activated in
-- the seed on 2026-09-16 and production still shows them inactive, because
-- nothing ever applies a seed twice.
--
-- A seed is a fixture. A catalogue is what the product sells. This migration
-- carries the catalogue, so `apply_migration` - the one path by which schema
-- reaches production - carries it too, and a plan change is a migration like
-- any other change to what the system does.
--
-- Every statement CONVERGES (`on conflict ... do update`) rather than skipping
-- (`do nothing`): git is the source of truth for a price, a limit, a name, and
-- a database that disagrees is behind, not different. The fixed ids have been
-- stable since the first seed; core.subscriptions and core.coupon_plans point
-- at them, which is why the ids are pinned rather than generated.
--
-- On the production database this is a data change only - the rows already
-- exist from the seed - and the two seeded subscriptions are untouched because
-- they are not catalogue and stay in the seed.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Product catalogue
-- ---------------------------------------------------------------------------

insert into core.products (id, key, name)
values ('00000000-0000-4000-8000-000000000020', 'advit', 'ad-vit')
on conflict (key) do update set name = excluded.name;

-- ---------------------------------------------------------------------------
-- Feature definitions
--
-- Defaults here are the floor an organisation gets with no plan grant and no
-- override. They are deliberately conservative: PRD D4 starts every new
-- workspace at autonomy L1, and an unknown plan should never imply more.
-- ---------------------------------------------------------------------------

insert into core.feature_definitions (key, product_id, name, description, value_type, default_json)
values
  ('max_workspaces', '00000000-0000-4000-8000-000000000020',
   'Workspaces', 'Workspaces the organisation may create.', 'integer', '1'::jsonb),

  ('max_ad_accounts', '00000000-0000-4000-8000-000000000020',
   'Connected ad accounts', 'Meta ad accounts connectable across all workspaces.', 'integer', '1'::jsonb),

  ('max_seats', '00000000-0000-4000-8000-000000000020',
   'Seats', 'Members who may belong to the organisation.', 'integer', '3'::jsonb),

  ('max_autonomy_level', '00000000-0000-4000-8000-000000000020',
   'Autonomy ceiling',
   'Highest autonomy level (L0-L4, PRD 10.5) any workspace may reach. Effective '
   'autonomy is min(workspace setting, this ceiling).',
   'integer', '1'::jsonb),

  ('monthly_model_token_budget', '00000000-0000-4000-8000-000000000020',
   'Monthly model token budget',
   'Hard per-organisation token ceiling (PRD 17.7). Exhaustion degrades a run to '
   'a partial answer stating what was skipped - it never fails open.',
   'integer', '5000000'::jsonb),

  ('monitoring_cadence_minutes', '00000000-0000-4000-8000-000000000020',
   'Monitoring cadence',
   'Floor for the adaptive delivery-monitor interval (PRD 6.3).', 'integer', '60'::jsonb),

  ('feature.experiments', '00000000-0000-4000-8000-000000000020',
   'Experiment engine', 'Pre-registered A/B testing and verdicts (PRD 9).', 'boolean', 'false'::jsonb),

  ('feature.competitor_intel', '00000000-0000-4000-8000-000000000020',
   'Competitor intelligence',
   'Licensed ad-intelligence provider (PRD 11.4, founder decision D5).', 'boolean', 'false'::jsonb),

  ('feature.white_label_reports', '00000000-0000-4000-8000-000000000020',
   'White-label reports', 'Agency-branded report export (PRD 12.6).', 'boolean', 'false'::jsonb),

  ('feature.byok', '00000000-0000-4000-8000-000000000020',
   'Bring your own model key', 'Customer-supplied OpenRouter key (PRD D3).', 'boolean', 'false'::jsonb),

  ('feature.offline_conversions', '00000000-0000-4000-8000-000000000020',
   'Offline conversion upload',
   'CAPI upload of confirmed sales (PRD 12.2). This is the closed loop - the '
   'product core, on by default.',
   'boolean', 'true'::jsonb),

  ('feature.compliance_gate', '00000000-0000-4000-8000-000000000020',
   'Compliance pre-flight gate',
   'Meta policy plus Indian law (PRD 13). Defined as a feature so it is visible '
   'and auditable, NOT so it can be sold as an upsell. It defaults on and no '
   'plan in the catalogue turns it off.',
   'boolean', 'true'::jsonb)
on conflict (key) do update
  set name = excluded.name, description = excluded.description,
      value_type = excluded.value_type, default_json = excluded.default_json;

-- ---------------------------------------------------------------------------
-- Plans - the PRD 19.3 ladder, active, and the pre-ladder default kept for
-- the subscriptions already on it.
-- ---------------------------------------------------------------------------

insert into core.plans (id, product_id, key, name, description, price_inr, billing_period, trial_days, grace_days, is_active, sort_order)
values
  ('00000000-0000-4000-8000-000000000030', '00000000-0000-4000-8000-000000000020',
   'standard', 'Legacy default',
   'The pre-ladder default at Rs 15,000. Kept for existing subscriptions; new sign-ups choose a PRD 19.3 tier.',
   15000.00, 'monthly', 14, 7, true, 10),

  ('00000000-0000-4000-8000-000000000031', '00000000-0000-4000-8000-000000000020',
   'starter', 'Starter',
   'PRD 19.3. Up to Rs 1.5L/month ad spend. 1 ad account, L0-L1 autonomy, daily and weekly reports, compliance gate.',
   4999.00, 'monthly', 14, 7, true, 20),

  ('00000000-0000-4000-8000-000000000032', '00000000-0000-4000-8000-000000000020',
   'growth', 'Growth',
   'PRD 19.3. Rs 1.5L-6L/month ad spend. 2 ad accounts, up to L3 autonomy, experiments, full reporting, priority monitoring, industry intelligence.',
   12999.00, 'monthly', 14, 7, true, 30),

  ('00000000-0000-4000-8000-000000000033', '00000000-0000-4000-8000-000000000020',
   'scale', 'Scale',
   'PRD 19.3. Rs 6L+/month ad spend. 5 ad accounts, L4 available, competitor intelligence, custom guardrails, white-labelled reports.',
   29999.00, 'monthly', 14, 7, true, 40),

  ('00000000-0000-4000-8000-000000000034', '00000000-0000-4000-8000-000000000020',
   'agency', 'Agency',
   'PRD 19.3. Multi-client. Multi-workspace, per-client autonomy and caps, white-label, team roles, consolidated billing.',
   49999.00, 'monthly', 14, 7, true, 50)
on conflict (id) do update
  set key = excluded.key, name = excluded.name, description = excluded.description,
      price_inr = excluded.price_inr, billing_period = excluded.billing_period,
      trial_days = excluded.trial_days, grace_days = excluded.grace_days,
      is_active = excluded.is_active, sort_order = excluded.sort_order;

-- A new feature the PRD names for Growth and above and nothing had a key for:
-- whether tier-2 industry patterns are retrieved into this account's context.
insert into core.feature_definitions (key, product_id, name, description, value_type, default_json)
values (
  'feature.industry_intelligence',
  (select id from core.products where key = 'advit'),
  'Industry intelligence',
  'Whether anonymised, gated tier-2 industry patterns are retrieved into this '
  'account''s context (PRD 5.1, 19.3). Starter accounts learn from themselves; '
  'Growth and above also learn from the industry.',
  'boolean', 'false'::jsonb
)
on conflict (key) do update
  set name = excluded.name, description = excluded.description,
      value_type = excluded.value_type, default_json = excluded.default_json;

insert into core.plan_features (plan_id, feature_key, value_json)
values
  -- standard, INR 15,000
  ('00000000-0000-4000-8000-000000000030', 'max_workspaces',             '2'::jsonb),
  ('00000000-0000-4000-8000-000000000030', 'max_ad_accounts',            '3'::jsonb),
  ('00000000-0000-4000-8000-000000000030', 'max_seats',                  '5'::jsonb),
  ('00000000-0000-4000-8000-000000000030', 'max_autonomy_level',         '3'::jsonb),
  ('00000000-0000-4000-8000-000000000030', 'monthly_model_token_budget', '15000000'::jsonb),
  ('00000000-0000-4000-8000-000000000030', 'monitoring_cadence_minutes', '30'::jsonb),
  ('00000000-0000-4000-8000-000000000030', 'feature.experiments',        'true'::jsonb),
  ('00000000-0000-4000-8000-000000000030', 'feature.competitor_intel',   'false'::jsonb),
  ('00000000-0000-4000-8000-000000000030', 'feature.white_label_reports','false'::jsonb),
  ('00000000-0000-4000-8000-000000000030', 'feature.byok',               'true'::jsonb),
  ('00000000-0000-4000-8000-000000000030', 'feature.offline_conversions','true'::jsonb),
  ('00000000-0000-4000-8000-000000000030', 'feature.compliance_gate',    'true'::jsonb),
  -- The legacy plan has always retrieved industry patterns; nobody on it
  -- loses what they have today.
  ('00000000-0000-4000-8000-000000000030', 'feature.industry_intelligence','true'::jsonb)
on conflict (plan_id, feature_key) do update set value_json = excluded.value_json;

-- ---------------------------------------------------------------------------
-- The PRD 19.3 tiers' features, from the PRD's own table. Where the PRD is
-- silent - seats, workspace counts below Agency, token budgets - the value is
-- marked ASSUMED beside it, so the next person knows which numbers to argue
-- with.
-- ---------------------------------------------------------------------------


-- The features, per tier. Every value that is NOT from the PRD table is
-- marked ASSUMED in the comment beside it, so the next person knows which
-- numbers to argue with.
insert into core.plan_features (plan_id, feature_key, value_json)
select p.id, f.key, f.value
  from core.plans p
  join (values
    -- Starter, Rs 4,999. "1 ad account, L0-L1 autonomy, daily + weekly reports,
    -- compliance gate, platform-managed models with a token budget."
    ('starter', 'max_ad_accounts',             '1'::jsonb),
    ('starter', 'max_autonomy_level',          '1'::jsonb),
    ('starter', 'max_workspaces',              '1'::jsonb),        -- ASSUMED: only Agency is multi-workspace
    ('starter', 'max_seats',                   '2'::jsonb),        -- ASSUMED: PRD is silent on seats
    ('starter', 'monthly_model_token_budget',  '5000000'::jsonb),  -- ASSUMED: "a token budget"; 5M
    ('starter', 'monitoring_cadence_minutes',  '60'::jsonb),
    ('starter', 'feature.experiments',         'false'::jsonb),
    ('starter', 'feature.competitor_intel',    'false'::jsonb),
    ('starter', 'feature.white_label_reports', 'false'::jsonb),
    ('starter', 'feature.industry_intelligence','false'::jsonb),
    -- BYOK on every tier. The PRD puts it at Scale as an option; the owner
    -- later decided every organisation supplies its own OpenRouter key from
    -- day one, and that decision supersedes the table here.
    ('starter', 'feature.byok',                'true'::jsonb),
    ('starter', 'feature.offline_conversions', 'true'::jsonb),
    ('starter', 'feature.compliance_gate',     'true'::jsonb),

    -- Growth, Rs 12,999. "2 ad accounts, up to L3 autonomy, experiments, full
    -- reporting, priority monitoring cadence, industry intelligence."
    ('growth', 'max_ad_accounts',              '2'::jsonb),
    ('growth', 'max_autonomy_level',           '3'::jsonb),
    ('growth', 'max_workspaces',               '1'::jsonb),        -- ASSUMED
    ('growth', 'max_seats',                    '3'::jsonb),        -- ASSUMED
    ('growth', 'monthly_model_token_budget',   '15000000'::jsonb), -- ASSUMED
    ('growth', 'monitoring_cadence_minutes',   '30'::jsonb),       -- "priority monitoring cadence"
    ('growth', 'feature.experiments',          'true'::jsonb),
    ('growth', 'feature.competitor_intel',     'false'::jsonb),
    ('growth', 'feature.white_label_reports',  'false'::jsonb),
    ('growth', 'feature.industry_intelligence','true'::jsonb),
    ('growth', 'feature.byok',                 'true'::jsonb),
    ('growth', 'feature.offline_conversions',  'true'::jsonb),
    ('growth', 'feature.compliance_gate',      'true'::jsonb),

    -- Scale, Rs 29,999. "5 ad accounts, L4 available, competitor intelligence,
    -- custom guardrails, white-labelled reports, BYOK option."
    ('scale', 'max_ad_accounts',               '5'::jsonb),
    ('scale', 'max_autonomy_level',            '4'::jsonb),
    ('scale', 'max_workspaces',                '2'::jsonb),        -- ASSUMED
    ('scale', 'max_seats',                     '5'::jsonb),        -- ASSUMED
    ('scale', 'monthly_model_token_budget',    '40000000'::jsonb), -- ASSUMED
    ('scale', 'monitoring_cadence_minutes',    '15'::jsonb),       -- ASSUMED: one step past Growth
    ('scale', 'feature.experiments',           'true'::jsonb),
    ('scale', 'feature.competitor_intel',      'true'::jsonb),
    ('scale', 'feature.white_label_reports',   'true'::jsonb),
    ('scale', 'feature.industry_intelligence', 'true'::jsonb),
    ('scale', 'feature.byok',                  'true'::jsonb),
    ('scale', 'feature.offline_conversions',   'true'::jsonb),
    ('scale', 'feature.compliance_gate',       'true'::jsonb),

    -- Agency, from Rs 49,999. "Multi-workspace, per-client autonomy and caps,
    -- white-label, team roles, consolidated billing."
    ('agency', 'max_ad_accounts',              '15'::jsonb),       -- ASSUMED: "multi-client"; superadmin overrides per org
    ('agency', 'max_autonomy_level',           '4'::jsonb),
    ('agency', 'max_workspaces',               '10'::jsonb),       -- ASSUMED: a workspace per client
    ('agency', 'max_seats',                    '15'::jsonb),       -- ASSUMED: "team roles"
    ('agency', 'monthly_model_token_budget',   '100000000'::jsonb),-- ASSUMED
    ('agency', 'monitoring_cadence_minutes',   '15'::jsonb),
    ('agency', 'feature.experiments',          'true'::jsonb),
    ('agency', 'feature.competitor_intel',     'true'::jsonb),
    ('agency', 'feature.white_label_reports',  'true'::jsonb),
    ('agency', 'feature.industry_intelligence','true'::jsonb),
    ('agency', 'feature.byok',                 'true'::jsonb),
    ('agency', 'feature.offline_conversions',  'true'::jsonb),
    ('agency', 'feature.compliance_gate',      'true'::jsonb)
  ) as f(plan_key, key, value) on f.plan_key = p.key
 where p.product_id = (select id from core.products where key = 'advit')
on conflict (plan_id, feature_key) do update set value_json = excluded.value_json;
