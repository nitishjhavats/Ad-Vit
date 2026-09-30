-- =============================================================================
-- Money moves the subscription
--
-- 20260916000001 gave the platform an invoice and said, of the money,
-- "collecting it is a payment gateway's job, not this table's". That was the
-- invoice table's position and it was right about the invoice. It is not the
-- platform's position, because there is no gateway. Money arrives by UPI or
-- NEFT against the invoice number, and the only thing that confirms it is an
-- operator finding the reference the customer typed on the bank statement.
-- So the record of that matching is a row - core.payments - and a subscription
-- moves out of pending_payment in exactly two ways: through an approved row
-- here, or through the settle walk below when no row arrives in time.
--
-- The four acts, and who performs each:
--
--   request  An owner or admin asks to pay an issued invoice. A row opens with
--            the invoice's total, the bank details the runtime holds, and a
--            window (PAYMENT_WINDOW_HOURS). One open row per invoice.
--   submit   The same owner or admin, inside the window, types the UTR or the
--            UPI reference and the method. Nothing is confirmed by this.
--   review   An operator matches the reference to the bank statement and says
--            approved or rejected. Approved: the invoice is paid and the
--            subscription is active again - whatever it had sunk to. Rejected:
--            the row closes with the operator's reason and the organisation
--            may request again.
--   settle   The clock. Every night, before invoices are raised, the walk
--            moves each subscription one step along the state machine the
--            enum comments in 20260903000001 drew - trial ended, period ended,
--            invoice overdue, grace exhausted - and closes payment windows
--            nobody used. Nothing else walks that machine today.
--
-- `authenticated` holds SELECT on core.payments and nothing else. Every write
-- is a SECURITY DEFINER function with its own guard inside: the tenant
-- functions check that auth.uid() is an owner or admin of the invoice's
-- organisation; review checks core.is_superadmin(); settle is granted to the
-- backend alone. A tenant cannot type a row that says "approved", and an
-- operator cannot approve a payment nobody submitted.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- 1. The row
-- ---------------------------------------------------------------------------
create table core.payments (
  id               uuid primary key default gen_random_uuid(),
  org_id           uuid not null references core.organisations(id) on delete restrict,
  -- RESTRICT rather than CASCADE: an invoice with a payment against it is an
  -- invoice somebody has acted on, and deleting it would delete the evidence
  -- of the act.
  invoice_id       uuid not null references core.invoices(id) on delete restrict,
  subscription_id  uuid not null references core.subscriptions(id) on delete restrict,
  -- The invoice total at the moment of the request. The invoice cannot change
  -- after issue, so this is a copy for the reader's convenience rather than a
  -- snapshot against drift - but it is also what the operator matches on the
  -- statement, and that number belongs on the row they are reviewing.
  amount_inr       numeric(12,2) not null,
  -- core.payment_status has carried 'draft' since the first schema. Nothing
  -- writes it: a payment that has not been requested is not a row.
  status           core.payment_status not null,
  method           text,
  reference        text,
  -- The date the customer says the money left their account. Theirs to
  -- state, the operator's to check; not the date of the submission.
  paid_on          date,
  requested_by     uuid references core.platform_users(id) on delete set null,
  requested_at     timestamptz not null default now(),
  window_ends_at   timestamptz,
  submitted_by     uuid references core.platform_users(id) on delete set null,
  submitted_at     timestamptz,
  reviewed_by      uuid references core.platform_users(id) on delete set null,
  reviewed_at      timestamptz,
  review_note      text,
  created_at       timestamptz not null default now(),

  constraint payments_never_draft check (status <> 'draft'),
  constraint payments_amount_not_negative check (amount_inr >= 0),
  constraint payments_method_known check (method is null or method in ('upi', 'bank_transfer')),
  -- A UTR is 12 to 22 characters; a UPI reference 12. Four to sixty-four is
  -- wide enough for every bank's format and narrow enough that "paid" is not
  -- a reference.
  constraint payments_reference_shape check (
    reference is null or length(reference) between 4 and 64
  ),
  -- What each state must carry, so a row cannot say "submitted" without a
  -- reference to match, "approved" without a verdict time, or "awaiting"
  -- without a deadline. The states are cumulative: an approved row was
  -- submitted, so it carries the submission too.
  constraint payments_awaiting_carry_window check (
    status <> 'awaiting_payment' or window_ends_at is not null
  ),
  constraint payments_submitted_carry_reference check (
    status not in ('submitted', 'approved', 'rejected')
    or (method is not null and reference is not null and submitted_at is not null)
  ),
  constraint payments_reviewed_carry_verdict check (
    status not in ('approved', 'rejected') or reviewed_at is not null
  )
);

