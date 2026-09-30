/**
 * The billing client: plans, the subscription, coupons, invoices and the
 * payments that settle them.
 *
 * Server-side only, through `runtimeCall`, so it inherits the token handling
 * and the error taxonomy of `lib/api.ts` without adding a method per route
 * there. What it refuses is the same thing every workspace-scoped client
 * refuses: a bare string where an `AuthorizedWorkspace` is required. The
 * compile-time test in `__type_tests__/billing.ts` holds that line.
 *
 * Nothing here computes a price. The list price, the percent off, the discount
 * and the effective price all arrive from `core.effective_price` via the
 * subscription route, and the page renders them as given - a second copy of
 * the arithmetic in TypeScript would be a second place for it to be wrong.
 * The same goes for where the subscription stands: `access_mode` is
 * `core.access_mode` as the runtime read it, and the page never derives it
 * from a date.
 *
 * Paying is a bank transfer or a UPI payment made outside this product. What
 * the runtime records is the organisation's own word - a method and a
 * reference - against a payment row it opened for one invoice; nothing is
 * confirmed until an operator matches the reference in the console. So the
 * two writes here are `requestPayment` (open the row, learn where to pay) and
 * `submitPayment` (record the reference), and neither moves money.
 *
 * One read goes to Supabase rather than the runtime: the caller's organisation
 * role. The invoices route deliberately answers a member with an empty list
 * rather than a refusal (so a media buyer cannot learn that there is something
 * to see), which leaves the page unable to tell "none yet" from "not yours to
 * see" without asking. The role changes only the wording of the empty state,
 * never what is shown - the rows are decided by RLS on the other side.
 */

import "server-only";

import { runtimeCall } from "@/lib/api";
import { currentUser, type AuthorizedWorkspace } from "@/lib/session";
import { supabaseServer } from "@/lib/supabase/server";

// ---------------------------------------------------------------------------
// Types. Narrow on purpose: only the fields the page renders. Money and
// percentages arrive as numeric from Postgres, which the runtime serialises
// as a number or a string depending on scale, so every one is `string | number`
// and is passed through Number() exactly once, at render.
// ---------------------------------------------------------------------------

export type PlanCoupon = {
  code: string;
  name: string;
  percent_off: string | number;
  valid_to: string | null;
};

/**
 * `features` is the plan's `core.plan_features` rows keyed by feature_key,
 * with the JSON value as stored: `max_ad_accounts` and `max_autonomy_level`
 * are numbers, the `feature.*` keys are booleans. A key the catalogue does
 * not carry is simply absent, and the page says "not stated" rather than
 * guessing a default.
 */
export type Plan = {
  plan_id: string;
  key: string;
  name: string;
  description: string | null;
  price_inr: string | number;
  billing_period: "monthly" | "quarterly" | "yearly";
  trial_days: number;
  sort_order: number;
  coupons: PlanCoupon[];
  features: Record<string, unknown>;
};

/**
 * What `core.access_mode` answers for the organisation: `full` runs the
 * product, `read_only` shows it, `denied` shows this page and little else.
 * It is a fact the runtime read, not something the page works out from
 * `grace_ends_at` or the period end.
 */
export type AccessMode = "full" | "read_only" | "denied";

export type Subscription = {
  subscription_id: string;
  status: string;
  plan_key: string;
  plan_name: string;
  current_period_start: string | null;
  current_period_end: string | null;
  trial_ends_at: string | null;
  grace_ends_at: string | null;
  access_mode: AccessMode;
  coupon_code: string | null;
  list_price_inr: string | number;
  percent_off: string | number;
  discount_inr: string | number;
  price_inr: string | number;
};

/**
 * One attempt to pay one invoice, as `core.payments` holds it. `draft` is in
 * the database enum but is never written, so it is not spelled here: a row
 * the page can see is at least awaiting a reference.
 */
export type PaymentStatus = "awaiting_payment" | "submitted" | "approved" | "rejected" | "expired";

export type Payment = {
  id: string;
  invoice_id: string;
  invoice_number: string | null;
  amount_inr: string;
  status: PaymentStatus;
  method: string | null;
  reference: string | null;
  paid_on: string | null;
  window_ends_at: string | null;
  submitted_at: string | null;
  reviewed_at: string | null;
  review_note: string | null;
  created_at: string;
};

/**
 * Where to send the money: the seller's bank account and UPI id as the
 * runtime is configured with them. Every field but the note may be null,
 * and the note is one fixed sentence the runtime writes, rendered as given.
 * The payments route answers `pay_to: null` when nothing is configured, and
 * the request route refuses (503) before opening a row - an organisation is
 * never told to pay with nowhere to pay to.
 */
export type PayTo = {
  account_name: string | null;
  account_number: string | null;
  ifsc: string | null;
  bank_name: string | null;
  upi_id: string | null;
  note: string;
};

