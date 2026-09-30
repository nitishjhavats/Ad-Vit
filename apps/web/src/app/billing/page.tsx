import { Suspense, cache } from "react";

import { applyCoupon } from "@/app/billing/actions";
import { ACCESS_TEXT, AccessBand, PayPanel, PaymentCell } from "@/app/billing/payments";
import {
  Card,
  Flash,
  PERIOD_LABEL,
  Problem,
  Skeleton,
  Tag,
  button,
  day,
  inr,
  select,
  when,
  type Tone,
} from "@/app/billing/ui";
import { Refused } from "@/lib/api";
import { billing, type Invoice, type Plan, type Subscription } from "@/lib/billing";
import { defaultWorkspace, type AuthorizedWorkspace } from "@/lib/session";

export const metadata = { title: "Billing" };

/**
 * The billing page: what the organisation is on, what it is paying, the
 * coupons it may apply, the ladder it could move along, what it has been
 * billed, and how to pay it.
 *
 * Every figure on this page is one the runtime returned. The effective price
 * comes from `core.effective_price` through the subscription route; the
 * coupons in the dropdown are the rows RLS let this caller see through the
 * plans route, which is the same predicate `core.apply_coupon` accepts - so
 * the list and the redemption cannot disagree. Nothing is recomputed here.
 *
 * Changing plan is not self-serve. The operator moves a subscription from the
 * console and it takes effect on the next invoice; this page says so rather
 * than offering a button that would have to be refused.
 *
 * Where the subscription stands - pending payment, past due, expired - is
 * read from the subscription's own `status` and `access_mode`, never worked
 * out from a date on this side. The paying itself lives in `payments.tsx`.
 */

/**
 * The invoices are read once per request and shared by the status band
 * (which names the invoice that is due) and the invoices table. React's
 * `cache()` is per-request in a server component and keyed by the workspace
 * object, which the page resolves once and hands to both.
 */
const invoicesOf = cache((ws: AuthorizedWorkspace) => billing.invoices(ws));
const roleOf = cache((ws: AuthorizedWorkspace) => billing.role(ws));

const STATUS_TONE: Record<string, Tone> = {
  active: "ok",
  trialing: "ok",
  pending_payment: "warn",
  past_due: "warn",
  grace: "warn",
  expired: "bad",
  suspended: "bad",
};

const STATUS_TEXT: Record<string, string> = {
  active: "active",
  trialing: "on trial",
  pending_payment: "awaiting payment",
  past_due: "past due",
  grace: "in grace",
  expired: "expired",
  suspended: "suspended by the operator",
};

// The word for `core.access_mode`, printed beside the status rather than
// folded into it: the status says where the subscription is, the access mode
// says what that means today, and only the runtime knows the second. One
// table, shared with the band in ./payments.tsx.

const INVOICE_TONE: Record<Invoice["status"], Tone> = {
  paid: "ok",
  issued: "muted",
  draft: "warn",
  void: "bad",
};

// ---------------------------------------------------------------------------
// The current subscription
// ---------------------------------------------------------------------------

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">{label}</dt>
      <dd className="mt-0.5 text-sm">{children}</dd>
    </div>
  );
}

