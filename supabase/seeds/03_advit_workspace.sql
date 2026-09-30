-- =============================================================================
-- ad-vit - workspace seed
--
-- Mirrors the real Meta accounts discovered on this Meta Ads MCP connection so
-- the spine is exercised against true data shapes. Connections are seeded
-- read-only: write_enabled is false everywhere except the unfunded account, so
-- the default posture cannot spend money even if a driver is misconfigured.
-- =============================================================================

-- industry_key replaces business_type, and industry_pack_id is gone: the pack
-- identity is derived from t_advit.industries.pack_id. The value stored here
-- by hand had already drifted - this workspace's neighbour carried 'general@1'
-- while its business_type said 'general_d2c'.
insert into t_advit.workspaces (
  id, org_id, name, industry_key,
  autonomy_level, daily_cap_inr, monthly_cap_inr, cac_ceiling_inr, timezone
)
values
  (
    '00000000-0000-4000-8000-000000000050',
    '00000000-0000-4000-8000-000000000010',
    'Demo Brand',
    'ayurveda',
    -- PRD D4: new workspaces start at L1 and climb only on outcome data.
    1,
    5000.00,
    150000.00,
    900.00,
    'Asia/Kolkata'
  ),
  (
    '00000000-0000-4000-8000-000000000051',
    '00000000-0000-4000-8000-000000000011',
    'Rival Workspace',
    'general_d2c',
    1,
    2000.00,
    50000.00,
    null,
    'Asia/Kolkata'
  )
on conflict (id) do nothing;

insert into t_advit.workspace_members (workspace_id, user_id, role, approval_limit_inr)
values
  ('00000000-0000-4000-8000-000000000050', '00000000-0000-4000-8000-000000000003',
   'media_buyer', 5000.00),
  ('00000000-0000-4000-8000-000000000051', '00000000-0000-4000-8000-000000000004',
   'marketing_manager', null)
on conflict do nothing;

-- ---------------------------------------------------------------------------
-- Meta connections
--
-- Account ids are the real ones this connection reports. Only 1000000000000003
-- carries write_enabled, and only because it has NO payment method attached -
-- so even an accidental activation cannot spend. The two funded Demo Brand
-- accounts stay read-only until explicitly widened.
-- ---------------------------------------------------------------------------

-- last_checked_at is set because this workspace is driven through activation by
-- the integration suite, and activation is only reachable after onboarding's
-- connection audit has run (PRD 6.1 step 4). A connection that has never been
-- checked has no established spend basis, and the guardrail layer refuses to
-- commit spend against caps it cannot evaluate - correctly.
insert into t_advit.meta_connections (
  workspace_id, business_id, ad_account_id, health, currency, write_enabled,
  health_detail, last_checked_at
)
values
  (
    '00000000-0000-4000-8000-000000000050',
    '2000000000000001', '1000000000000001',
    'unknown', 'INR', false,
    jsonb_build_object(
      'account_name', 'Demo Brand call ads',
      'has_payment_method', true,
      'note', 'Funded. Read-only until the write path is proven end to end.'
    ),
    now() - interval '1 hour'
  ),
  (
    '00000000-0000-4000-8000-000000000050',
    '2000000000000001', '1000000000000002',
    'unknown', 'INR', false,
    jsonb_build_object(
      'account_name', 'PILES CARE 2',
      'has_payment_method', true,
      'note', 'Funded. Read-only until the write path is proven end to end.'
    ),
    now() - interval '1 hour'
  ),
  (
    '00000000-0000-4000-8000-000000000050',
    null, '1000000000000003',
    'unknown', 'INR', true,
    jsonb_build_object(
      'account_name', 'Aman Jha',
      'has_payment_method', false,
      'note', 'No payment method. Designated write target: activation cannot spend.'
    ),
    now() - interval '1 hour'
  )
on conflict (workspace_id, ad_account_id) do nothing;

-- ---------------------------------------------------------------------------
-- Account context (T1) and catalogue
--
-- Seeded as owner-asserted at low confidence. The OS tests these rather than
-- trusting them (PRD 6.2) - they are starting hypotheses, not facts.
-- ---------------------------------------------------------------------------