export type PaymentsResponse = {
  pay_to: PayTo | null;
  payments: Payment[];
};

/** What opening a payment request returns: the row and where to pay it. */
export type PaymentRequest = {
  payment: Payment;
  pay_to: PayTo;
};

export type PaymentSubmission = {
  method: "upi" | "bank_transfer";
  reference: string;
  paid_on: string | null;
};

export type Invoice = {
  id: string;
  number: string | null;
  status: "draft" | "issued" | "paid" | "void";
  period_start: string;
  period_end: string;
  plan_name: string;
  list_price_inr: string | number;
  coupon_code: string | null;
  percent_off: string | number;
  discount_inr: string | number;
  taxable_inr: string | number;
  gst_rate_percent: string | number;
  gst_split: "cgst_sgst" | "igst";
  cgst_inr: string | number;
  sgst_inr: string | number;
  igst_inr: string | number;
  total_inr: string | number;
  issued_at: string | null;
  due_at: string | null;
  paid_at: string | null;
  /** The newest payment row for this invoice in any status; null when none was ever opened. */
  payment: Payment | null;
};

export type OrgRole = "owner" | "admin" | "member";

// ---------------------------------------------------------------------------

/**
 * The caller's role in the organisation that owns this workspace, read
 * through PostgREST as the caller. Two policies decide the answer -
 * `workspaces` (members see their own) and `org_members_select` (fellow
 * members see each other) - so this cannot name a role the caller could not
 * see for themselves.
 *
 * `null` means the role could not be established, and the page treats that
 * the way a guard with nothing to compare against must: as not proved.
 */
async function orgRole(ws: AuthorizedWorkspace): Promise<OrgRole | null> {
  const user = await currentUser();
  if (!user) return null;

  const supabase = await supabaseServer();
  const workspace = await supabase
    .schema("t_advit")
    .from("workspaces")
    .select("org_id")
    .eq("id", ws.id)
    .maybeSingle();
  if (workspace.error || !workspace.data) return null;

  const membership = await supabase
    .schema("core")
    .from("organisation_members")
    .select("role")
    .eq("org_id", (workspace.data as { org_id: string }).org_id)
    .eq("user_id", user.id)
    .maybeSingle();
  if (membership.error || !membership.data) return null;

  const role = (membership.data as { role: string }).role;
  return role === "owner" || role === "admin" || role === "member" ? role : null;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * An id that came from a form is a string the browser sent, and a string
 * with a slash in it would reshape the runtime path - "../admin/payments"
 * lands a POST, carrying the caller's own bearer token, on a route this
 * client must not be able to spell. The runtime declares both ids as UUIDs,
 * so anything else is refused here with our sentence rather than sent.
 */
export class NotAPaymentId extends Error {
  constructor(what: string) {
    super(`${what} is not an id this page issued`);
    this.name = "NotAPaymentId";
  }
}

function idSegment(value: string, what: string): string {
  if (!UUID.test(value)) throw new NotAPaymentId(what);
  return encodeURIComponent(value);
}

export const billing = {
  // Every one of these takes a workspace that lib/session.ts has proved. The
  // signature is the enforcement; see __type_tests__/billing.ts.
  plans: (ws: AuthorizedWorkspace) =>
    runtimeCall<Plan[]>(`/api/workspaces/${ws.id}/billing/plans`),
  subscription: (ws: AuthorizedWorkspace) =>
    runtimeCall<Subscription>(`/api/workspaces/${ws.id}/billing/subscription`),
  applyCoupon: (ws: AuthorizedWorkspace, code: string) =>
    runtimeCall<Subscription>(`/api/workspaces/${ws.id}/billing/coupon`, {
      method: "POST",
      body: JSON.stringify({ code }),
    }),
  invoices: (ws: AuthorizedWorkspace) =>
    runtimeCall<Invoice[]>(`/api/workspaces/${ws.id}/billing/invoices`),
  payments: (ws: AuthorizedWorkspace) =>
    runtimeCall<PaymentsResponse>(`/api/workspaces/${ws.id}/billing/payments`),
  // 201 for a fresh row, 200 for the awaiting row that already exists for the
  // invoice; the body is the same shape either way, so the caller need not
  // tell them apart to land on the pay panel.
  requestPayment: (ws: AuthorizedWorkspace, invoiceId: string) =>
    runtimeCall<PaymentRequest>(
      `/api/workspaces/${ws.id}/billing/invoices/${idSegment(invoiceId, "the invoice")}/payment-request`,
      { method: "POST" },
    ),
  // The reference rides in the body and nowhere else: not in the path, not
  // in a redirect, not in the ok sentence the action composes afterwards.
  submitPayment: (ws: AuthorizedWorkspace, paymentId: string, body: PaymentSubmission) =>
    runtimeCall<Payment>(
      `/api/workspaces/${ws.id}/billing/payments/${idSegment(paymentId, "the payment")}/submit`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  role: orgRole,
};
