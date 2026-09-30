"use server";

/**
 * The mutations the billing page makes: apply a coupon to the organisation's
 * live subscription, open a payment request for an invoice, and record the
 * reference of a payment made.
 *
 * Server Actions, so Next checks Origin against Host while the session sits
 * in an httpOnly cookie. Each one re-proves the workspace (`requireWorkspace`)
 * before calling the runtime, and the runtime re-proves membership again and
 * then hands the decision to a SECURITY DEFINER function in `core` - so what
 * this file contributes is the form parsing and the redirect, not a decision.
 *
 * Refusals come back as `?error=` on the page rather than as a thrown error,
 * so the reason the database gave - `coupon_wrong_plan`, `invoice_not_payable`,
 * `window_elapsed`, `bank_details_unavailable` - is shown verbatim beside the
 * form that caused it. Success comes back as `?ok=` naming what the runtime
 * said happened, never recomputed here.
 *
 * One field never rides a redirect: the UTR / UPI reference. It goes into the
 * request body of `billing.submitPayment` and the ok sentence says only that
 * a reference was recorded. A URL ends up in a browser history, a proxy log
 * and a support screenshot; the row on the page shows the reference to the
 * people RLS lets see it.
 */

import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";

import { NotAuthorized, Refused, RuntimeUnreachable } from "@/lib/api";
import { billing, NotAPaymentId, type PaymentSubmission } from "@/lib/billing";
import { requireWorkspace } from "@/lib/session";

const PATH = "/billing";

/**
 * Redirect back to the page. `pay` names the payment whose panel should be
 * open on landing - the request action uses it so the owner arrives at the
 * bank details and the reference form without a second click.
 */
function back({ error, ok, pay }: { error?: string; ok?: string; pay?: string }): never {
  const q = new URLSearchParams();
  if (error) q.set("error", error);
  if (ok) q.set("ok", ok);
  if (pay) q.set("pay", pay);
  const qs = q.toString();
  revalidatePath(PATH);
  redirect(qs ? `${PATH}?${qs}${pay ? "#pay" : ""}` : PATH);
}

/**
 * Run one runtime call and turn its outcome into a redirect. Every refusal -
 * the 422s with a hint, the 403 for a member, the 409 for a payment already
 * open, the 503 for missing bank details - arrives as Refused carrying the
 * runtime's own sentence, and that is what the owner reads. Nothing here
 * names a cause the runtime did not send.
 *
 * redirect() works by throwing, so the success redirect stays outside the
 * try: a `back()` inside it would be caught as if the runtime had refused.
 */
async function attempt<T>(
  fn: () => Promise<T>,
  done: (result: T) => { ok?: string; pay?: string },
  unreachable: string,
  keep: { pay?: string } = {},
): Promise<never> {
  let result: T;
  try {
    result = await fn();
  } catch (e) {
    // A refusal the runtime worded is shown as worded - unless it is
    // pydantic's, which is a JSON list that echoes the input; that becomes
    // one fixed sentence, because the input may be the reference.
    if (e instanceof Refused) back({ ...keep, error: worded(e) });
    // Only a missing or expired session is NotAuthorized. Its message carries
    // the workspace id and the route; neither belongs in a URL.
    if (e instanceof NotAuthorized) back({ error: "your session has expired; sign in again" });
    if (e instanceof RuntimeUnreachable) {
      back({ ...keep, error: `the agent runtime is not reachable; ${unreachable}` });
    }
    if (e instanceof NotAPaymentId) back({ error: e.message });
    // Anything else is ours to log, not the owner's to read: a message from
    // a parser or a socket names nothing they can act on.
    console.error("billing action failed:", e);
    back({ ...keep, error: `something failed on our side; ${unreachable}` });
  }
  back(done(result));
}

function worded(e: Refused): string {
  const d = e.detail.trim();
  return d.startsWith("[") || d.startsWith("{")
    ? "the runtime refused the form as sent; check the fields and try again"
    : d;
}

const str = (fd: FormData, k: string) => String(fd.get(k) ?? "").trim();

// YYYY-MM-DD that is a day on the calendar: 2026-02-30 matches the shape and
// is not one, and the runtime's refusal would echo it.
function isCalendarDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const d = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === value;
}

