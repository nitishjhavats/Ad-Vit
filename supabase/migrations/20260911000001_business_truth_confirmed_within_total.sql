-- =============================================================================
-- Fix: an unreported cancellation count disabled the confirmed-vs-total check
--
-- The constraint read:
--
--   check (total_orders is null or confirmed_orders is null
--          or cancelled_orders is null
--          or (confirmed_orders + cancelled_orders) <= total_orders)
--
-- so a NULL in `cancelled_orders` switched off a comparison that does not need
-- cancelled_orders at all. Confirming more orders than were placed is
-- impossible, and catching it is the whole point of the constraint - but it
-- only fired when the owner happened to report all three numbers.
--
-- Reproduced against the running database:
--
--   insert ... (total_orders, confirmed_orders) values (10, 100);   -- ACCEPTED
--   select t_advit.compute_blended_daily(ws, that_date);
--   -- ERROR: numeric field overflow
--
-- Two failures, and the second is the worse one. blended_daily.confirm_rate is
-- numeric(6,5), which holds at most 9.99999; a confirm rate of 10.0 overflows
-- it. So a typo at 20:30 did not produce a wrong number - it produced a
-- scheduled job that dies, every day, on a row nothing will clean up, taking
-- the whole day's economics with it.
--
-- The fix is to guard the check on the columns the comparison actually uses and
-- treat an unreported cancellation as zero for this purpose. That is the honest
-- reading: cancellations not being reported yet does not make "confirmed
-- exceeds total" acceptable.
--
-- Deliberately NOT added as NOT VALID. If a row already violates this, the
-- migration should stop and make someone look, because such a row is already
-- breaking compute_blended_daily every time it runs.
-- =============================================================================

alter table t_advit.business_truth
  drop constraint if exists business_truth_confirmed_within_total;

alter table t_advit.business_truth
  add constraint business_truth_confirmed_within_total check (
    total_orders is null
    or confirmed_orders is null
    -- coalesce, not `cancelled_orders is null or ...`: a missing cancellation
    -- count must not excuse an impossible confirmation count.
    or (confirmed_orders + coalesce(cancelled_orders, 0)) <= total_orders
  );

comment on constraint business_truth_confirmed_within_total on t_advit.business_truth is
  'Confirmed plus cancelled cannot exceed the orders placed. Guarded only on the '
  'columns the comparison uses, so an unreported cancellation count does not '
  'silently disable it - which is how a confirm rate of 10.0 reached '
  'blended_daily.confirm_rate numeric(6,5) and overflowed it.';


-- ---------------------------------------------------------------------------
-- The same shape, one table over.
--
-- delivered_within_confirmed guards on `confirmed_orders is null`, which is
-- correct - the comparison genuinely needs it. But rto_orders is compared
-- against confirmed elsewhere (compute_blended_daily divides by it) with no
-- constraint at all, so an RTO count larger than the confirmed count is
-- storable and produces an rto_rate above 1, which then feeds the contribution
-- margin. Same class, caught while here.
-- ---------------------------------------------------------------------------

alter table t_advit.business_truth
  drop constraint if exists business_truth_rto_within_confirmed;

alter table t_advit.business_truth
  add constraint business_truth_rto_within_confirmed check (
    confirmed_orders is null
    or rto_orders is null
    or rto_orders <= confirmed_orders
  );

comment on constraint business_truth_rto_within_confirmed on t_advit.business_truth is
  'Returns cannot exceed the orders that were confirmed. Without this an RTO '
  'rate above 1 reaches contribution_margin_per_delivered_order and reports a '
  'margin that never existed.';