-- One open request per invoice. A second request while one is awaiting
-- returns the first; while one is submitted it is refused - and this index is
-- what makes that true under two concurrent requests rather than only under
-- the function's own reading of the table.
create unique index payments_one_open_per_invoice
  on core.payments (invoice_id)
  where status in ('awaiting_payment', 'submitted');

create index payments_org_created_idx on core.payments (org_id, created_at desc);
create index payments_submitted_idx   on core.payments (submitted_at desc) where status = 'submitted';
-- The settle walk asks, per subscription, whether a reference is under
-- review; without this the question is a scan of the ledger every night.
create index payments_under_review_idx on core.payments (subscription_id) where status = 'submitted';

-- ---------------------------------------------------------------------------
-- Which calendar day a period boundary falls on.
--
-- core.invoices.period_start is a DATE; core.subscriptions.current_period_start
-- is a timestamptz that the settle walk below sets to the trial's end or the
-- previous period's end - an instant with a time of day, because onboarding
-- stamped now(). Comparing the two directly casts the instant in the SESSION
-- time zone (UTC on the service pool), so a boundary at 02:00 IST lands on
-- the previous day, and a boundary that is not midnight never equals the
-- date raise_invoices wrote - which would have made raise_invoices re-select
-- an already-invoiced subscription the next night and abort on
-- invoices_one_per_period. The product's day is the IST day (metrics_daily,
-- the jobs' clocks); this function says so once, and every place that turns
-- a boundary into a date calls it.
-- ---------------------------------------------------------------------------
create or replace function core.period_date(p_at timestamptz)
returns date
language sql
immutable
parallel safe
as $fn$
  select (p_at at time zone 'Asia/Kolkata')::date;
$fn$;

comment on function core.period_date(timestamptz) is
  'The IST calendar date of a period boundary. The one definition invoices and '
  'the settle walk share, so a non-midnight boundary is the same day to both.';

revoke all on function core.period_date(timestamptz) from public;
grant execute on function core.period_date(timestamptz) to authenticated, advit_backend;

comment on table core.payments is
  'One row per request to pay an invoice. There is no gateway: the customer '
  'pays by UPI or NEFT against the invoice number and types the reference, and '
  'an operator matching it to the bank statement is the confirmation. Written '
  'only through core.request_payment, core.submit_payment, core.review_payment '
  'and the settle walk; a tenant holds SELECT and nothing else.';

comment on column core.payments.window_ends_at is
  'When the request lapses unused. After this a submit is refused and the row '
  'is moved to expired; the organisation requests again and gets a fresh window.';


-- -- RLS -------------------------------------------------------------------
alter table core.payments enable row level security;

create policy advit_backend_all on core.payments
  for all to advit_backend using (true) with check (true);

-- Owners and admins see their organisation's rows; the operator sees every
-- row. Nobody with a session inserts or updates - there is no policy for it
-- and no grant, and the functions below run as their owner.
create policy payments_read on core.payments
  for select to authenticated
  using (
    core.has_org_role(org_id, array['owner', 'admin']::core.org_role[])
    or core.is_superadmin()
  );

grant select on core.payments to authenticated;
grant select, insert, update on core.payments to advit_backend;


