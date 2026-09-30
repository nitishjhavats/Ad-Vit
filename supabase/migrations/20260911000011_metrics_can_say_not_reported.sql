-- =============================================================================
-- t_advit.metrics_daily could not say "not reported"
--
-- Every measured column was NOT NULL DEFAULT 0:
--
--   impressions, reach, spend_inr, clicks, link_clicks, results,
--   purchases, purchase_value_inr
--
-- So the table that holds the measurements is structurally incapable of
-- distinguishing "Meta reported zero" from "Meta did not report this", and a
-- writer that omits a field gets a confident zero it never claimed.
--
-- This repository has fixed the same defect in five other places and written
-- down why each time:
--
--   * 20260911000002 stopped compute_blended_daily coalescing an un-ingested
--     spend to zero, and added blended_daily.gaps to name what was missing;
--   * t_advit.workspaces.spend_basis_known exists ONLY because a sum over zero
--     rows is indistinguishable from a sum over rows that are zero;
--   * 20260911000001 stopped a NULL cancellation count disabling a CHECK;
--   * the Python economics engine returns None for an unknown rate rather than
--     0.0, and names the gap;
--   * 20260911000010, one migration ago, added `source` so a synthetic row
--     cannot present itself as measurement.
--
-- The table those five fixes all read from could not represent the distinction
-- at all. The gap only became reachable now, because until this week nothing
-- wrote to it - which is the same reason it was never noticed.
--
-- The consequence, concretely: Meta omits `results` while a conversion window
-- is still open. Stored as 0, that day reads as "the ads ran and nobody
-- converted" - a signal to pause an ad set that is in fact performing. Stored
-- as NULL, it reads as "not measured yet", which is the truth and is already
-- what every consumer downstream is written to handle.
--
-- `frequency`, `cost_per_result`, `ctr`, `cpm` and `cpc` were already nullable.
-- They are the DERIVED figures - the ones nobody would dream of defaulting to
-- zero, because a CPC of 0 is obviously wrong. The base measurements got the
-- treatment nobody questions precisely because a spend of 0 looks plausible.
-- =============================================================================

alter table t_advit.metrics_daily alter column impressions        drop not null;
alter table t_advit.metrics_daily alter column reach              drop not null;
alter table t_advit.metrics_daily alter column spend_inr          drop not null;
alter table t_advit.metrics_daily alter column clicks             drop not null;
alter table t_advit.metrics_daily alter column link_clicks        drop not null;
alter table t_advit.metrics_daily alter column results            drop not null;
alter table t_advit.metrics_daily alter column purchases          drop not null;
alter table t_advit.metrics_daily alter column purchase_value_inr drop not null;

-- The defaults go too, and that is the half that matters.
--
-- Leaving `default 0` while allowing NULL would mean a writer that omits the
-- column still gets a zero - the same wrong answer, now merely avoidable rather
-- than impossible. With no default, an omitted column is NULL and says so.
alter table t_advit.metrics_daily alter column impressions        drop default;
alter table t_advit.metrics_daily alter column reach              drop default;
alter table t_advit.metrics_daily alter column spend_inr          drop default;
alter table t_advit.metrics_daily alter column clicks             drop default;
alter table t_advit.metrics_daily alter column link_clicks        drop default;
alter table t_advit.metrics_daily alter column results            drop default;
alter table t_advit.metrics_daily alter column purchases          drop default;
alter table t_advit.metrics_daily alter column purchase_value_inr drop default;

-- Existing rows are NOT rewritten to NULL. A zero already stored was written by
-- the seed on purpose and is a claim about a measured day; turning it into
-- "unknown" would be inventing a gap, which is the mirror image of the defect
-- and just as dishonest.

comment on column t_advit.metrics_daily.spend_inr is
  'Spend as Meta reported it. NULL means NOT REPORTED, which is different from '
  'a measured zero - a distinction this column could not make until '
  '20260911000011, and which t_advit.workspaces.spend_basis_known exists '
  'because of.';

comment on column t_advit.metrics_daily.results is
  'Conversions in the account''s optimisation event. NULL while the attribution '
  'window is still open, which Meta signals by omitting the field. Stored as 0 '
  'it reads as "the ads ran and nobody converted" - a reason to pause an ad set '
  'that may be performing well.';


-- ---------------------------------------------------------------------------
-- A row that measures nothing is not a measurement
--
-- Now that every figure may be NULL, a row can exist carrying only its keys. It
-- would satisfy has_measured_spend, count towards "we have data", and contain
-- nothing. That is worse than no row, because an absent row is honest.
-- ---------------------------------------------------------------------------

alter table t_advit.metrics_daily
  drop constraint if exists metrics_daily_reports_something;

alter table t_advit.metrics_daily
  add constraint metrics_daily_reports_something check (
    impressions is not null
    or spend_inr is not null
    or clicks is not null
    or results is not null
    or purchases is not null
  );

comment on constraint metrics_daily_reports_something on t_advit.metrics_daily is
  'A row must report at least one measured figure. Without it a sync could '
  'write keys with nothing attached, and "we have data for that day" would be '
  'true of a row containing none.';