const rupees = (v: string | number) =>
  `₹${Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;

export async function applyCoupon(formData: FormData) {
  // The workspace id rides in a hidden field so the action acts on the
  // workspace whose offers the page showed. It is still only an id until
  // requireWorkspace has proved it against the caller's own memberships.
  const ws = await requireWorkspace(str(formData, "workspace_id"));
  const code = str(formData, "code").toUpperCase();
  if (!code) back({ error: "choose a coupon from the list first" });

  return attempt(
    () => billing.applyCoupon(ws, code),
    (sub) => ({
      ok:
        `${sub.coupon_code ?? code} applied: ${sub.plan_name} is now ${rupees(sub.price_inr)} ` +
        `(list ${rupees(sub.list_price_inr)}, ${Number(sub.percent_off)}% off)`,
    }),
    "the coupon was not applied",
  );
}

/**
 * Open a payment request for one issued invoice. The runtime answers with the
 * row (fresh, or the one still awaiting a reference for this invoice) and
 * where to pay, and the page lands on the pay panel for it. A member's
 * attempt is refused by `core.request_payment` itself, not by anything here:
 * the button is hidden for a member, but a hidden button is not a guard.
 */
export async function requestPayment(formData: FormData) {
  const ws = await requireWorkspace(str(formData, "workspace_id"));
  const invoiceId = str(formData, "invoice_id");
  if (!invoiceId) back({ error: "the form did not say which invoice to pay" });

  return attempt(
    () => billing.requestPayment(ws, invoiceId),
    ({ payment }) => ({
      pay: payment.id,
      ok:
        `${payment.invoice_number ? `invoice ${payment.invoice_number}` : "the invoice"} is ready to pay: ` +
        `${rupees(payment.amount_inr)} exactly; where to send it and the reference form are below`,
    }),
    "no payment request was opened",
  );
}

type Method = PaymentSubmission["method"];
const METHOD_TEXT: Record<Method, string> = {
  upi: "UPI",
  bank_transfer: "bank transfer",
};
// Object.hasOwn, not `in`: "constructor" is in every object.
const isMethod = (v: string): v is Method => Object.hasOwn(METHOD_TEXT, v);

/**
 * Record how an invoice was paid and the reference to match it by. The
 * contract's length rule is checked here first so a too-short reference is
 * refused with our sentence rather than pydantic's, which echoes the input;
 * everything else - the row's state, the window - is the database's call.
 */
export async function submitPayment(formData: FormData) {
  const ws = await requireWorkspace(str(formData, "workspace_id"));
  const paymentId = str(formData, "payment_id");
  if (!paymentId) back({ error: "the form did not say which payment this reference is for" });

  const method = str(formData, "method");
  if (!isMethod(method)) back({ error: "say whether you paid by UPI or by bank transfer", pay: paymentId });

  const reference = str(formData, "reference");
  if (reference.length < 4 || reference.length > 64) {
    back({ error: "the UTR / UPI reference must be between 4 and 64 characters", pay: paymentId });
  }

  // A date input submits YYYY-MM-DD or nothing. Anything else is a browser
  // without a date picker sending free text, and the runtime would refuse it
  // with pydantic's wording; refusing here keeps the sentence ours.
  const paidOnRaw = str(formData, "paid_on");
  if (paidOnRaw && !isCalendarDate(paidOnRaw)) {
    back({ error: "the date paid must be a calendar date (YYYY-MM-DD)", pay: paymentId });
  }
  const paid_on = paidOnRaw || null;

  return attempt(
    () => billing.submitPayment(ws, paymentId, { method, reference, paid_on }),
    (payment) => ({
      // The reference itself stays out of the URL on purpose; the invoice row
      // shows it to whoever may see the row.
      ok:
        `${METHOD_TEXT[method]} reference recorded for ` +
        `${payment.invoice_number ? `invoice ${payment.invoice_number}` : "the invoice"} ` +
        `(${rupees(payment.amount_inr)}); nothing is confirmed until an operator matches it`,
    }),
    "the reference was not recorded",
    { pay: paymentId },
  );
}