insert into t_advit.account_context
  (workspace_id, dimension, key, value_json, confidence, source)
values
  ('00000000-0000-4000-8000-000000000050', 'compliance', 'category_sensitivity',
   to_jsonb('high'::text), 0.900, 'industry_pack'),

  ('00000000-0000-4000-8000-000000000050', 'compliance', 'sensitivity_rationale',
   to_jsonb(
     'Buyers will not say this problem aloud on a phone call. PRD 11.5: for sensitive '
     'categories WhatsApp ranks first, instant form second, click-to-call last - and '
     'this outranks a cheaper CPL.'::text
   ), 0.900, 'industry_pack'),

  ('00000000-0000-4000-8000-000000000050', 'compliance', 'schedule_j_exposure',
   to_jsonb(
     'The product category may fall under DMR Act Schedule J. Every creative must clear '
     'the stage-2 gate, and the Schedule J term list requires legal verification before '
     'commercial reliance.'::text
   ), 0.700, 'industry_pack'),

  ('00000000-0000-4000-8000-000000000050', 'sales_operation', 'primary_cta',
   to_jsonb('call'::text), 0.500, 'owner_asserted'),

  ('00000000-0000-4000-8000-000000000050', 'sales_operation', 'cta_observed_from',
   to_jsonb('Ad account named "Demo Brand call ads" implies click-to-call is in use.'::text),
   0.400, 'inferred')
on conflict do nothing;

insert into t_advit.catalog_products
  (workspace_id, sku, name, mrp_inr, price_inr, cogs_inr, margin_rate, classification)
values
  ('00000000-0000-4000-8000-000000000050', 'PC-001', 'Demo Brand (placeholder)',
   1999.00, 1299.00, 494.00, 0.6200, 'unverified')
on conflict (workspace_id, sku) do nothing;

comment on table t_advit.catalog_products is
  'Seeded values are placeholders. The CAC ceiling is derived from real margin and RTO '
  '(PRD 12.4, FR-039), so these must be replaced with the owner''s actual unit economics '
  'before any scaling proposal is trustworthy.';

-- =============================================================================
-- Ingested spend, so the seeded account is one that has actually been synced
--
-- t_advit.metrics_daily had no rows and no writer, which meant the seed
-- described a workspace that has never been read from Meta. No real workspace
-- is ever in that state for long, and the fixture being in it hid a defect:
-- the monthly cap summed this table, coalesced the empty result to zero, and
-- therefore passed every check it was ever given.
--
-- The daily cap is unaffected by these rows - it is computed from ad_sets and
-- from this system's own t_advit.actions record, not from ingested metrics -
-- so the sequential-activation tests still exercise what they were written for.
--
-- Dates are generated in the workspace's own timezone rather than the server's,
-- because the month boundary is the workspace's. At most five days, and only
-- days that have actually happened, so seeding on the 1st does not invent a
-- future.
-- =============================================================================

insert into t_advit.metrics_daily (
  date, workspace_id, level, entity_id, ad_account_id, source,
  spend_inr, impressions, reach, clicks, link_clicks, results, cost_per_result
)
select
  d::date,
  '00000000-0000-4000-8000-000000000050',
  'account',
  '1000000000000001',
  -- Equal to entity_id, and a CHECK now says so: an account-level row whose
  -- entity_id and ad_account_id disagree describes two accounts at once.
  '1000000000000001',
  -- 'fixture', not 'meta'. These numbers were never read from Meta, and the
  -- whole point of the column is that a row this system invented cannot
  -- present itself as measurement - not even to the seed's own tests.
  'fixture',
  1200.00, 42000, 31000, 860, 640, 22, 54.5455
from generate_series(
       greatest(
         date_trunc('month', (now() at time zone 'Asia/Kolkata')::date)::date,
         (now() at time zone 'Asia/Kolkata')::date - 4
       ),
       (now() at time zone 'Asia/Kolkata')::date,
       interval '1 day'
     ) d
on conflict (workspace_id, date, level, entity_id) do nothing;