-- ---------------------------------------------------------------------------
-- 2. Who may act on an invoice's money.
--
-- The one question three of the functions ask. Written once so they cannot
-- answer it three ways. Two refusals, deliberately different in shape:
--
--   not_allowed    The caller belongs to the organisation but is not an owner
--                  or admin. They may be told so - a member asking to pay the
--                  company's bill is refused, not hidden from.
--   payment_unknown / invoice_unknown
--                  The caller is not a member at all, or the thing does not
--                  exist. Both are "not found", because a stranger probing an
--                  id must learn nothing from the shape of the refusal.
-- ---------------------------------------------------------------------------
create or replace function core.assert_billing_admin_of(p_org uuid, p_hint_when_stranger text)
returns void
language plpgsql
stable
security definer
set search_path = core, pg_catalog
as $fn$
declare
  v_caller uuid := auth.uid();
begin
  if v_caller is null then
    -- These are a person's acts. A connection with no subject is not a person
    -- and is refused rather than waved through as the backend: the settle walk
    -- has its own function and its own grant.
    raise exception 'not found' using errcode = '42501', hint = p_hint_when_stranger;
  end if;
  if not core.is_org_member(p_org, v_caller) and not core.is_superadmin(v_caller) then
    raise exception 'not found' using errcode = '42501', hint = p_hint_when_stranger;
  end if;
  if not core.has_org_role(p_org, array['owner', 'admin']::core.org_role[], v_caller) then
    raise exception 'only an owner or admin may act on organisation %''s payments', p_org
      using errcode = '42501', hint = 'not_allowed';
  end if;
end;
$fn$;

revoke execute on function core.assert_billing_admin_of(uuid, text) from public;
-- Called only from the definer bodies below, which run as the owner. No grant.


-- ---------------------------------------------------------------------------
-- 3. Request
-- ---------------------------------------------------------------------------
create or replace function core.request_payment(p_invoice uuid, p_window_hours integer)
returns core.payments
language plpgsql
volatile
security definer
set search_path = core, pg_catalog
as $fn$
declare
  v_inv  core.invoices;
  v_open core.payments;
  v_row  core.payments;
begin
  select * into v_inv from core.invoices i where i.id = p_invoice for update;
  if not found then
    raise exception 'not found' using errcode = '42501', hint = 'invoice_unknown';
  end if;
  perform core.assert_billing_admin_of(v_inv.org_id, 'invoice_unknown');

  -- A window of nothing is not a window. The runtime reads PAYMENT_WINDOW_HOURS
  -- and passes it; a misconfigured zero must refuse here rather than open a
  -- request that has lapsed before it returns.
  if p_window_hours is null or p_window_hours <= 0 then
    raise exception 'a payment window must be a positive number of hours, not %', p_window_hours
      using errcode = '23514', hint = 'window_invalid';
  end if;

  -- Only an ISSUED invoice is payable. A draft has no number to quote and no
  -- tax decided; a paid one is paid; a void one was withdrawn.
  if v_inv.status <> 'issued' then
    raise exception 'invoice % is % and cannot be paid', coalesce(v_inv.number, p_invoice::text), v_inv.status
      using errcode = '23514', hint = 'invoice_not_payable';
  end if;

  select * into v_open from core.payments p
   where p.invoice_id = p_invoice and p.status in ('awaiting_payment', 'submitted')
   for update;
  if found then
    if v_open.status = 'submitted' then
      raise exception 'a payment reference for invoice % is already awaiting review',
        coalesce(v_inv.number, p_invoice::text)
        using errcode = '23505', hint = 'payment_open';
    end if;
    if v_open.window_ends_at > now() then
      -- Idempotent: the same window, the same bank details, the same row. An
      -- owner who reloads the page is not asking for a second deadline.
      return v_open;
    end if;
    -- The window lapsed and nobody swept it yet. Close it here so the unique
    -- index lets a fresh one open; the settle walk would have done the same.
    perform core.expire_payment(v_open.id);
  end if;

  begin
    insert into core.payments
      (org_id, invoice_id, subscription_id, amount_inr, status,
       requested_by, requested_at, window_ends_at)
    values
      (v_inv.org_id, v_inv.id, v_inv.subscription_id, v_inv.total_inr, 'awaiting_payment',
       auth.uid(), now(), now() + make_interval(hours => p_window_hours))
    returning * into v_row;
  exception when unique_violation then
    -- Two owners pressed Pay in the same instant: both read no open row,
    -- one INSERT won. The other gets the same answer a reload would - the
    -- open row - rather than a duplicate-key error with no hint.
    select * into v_open from core.payments p
     where p.invoice_id = p_invoice and p.status in ('awaiting_payment', 'submitted');
    if v_open.status = 'submitted' then
      raise exception 'a payment reference for invoice % is already awaiting review',
        coalesce(v_inv.number, p_invoice::text)
        using errcode = '23505', hint = 'payment_open';
    end if;
    return v_open;
  end;

  perform core.log_audit(
    'organisation'::core.audit_scope, 'payment.requested',
    p_org        => v_inv.org_id,
    p_actor_type => 'user'::core.actor_type,
    p_actor      => auth.uid(),
    p_payload    => jsonb_build_object(
      'payment_id',     v_row.id,
      'invoice_id',     v_inv.id,
      'invoice_number', v_inv.number,
      'amount_inr',     v_row.amount_inr,
      'window_ends_at', v_row.window_ends_at
    )
  );
  return v_row;