function CurrentPlan({ sub, period }: { sub: Subscription; period: string }) {
  const off = Number(sub.percent_off);
  return (
    <dl className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
      <Field label="Plan">
        <span className="text-lg font-semibold">{sub.plan_name}</span>
      </Field>
      <Field label="Status">
        <Tag tone={STATUS_TONE[sub.status] ?? "muted"}>{STATUS_TEXT[sub.status] ?? sub.status}</Tag>
        {sub.status === "trialing" && sub.trial_ends_at && (
          <span className="ml-2 text-xs text-slate-500 dark:text-slate-400">
            trial ends {day(sub.trial_ends_at)}
          </span>
        )}
      </Field>
      <Field label="Access right now">
        <span>{ACCESS_TEXT[sub.access_mode] ?? sub.access_mode}</span>
        {sub.grace_ends_at && (
          <span className="ml-2 text-xs text-slate-500 dark:text-slate-400">
            grace ends {when(sub.grace_ends_at)}
          </span>
        )}
      </Field>
      <Field label="Current period ends">
        <span className="tabular-nums">{day(sub.current_period_end)}</span>
      </Field>
      <Field label="List price">
        <span className="tabular-nums">{inr(sub.list_price_inr)}</span>{" "}
        <span className="text-xs text-slate-500 dark:text-slate-400">{period}</span>
      </Field>
      <Field label="Coupon in force">
        {sub.coupon_code ? (
          <>
            <span className="font-mono font-medium">{sub.coupon_code}</span>{" "}
            <span className="text-xs text-slate-500 dark:text-slate-400">
              {off}% off, saves {inr(sub.discount_inr)} {period}
            </span>
          </>
        ) : (
          <span className="text-slate-500 dark:text-slate-400">none</span>
        )}
      </Field>
      <Field label="You pay">
        <span className="text-lg font-semibold tabular-nums">{inr(sub.price_inr)}</span>{" "}
        <span className="text-xs text-slate-500 dark:text-slate-400">{period}, before GST</span>
      </Field>
    </dl>
  );
}

// ---------------------------------------------------------------------------
// Coupons for the plan the organisation is on
// ---------------------------------------------------------------------------

function Offers({
  ws,
  sub,
  plan,
  period,
}: {
  ws: AuthorizedWorkspace;
  sub: Subscription;
  plan: Plan | undefined;
  period: string;
}) {
  // The dropdown lists only what applies to the CURRENT plan: the coupons
  // array on that plan, as the plans route returned it for this caller.
  const coupons = plan?.coupons ?? [];
  const inForce = sub.coupon_code;

  if (coupons.length === 0) {
    return (
      <p className="text-sm text-slate-500 dark:text-slate-400">
        No coupon applies to {sub.plan_name} right now.
        {inForce ? ` ${inForce} stays in force.` : ""}
      </p>
    );
  }

  return (
    <form action={applyCoupon} className="flex flex-col gap-3 sm:flex-row sm:items-end">
      <input type="hidden" name="workspace_id" value={ws.id} />
      <label className="flex-1 text-sm">
        <span className="text-slate-600 dark:text-slate-400">Coupons that apply to {sub.plan_name}</span>
        <select name="code" required defaultValue="" className={select}>
          <option value="" disabled>
            Choose a coupon
          </option>
          {coupons.map((c) => (
            <option key={c.code} value={c.code}>
              {c.code} · {c.name} · {Number(c.percent_off)}% off
              {c.valid_to ? ` · until ${day(c.valid_to)}` : ""}
              {c.code === inForce ? " · in force" : ""}
            </option>
          ))}
        </select>
      </label>
      <button type="submit" className={button}>
        Apply
      </button>
      <p className="text-xs text-slate-500 dark:text-slate-400 sm:max-w-xs">
        Applying replaces the coupon in force and changes the price {period} from the next invoice.
        Owners and admins only.
      </p>
    </form>
  );
}

// ---------------------------------------------------------------------------
// The ladder
// ---------------------------------------------------------------------------

/**
 * The PRD 19.3 limits, in the order the table states them. A numeric limit is
 * printed as a count; a boolean feature as included or not, with the word and
 * not only the mark; a key the catalogue does not carry says "not stated".
 */
const LIMITS: Array<{ key: string; label: string; kind: "count" | "level" | "flag" }> = [
  { key: "max_ad_accounts", label: "ad accounts", kind: "count" },
  { key: "max_autonomy_level", label: "autonomy", kind: "level" },
  { key: "feature.experiments", label: "Experiments", kind: "flag" },
  { key: "feature.competitor_intel", label: "Competitor intelligence", kind: "flag" },
  { key: "feature.industry_intelligence", label: "Industry intelligence", kind: "flag" },
  { key: "feature.white_label_reports", label: "White-label reports", kind: "flag" },
];

