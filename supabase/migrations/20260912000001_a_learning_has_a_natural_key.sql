-- =============================================================================
-- t_advit.learnings gets the key that makes it upsertable
--
-- Nothing has ever written this table, so it has no natural key - and the first
-- writer would have accumulated a fresh row per tick instead of sharpening one
-- claim as evidence arrived. A learning that exists eleven times with eleven
-- different confidences is not eleven learnings; it is one, recorded badly.
--
-- The claim an account-tier learning makes is a triple: for THIS kind of
-- decision, THIS metric moves THIS way. The statement text is not part of the
-- identity - it is rendered from the evidence and changes every time the
-- evidence does, which is exactly why it cannot be the key.
--
-- Partial on `status <> 'historical'` so superseding works: a claim that stops
-- holding is marked historical and keeps its row (the evidence it was built on
-- is the audit trail for a decision somebody took), while a new active claim
-- about the same triple can be written beside it.
-- =============================================================================

create unique index learnings_account_claim_unique
  on t_advit.learnings (
    workspace_id,
    (conditions_json ->> 'decision_type'),
    (conditions_json ->> 'metric'),
    (conditions_json ->> 'direction')
  )
  where tier = 'account' and status <> 'historical';

comment on index t_advit.learnings_account_claim_unique is
  'One live account-tier claim per (decision type, metric, predicted direction). '
  'The statement text is rendered from the evidence and is deliberately not part '
  'of the key.';


-- The shared tiers are keyed differently on purpose. An industry or global
-- learning has no workspace_id (learnings_tier_scoping enforces that), so the
-- triple above would collapse every workspace's claims into one row. Those
-- tiers are reached by promotion from t_advit.industry_patterns, which has its
-- own independence gate - so they are keyed on the claim alone.
create unique index learnings_shared_claim_unique
  on t_advit.learnings (
    tier,
    (conditions_json ->> 'decision_type'),
    (conditions_json ->> 'metric'),
    (conditions_json ->> 'direction')
  )
  where tier <> 'account' and status <> 'historical';