end;
$fn$;

revoke execute on function core.request_payment(uuid, integer) from public;
grant  execute on function core.request_payment(uuid, integer) to authenticated;

comment on function core.request_payment(uuid, integer) is
  'Open a payment request for an issued invoice: owner or admin of its '
  'organisation, checked inside. Returns the existing request while its window '
  'is open; refuses (payment_open) while a reference is under review.';


-- ---------------------------------------------------------------------------
-- 4. Expire
--
-- A request whose window has lapsed becomes 'expired'. Two callers: the
-- settle walk, nightly, for every lapsed row; and the runtime, when a submit
-- is refused with window_elapsed. The refusal is an exception, and an
-- exception rolls back everything the function wrote - so submit_payment
-- cannot both refuse and close the row in one call. The caller refuses, then
-- calls this in a fresh transaction. Idempotent and consequence-free: a row
-- that is not awaiting, or whose window is still open, is returned untouched.
-- ---------------------------------------------------------------------------
create or replace function core.expire_payment(p_payment uuid, p_now timestamptz default now())
returns core.payments
language plpgsql
volatile
security definer
set search_path = core, pg_catalog
as $fn$
declare
  v_row     core.payments;
  v_caller  uuid := auth.uid();
  -- The same test core.may_ask_about uses: no subject AND no PostgREST role is
  -- the backend. A subjectless `authenticated` is anonymous and is checked
  -- like any session - which refuses it, because it is nobody's owner.
  v_backend boolean := v_caller is null
                       and coalesce(current_setting('role', true), 'none') not in ('anon', 'authenticated');
  -- p_now is the settle walk's clock and is honoured for the backend only. A
  -- session gets the real clock whatever it passes: an owner may not decide
  -- that their own window has already closed, or that it has not.
  v_now     timestamptz := case when v_backend then coalesce(p_now, now()) else now() end;
begin
  select * into v_row from core.payments p where p.id = p_payment for update;
  if not found then
    raise exception 'not found' using errcode = '42501', hint = 'payment_unknown';
  end if;
  if not v_backend then
    perform core.assert_billing_admin_of(v_row.org_id, 'payment_unknown');
  end if;

  if v_row.status <> 'awaiting_payment' or v_row.window_ends_at > v_now then
    return v_row;
  end if;

  update core.payments set status = 'expired' where id = v_row.id returning * into v_row;

  perform core.log_audit(
    'organisation'::core.audit_scope, 'payment.expired',
    p_org        => v_row.org_id,
    p_actor_type => (case when v_caller is null then 'system' else 'user' end)::core.actor_type,
    p_actor      => v_caller,
    p_payload    => jsonb_build_object(
      'payment_id', v_row.id, 'invoice_id', v_row.invoice_id, 'window_ends_at', v_row.window_ends_at
    )
  );
  return v_row;
end;
$fn$;