function Limit({ plan, spec }: { plan: Plan; spec: (typeof LIMITS)[number] }) {
  const v = plan.features[spec.key];
  if (v === undefined || v === null) {
    return (
      <li className="text-slate-500 dark:text-slate-400">
        <span aria-hidden="true">? </span>
        {spec.label}: not stated
      </li>
    );
  }
  if (spec.kind === "count") {
    const n = Number(v);
    return (
      <li>
        <span aria-hidden="true">• </span>
        {n} {n === 1 ? "ad account" : spec.label}
      </li>
    );
  }
  if (spec.kind === "level") {
    return (
      <li>
        <span aria-hidden="true">• </span>
        {spec.label} up to L{Number(v)}
      </li>
    );
  }
  const on = v === true;
  return (
    <li className={on ? "" : "text-slate-500 dark:text-slate-400"}>
      <span aria-hidden="true">{on ? "✓ " : "— "}</span>
      {spec.label}
      {on ? "" : " (not included)"}
    </li>
  );
}

function Ladder({ plans, currentKey }: { plans: Plan[]; currentKey: string | null }) {
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      {plans.map((p) => {
        const current = p.key === currentKey;
        return (
          <article
            key={p.plan_id}
            aria-current={current ? "true" : undefined}
            className={`rounded-md border p-4 text-sm ${
              current
                ? "border-slate-900 bg-slate-50 dark:border-slate-100 dark:bg-slate-800"
                : "border-slate-200 dark:border-slate-800"
            }`}
          >
            <div className="flex items-baseline justify-between gap-2">
              <h3 className="text-base font-semibold">{p.name}</h3>
              {current && <Tag tone="ok">your plan</Tag>}
            </div>
            <p className="mt-1 tabular-nums">
              <span className="text-lg font-semibold">{inr(p.price_inr)}</span>{" "}
              <span className="text-xs text-slate-500 dark:text-slate-400">
                {PERIOD_LABEL[p.billing_period] ?? p.billing_period}
              </span>
            </p>
            <ul className="mt-3 space-y-1">
              {LIMITS.map((spec) => (
                <Limit key={spec.key} plan={p} spec={spec} />
              ))}
            </ul>
            <div className="mt-3 border-t border-slate-200 pt-3 text-xs dark:border-slate-800">
              <p className="uppercase tracking-wide text-slate-500 dark:text-slate-400">Coupons accepted</p>
              {p.coupons.length === 0 ? (
                <p className="mt-1 text-slate-500 dark:text-slate-400">none live</p>
              ) : (
                <ul className="mt-1 space-y-0.5">
                  {p.coupons.map((c) => (
                    <li key={c.code}>
                      <span className="font-mono font-medium">{c.code}</span> · {Number(c.percent_off)}% off
                      {c.valid_to ? ` · until ${day(c.valid_to)}` : ""}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </article>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// The sections, each behind its own Suspense boundary
// ---------------------------------------------------------------------------

async function PlanAndOffers({ ws }: { ws: AuthorizedWorkspace }) {
  let plans: Plan[];
  try {
    plans = await billing.plans(ws);
  } catch (e) {
    return <Problem error={e} />;
  }

  // The subscription is read separately so an organisation without a live one
  // still sees the ladder: the 404 the runtime gives it is a fact about the
  // subscription, not about the catalogue.
  let sub: Subscription | null = null;
  let subscriptionProblem: unknown = null;
  try {
    sub = await billing.subscription(ws);
  } catch (e) {
    subscriptionProblem = e;
  }

  const plan = sub ? plans.find((p) => p.key === sub.plan_key) : undefined;
  const period = plan ? (PERIOD_LABEL[plan.billing_period] ?? plan.billing_period) : "per period";
  // The one expected absence: the runtime's 404 "no live subscription". Any
  // other failure is a problem to show, in both cards, not an absent
  // subscription in one and a refusal in the other.
  const noSubscription = subscriptionProblem instanceof Refused && subscriptionProblem.status === 404;

  // The band under the plan names the invoice that is due. A failed invoices
  // read is the table's problem to report; the band then speaks from the
  // subscription alone.
  let invoices: Invoice[] | null = null;
  let canPay = false;
  if (sub && (sub.status === "pending_payment" || sub.status === "past_due" || sub.status === "expired")) {
    try {
      invoices = await invoicesOf(ws);
    } catch {
      invoices = null;
    }
    // Not proved is not an owner: a null role reads as a member here, the
    // same way the invoices table treats it.
    const role = await roleOf(ws);
    canPay = role === "owner" || role === "admin";
  }

  return (
    <>
      <Card title="Your subscription" hint="Every figure here is what the runtime returned; nothing on this page recomputes a price.">
        {sub ? (
          <>
            <CurrentPlan sub={sub} period={period} />
            <AccessBand sub={sub} invoices={invoices} canPay={canPay} />
          </>
        ) : noSubscription ? (
          <p className="text-sm text-slate-500 dark:text-slate-400">No live subscription.</p>
        ) : (
          <Problem error={subscriptionProblem} />
        )}
      </Card>

      <Card
        title="Coupons"
        hint="Only offers that apply to your current plan are listed - the same rows the redemption accepts. A refusal states the database's reason."
      >
        {sub ? (
          <Offers ws={ws} sub={sub} plan={plan} period={period} />
        ) : noSubscription ? (
          <p className="text-sm text-slate-500 dark:text-slate-400">
            There is no live subscription to apply a coupon to.
          </p>
        ) : (
          <Problem error={subscriptionProblem} />
        )}
      </Card>

      <Card
        title="Plans"
        hint="Changing plan is not self-serve: ask your account manager, and the move takes effect on the next invoice."
      >
        {plans.length === 0 ? (
          <p className="text-sm text-slate-500 dark:text-slate-400">No plans are on offer right now.</p>
        ) : (
          <Ladder plans={plans} currentKey={sub?.plan_key ?? null} />
        )}
      </Card>
    </>
  );
}

function Gst({ i }: { i: Invoice }) {
  if (i.gst_split === "cgst_sgst") {
    return (
      <>
        <p className="tabular-nums">CGST {inr(i.cgst_inr)}</p>
        <p className="tabular-nums">SGST {inr(i.sgst_inr)}</p>
      </>
    );
  }
  return <p className="tabular-nums">IGST {inr(i.igst_inr)}</p>;
}

/**
 * The runtime answers a member with an empty list rather than a refusal, so
 * an empty result is ambiguous on its own. The caller's role settles which of
 * the two it is; when the role cannot be established the page says that,
 * rather than asserting either.
 */
async function NoInvoices({ ws }: { ws: AuthorizedWorkspace }) {
  const role = await roleOf(ws);
  if (role === "owner" || role === "admin") {
    return (
      <p className="text-sm text-slate-500 dark:text-slate-400">
        None yet. The platform raises an invoice at 00:30 IST once a billing period ends; as an{" "}
        {role} you will see it here.
      </p>
    );
  }
  if (role === "member") {
    return (
      <p className="text-sm text-slate-500 dark:text-slate-400">
        Invoices are visible to owners and admins of the organisation. Your role is member, so none
        are shown here - ask an owner or admin for a copy.
      </p>
    );
  }
  return (
    <p className="text-sm text-slate-500 dark:text-slate-400">
      Invoices are visible to owners and admins of the organisation. This session&apos;s role could not
      be established, so this page cannot say whether any have been raised.
    </p>
  );
}

async function Invoices({ ws }: { ws: AuthorizedWorkspace }) {
  let rows: Invoice[];
  try {
    rows = await invoicesOf(ws);
  } catch (e) {
    return <Problem error={e} />;
  }
  if (rows.length === 0) return <NoInvoices ws={ws} />;

  // Whether the rows get a "Pay this invoice" button. The role decides the
  // wording only; `core.request_payment` decides who may open a request.
  const role = await roleOf(ws);
  const canPay = role === "owner" || role === "admin";

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-left text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
          <tr>
            <th className="py-2 pr-3">Number</th>
            <th className="py-2 pr-3">Period</th>
            <th className="py-2 pr-3">Plan</th>
            <th className="py-2 pr-3">Taxable</th>
            <th className="py-2 pr-3">GST</th>
            <th className="py-2 pr-3">Total</th>
            <th className="py-2 pr-3">Status</th>
            <th className="py-2 pr-3">Due / paid</th>
            <th className="py-2 pr-3">Payment</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((i) => (
            <tr key={i.id} className="border-t border-slate-200 align-top dark:border-slate-800">
              <td className="py-2 pr-3 font-mono text-xs">{i.number ?? "draft"}</td>
              <td className="py-2 pr-3 text-xs tabular-nums">
                {day(i.period_start)} → {day(i.period_end)}
              </td>
              <td className="py-2 pr-3">
                {i.plan_name}
                {i.coupon_code && (
                  <p className="text-xs text-slate-500 dark:text-slate-400">
                    {i.coupon_code} · {Number(i.percent_off)}% off · −{inr(i.discount_inr)}
                  </p>
                )}
              </td>
              <td className="py-2 pr-3 tabular-nums">{inr(i.taxable_inr)}</td>
              <td className="py-2 pr-3 text-xs">
                <Gst i={i} />
                <p className="text-slate-500 dark:text-slate-400">at {Number(i.gst_rate_percent)}%</p>
              </td>
              <td className="py-2 pr-3 font-medium tabular-nums">{inr(i.total_inr)}</td>
              <td className="py-2 pr-3">
                <Tag tone={INVOICE_TONE[i.status] ?? "muted"}>{i.status}</Tag>
              </td>
              <td className="py-2 pr-3 text-xs tabular-nums">
                {i.paid_at ? `paid ${day(i.paid_at)}` : i.due_at ? `due ${day(i.due_at)}` : "—"}
              </td>
              <td className="py-2 pr-3">
                <PaymentCell ws={ws} invoice={i} canPay={canPay} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default async function BillingPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string; ok?: string; pay?: string }>;
}) {
  // Resolved once, from the signed-in session, and awaited before the
  // Suspense boundaries so an account with no workspace gets one honest
  // message. `defaultWorkspace()` redirects to /login when there is no session.
  const ws = await defaultWorkspace();
  const { error, ok, pay } = await searchParams;

  if (!ws) {
    return (
      <div className="rounded-md border border-slate-200 bg-white p-5 text-sm dark:border-slate-800 dark:bg-slate-900">
        <p className="font-medium">No workspace yet</p>
        <p className="mt-1 text-slate-600 dark:text-slate-400">
          This account is signed in but is not a member of any workspace, so there is no
          subscription to show. An owner or admin of the organisation can add you.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Billing</h1>
      <Flash error={error} ok={ok} />
      <Suspense fallback={<Skeleton rows={3} />}>
        <PlanAndOffers ws={ws} />
      </Suspense>
      {pay && (
        // `?pay=` is only an id until the payments route returns a row with
        // it for this organisation; the panel says so when it does not.
        <Suspense fallback={<Skeleton rows={2} />}>
          <PayPanel ws={ws} paymentId={pay} />
        </Suspense>
      )}
      <Card
        id="invoices"
        title="Invoices"
        hint="Raised by the platform at 00:30 IST. A draft has no number yet; it becomes an invoice once the seller details are configured. An issued invoice is paid by UPI or bank transfer and its reference recorded here."
      >
        <Suspense fallback={<Skeleton rows={2} />}>
          <Invoices ws={ws} />
        </Suspense>
      </Card>
    </div>
  );
}