revoke execute on function core.expire_payment(uuid, timestamptz) from public;
grant  execute on function core.expire_payment(uuid, timestamptz) to authenticated, advit_backend;


-- ---------------------------------------------------------------------------
-- 5. Submit
-- ---------------------------------------------------------------------------
create or replace function core.submit_payment(
  p_payment   uuid,
  p_method    text,
  p_reference text,
  p_paid_on   date default null
)
returns core.payments
language plpgsql
volatile
security definer
set search_path = core, pg_catalog
as $fn$
declare
  v_row core.payments;
  v_ref text := nullif(btrim(coalesce(p_reference, '')), '');
begin
  select * into v_row from core.payments p where p.id = p_payment for update;
  if not found then
    raise exception 'not found' using errcode = '42501', hint = 'payment_unknown';
  end if;
  perform core.assert_billing_admin_of(v_row.org_id, 'payment_unknown');

  if p_method is null or p_method not in ('upi', 'bank_transfer') then
    raise exception 'method must be upi or bank_transfer, not %', p_method
      using errcode = '23514', hint = 'method_unknown';
  end if;
  if v_ref is null or length(v_ref) < 4 or length(v_ref) > 64 then
    raise exception 'a UTR or UPI reference is 4 to 64 characters'
      using errcode = '23514', hint = 'reference_invalid';
  end if;
  -- The customer's own clock, as everywhere a date is checked (guard_reverify).
  if p_paid_on is not null and p_paid_on > (now() at time zone 'Asia/Kolkata')::date then
    raise exception 'paid_on % is in the future', p_paid_on
      using errcode = '23514', hint = 'paid_on_future';
  end if;

  if v_row.status <> 'awaiting_payment' then
    raise exception 'payment % is % and is not awaiting a reference', p_payment, v_row.status
      using errcode = '23514', hint = 'payment_not_awaiting';
  end if;
  if v_row.window_ends_at <= now() then
    -- Refused, and the caller closes the row (core.expire_payment) in a
    -- transaction this exception does not roll back. The money, if it was
    -- sent, is still in the bank; the organisation requests again and quotes
    -- the same reference against the fresh request.
    raise exception 'the payment window for % closed at %', p_payment, v_row.window_ends_at
      using errcode = '23514', hint = 'window_elapsed';
  end if;

  update core.payments
     set status       = 'submitted',
         method       = p_method,
         reference    = v_ref,
         paid_on      = p_paid_on,
         submitted_by = auth.uid(),
         submitted_at = now()
   where id = v_row.id
   returning * into v_row;

  perform core.log_audit(
    'organisation'::core.audit_scope, 'payment.submitted',
    p_org        => v_row.org_id,
    p_actor_type => 'user'::core.actor_type,
    p_actor      => auth.uid(),
    p_payload    => jsonb_build_object(
      'payment_id', v_row.id, 'invoice_id', v_row.invoice_id,
      'method', v_row.method, 'reference', v_row.reference, 'paid_on', v_row.paid_on,
      'amount_inr', v_row.amount_inr
    )
  );
  return v_row;
end;
$fn$;

revoke execute on function core.submit_payment(uuid, text, text, date) from public;
grant  execute on function core.submit_payment(uuid, text, text, date) to authenticated;

comment on function core.submit_payment(uuid, text, text, date) is
  'The customer states how they paid and quotes the reference, inside the '
  'window. Owner or admin, checked inside. Confirms nothing: the row waits for '
  'an operator.';


-- ---------------------------------------------------------------------------
-- 6. Review
--
-- Superadmin only, checked inside and 404-shaped like set_organisation_status.
-- Approval is the one act in this migration that moves a subscription forward
-- on a person's say-so: the invoice is paid and the subscription is active
-- again wherever it had sunk to - pending_payment, past_due, grace, expired.
-- Money restores access; that is what the customer paid for. Two states are
-- left alone on purpose: 'active' is already there, and 'suspended' is an
-- operator's verdict on the organisation that a payment does not overturn -
-- core.set_organisation_status is the door for that. 'trialing' is not
-- invoiced (raise_invoices skips it) so no payment can name it.
-- ---------------------------------------------------------------------------
create or replace function core.review_payment(
  p_payment uuid,
  p_verdict core.payment_status,
  p_note    text default null
)
returns core.payments
language plpgsql
volatile
security definer
set search_path = core, pg_catalog
as $fn$
declare
  v_row        core.payments;
  v_note       text := nullif(btrim(coalesce(p_note, '')), '');
  v_sub_before core.subscription_status;
  v_sub_after  core.subscription_status;
begin
  if not core.is_superadmin() then
    raise exception 'not found' using errcode = '42501', hint = 'not_superadmin';
  end if;
  if p_verdict not in ('approved', 'rejected') then
    raise exception 'a review is approved or rejected, not %', p_verdict
      using errcode = '23514', hint = 'verdict_invalid';
  end if;
  if p_verdict = 'rejected' and v_note is null then
    -- The customer will read this. "Rejected" with nothing after it is a
    -- customer who does not know whether to pay again or call.
    raise exception 'a rejection needs a reason the customer can act on'
      using errcode = '23514', hint = 'reason_required';
  end if;

  select * into v_row from core.payments p where p.id = p_payment for update;
  if not found then
    raise exception 'not found' using errcode = '42501', hint = 'payment_unknown';
  end if;
  if v_row.status <> 'submitted' then
    raise exception 'payment % is % and is not awaiting review', p_payment, v_row.status
      using errcode = '23514', hint = 'payment_not_submitted';
  end if;

  update core.payments
     set status      = p_verdict,
         reviewed_by = auth.uid(),
         reviewed_at = now(),
         review_note = v_note
   where id = v_row.id
   returning * into v_row;

  if p_verdict = 'approved' then
    update core.invoices
       set status = 'paid', paid_at = now()
     where id = v_row.invoice_id and status = 'issued';
    if not found then
      -- The invoice moved under the payment - voided by a migration, or paid by
      -- another row. Approving money against it would say two things at once.
      raise exception 'invoice % is no longer payable', v_row.invoice_id
        using errcode = '23514', hint = 'invoice_not_payable';
    end if;

    select s.status into v_sub_before from core.subscriptions s where s.id = v_row.subscription_id for update;
    -- Money restores access, and the period is left where it was. An expired
    -- subscription whose period has already ended becomes active on a stale
    -- period; the next settle walk rolls it forward from the old end and
    -- raise_invoices bills the new one. The customer paid for the period
    -- they were invoiced, and the time they were denied inside it was the
    -- consequence of not paying - re-anchoring the period to the payment
    -- date would instead invoice the new period tonight, on top of the one
    -- just paid.
    update core.subscriptions
       set status = 'active', grace_ends_at = null
     where id = v_row.subscription_id
       and status in ('pending_payment', 'past_due', 'grace', 'expired');
    select s.status into v_sub_after from core.subscriptions s where s.id = v_row.subscription_id;
  end if;

  perform core.log_audit(
    'organisation'::core.audit_scope,
    case p_verdict when 'approved' then 'payment.approved' else 'payment.rejected' end,
    p_org        => v_row.org_id,
    p_actor_type => 'superadmin'::core.actor_type,
    p_actor      => auth.uid(),
    p_payload    => jsonb_build_object(
      'payment_id', v_row.id, 'invoice_id', v_row.invoice_id,
      'amount_inr', v_row.amount_inr, 'reference', v_row.reference,
      'note', v_note,
      'subscription', case when p_verdict = 'approved'
                           then jsonb_build_object('from', v_sub_before, 'to', v_sub_after)
                           else null end
    )
  );
  return v_row;
end;
$fn$;

revoke execute on function core.review_payment(uuid, core.payment_status, text) from public;
grant  execute on function core.review_payment(uuid, core.payment_status, text) to authenticated;

comment on function core.review_payment(uuid, core.payment_status, text) is
  'The operator''s match against the bank statement. Superadmin only, checked '
  'inside; a rejection carries a reason. Approved pays the invoice and makes '
  'the subscription active with no grace deadline, from whichever unpaid state '
  'it had reached.';


-- ---------------------------------------------------------------------------
-- 7. Settle
--
-- The state machine the enum comments in 20260903000001 drew, walked one
-- step per subscription per call:
--
--   trialing         trial_ends_at <= now      -> pending_payment, and the
--                                                 first paid period starts
--                                                 where the trial ended
--   active           current_period_end <= now -> pending_payment, period
--                                                 rolled forward by the plan's
--                                                 billing_period
--   pending_payment  the current period's ISSUED invoice is unpaid and past
--                    due                        -> past_due, grace_ends_at =
--                                                 due_at + plan.grace_days
--   past_due         grace_ends_at <= now       -> expired
--
-- and, off the table it returns, every awaiting_payment row whose window has
-- lapsed becomes expired.
--
-- What is NOT walked, and why:
--
--   * A DRAFT invoice never makes anyone past due. A draft is an invoice the
--     platform could not issue - no seller GSTIN configured, no buyer state on
--     file - and you cannot be late on a bill you were never sent. The
--     subscription stays pending_payment, with full access, until the invoice
--     is issued and its due date passes.
--   * A subscription with a SUBMITTED payment under review is not moved to
--     past_due or expired. The reference may already be on the bank statement;
--     the operator's lag must not narrow the customer's access.
--   * suspended is the operator's verdict on the organisation; expired has
--     nowhere further to go; cancelled is over; grace is reachable today only
--     through the operator console (PATCH .../subscription) and the operator
--     who put a customer there decides when they leave.
--
-- Runs at 00:15 IST, before raise_invoices at 00:30: a period rolled here is
-- invoiced the same night, because raise_invoices invoices any subscription
-- whose current_period_start <= today with no invoice for that start.
--
-- p_now is the clock, defaulted, so a test can move it. p_org narrows the walk
-- to one organisation so a test's fixture organisation can be moved without
-- the seeded ones moving with it.
-- ---------------------------------------------------------------------------
create or replace function core.settle_subscriptions(
  p_now timestamptz default now(),
  p_org uuid        default null
)
returns table (subscription_id uuid, org_id uuid, from_status text, to_status text, reason text)
language plpgsql
volatile
security definer
set search_path = core, pg_catalog
as $fn$
declare
  r          record;
  v_period   interval;
  v_due      timestamptz;
  v_deadline timestamptz;
  v_to       core.subscription_status;
  v_reason   text;
  v_lapsed   uuid;
  v_expired  integer := 0;
begin
  -- Granted to advit_backend only, and checked anyway: a grant can be
  -- re-issued by a later migration, and this walk moves every customer.
  if auth.uid() is not null
     or coalesce(current_setting('role', true), 'none') in ('anon', 'authenticated') then
    raise exception 'the settle walk is the platform''s act, not a session''s'
      using errcode = '42501', hint = 'not_backend';
  end if;
  if p_now is null then
    raise exception 'settle_subscriptions needs a clock' using errcode = '23514', hint = 'now_required';
  end if;

  for r in
    select s.id, s.org_id, s.status, s.trial_ends_at,
           s.current_period_start, s.current_period_end, s.grace_ends_at,
           p.billing_period, p.grace_days,
           exists (select 1 from core.payments pay
                    where pay.subscription_id = s.id and pay.status = 'submitted') as under_review
      from core.subscriptions s
      join core.plans p on p.id = s.plan_id
     where s.cancelled_at is null
       and (p_org is null or s.org_id = p_org)
       and s.status in ('trialing', 'active', 'pending_payment', 'past_due')
     order by s.created_at, s.id
       for update of s
  loop
    v_period := case r.billing_period
                  when 'monthly'   then interval '1 month'
                  when 'quarterly' then interval '3 months'
                  when 'yearly'    then interval '1 year'
                end;
    v_to := null;
    v_reason := null;

    if r.status = 'trialing' then
      -- A trial with no trial_ends_at (PATCH .../subscription can set the
      -- status without stamping one) ends when its period does. A null
      -- deadline is not a longer trial; it is a missing one, and the period
      -- end is the deadline the row does carry.
      v_deadline := coalesce(r.trial_ends_at, r.current_period_end);
      if v_deadline <= p_now then
        update core.subscriptions s
           set status = 'pending_payment',
               current_period_start = v_deadline,
               current_period_end   = v_deadline + v_period
         where s.id = r.id;
        v_to := 'pending_payment'; v_reason := 'trial ended';
      end if;

    elsif r.status = 'active' then
      if r.current_period_end <= p_now then
        update core.subscriptions s
           set status = 'pending_payment',
               current_period_start = r.current_period_end,
               current_period_end   = r.current_period_end + v_period
         where s.id = r.id;
        v_to := 'pending_payment'; v_reason := 'period ended';
      end if;

    elsif r.status = 'pending_payment' then
      if not r.under_review then
        select i.due_at into v_due
          from core.invoices i
         where i.subscription_id = r.id
           and i.period_start = core.period_date(r.current_period_start)
           and i.status = 'issued'
           and i.paid_at is null
           and i.due_at <= p_now;
        if found then
          update core.subscriptions s
             set status = 'past_due',
                 grace_ends_at = v_due + make_interval(days => r.grace_days)
           where s.id = r.id;
          v_to := 'past_due'; v_reason := 'invoice overdue';
        end if;
      end if;

    elsif r.status = 'past_due' then
      if not r.under_review and r.grace_ends_at is null then
        -- Past due with no grace clock: an operator set the status by hand.
        -- Read-only forever is not what past_due means, so the clock starts
        -- now - from the overdue invoice's due date when there is one, from
        -- this walk when there is not - and the row is reported so the
        -- trail says when the countdown began.
        select i.due_at into v_due
          from core.invoices i
         where i.subscription_id = r.id
           and i.status = 'issued' and i.paid_at is null
         order by i.due_at desc limit 1;
        update core.subscriptions s
           set grace_ends_at = coalesce(v_due, p_now) + make_interval(days => r.grace_days)
         where s.id = r.id;
        v_to := 'past_due'; v_reason := 'grace clock started';
      elsif not r.under_review and r.grace_ends_at <= p_now then
        update core.subscriptions s set status = 'expired' where s.id = r.id;
        v_to := 'expired'; v_reason := 'grace ended';
      end if;
    end if;

    if v_to is not null then
      perform core.log_audit(
        'organisation'::core.audit_scope, 'subscription.settled',
        p_org        => r.org_id,
        p_actor_type => 'system'::core.actor_type,
        p_actor      => null,
        p_payload    => jsonb_build_object(
          'subscription_id', r.id, 'from', r.status, 'to', v_to, 'reason', v_reason, 'at', p_now
        )
      );
      subscription_id := r.id; org_id := r.org_id;
      from_status := r.status::text; to_status := v_to::text; reason := v_reason;
      return next;
    end if;
  end loop;

  -- Windows nobody used, closed through the one function that writes
  -- 'expired' so the trail reads the same whichever caller closed the row.
  for v_lapsed in
    select pay.id from core.payments pay
     where pay.status = 'awaiting_payment'
       and pay.window_ends_at <= p_now
       and (p_org is null or pay.org_id = p_org)
  loop
    perform core.expire_payment(v_lapsed, p_now);
    v_expired := v_expired + 1;
  end loop;
  if v_expired > 0 then
    raise notice 'settle_subscriptions: % payment window(s) lapsed', v_expired;
  end if;
  return;
end;
$fn$;

revoke execute on function core.settle_subscriptions(timestamptz, uuid) from public;
grant  execute on function core.settle_subscriptions(timestamptz, uuid) to advit_backend;

comment on function core.settle_subscriptions(timestamptz, uuid) is
  'The nightly walk of the subscription state machine: trial ended, period '
  'ended, invoice overdue, grace exhausted - one step per subscription per '
  'call - and the sweep of lapsed payment windows. Backend only. Nothing else '
  'moves a subscription along that machine except an approved payment.';
