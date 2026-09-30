import { requestPayment, submitPayment } from "@/app/billing/actions";
import { Card, Problem, Tag, button, buttonSmall, day, inr, input, when, type Tone } from "@/app/billing/ui";
import {
  billing,
  type AccessMode,
  type Invoice,
  type PayTo,
  type Payment,
  type PaymentsResponse,
  type Subscription,
} from "@/lib/billing";
import type { AuthorizedWorkspace } from "@/lib/session";

/**
 * The paying half of the billing page: where the subscription stands, what
 * each invoice row can do about it, and the panel that says where to send
 * the money and takes the reference back.
 *
 * Everything here renders a fact the runtime returned. The band reads
 * `status` and `access_mode` off the subscription and never works either out
 * from a date; the row reads the invoice's newest payment row; the panel
 * reads the seller's details as the runtime is configured with them. What
 * this file refuses to do is show a button that would have to be refused: a
 * member sees who pays, not a form, and an invoice that is not issued and
 * unpaid has nothing to pay.
 *
 * Nothing on this page moves money. The organisation pays by UPI or bank
 * transfer outside the product and comes back with the reference; the row
 * stays "submitted" until an operator matches it in the console.
 */

export const ACCESS_TEXT: Record<AccessMode, string> = {
  full: "full",
  read_only: "read-only",
  denied: "denied",
};

// The clause that opens a band: what access IS, in the runtime's word. The
// status says where the subscription is; only core.access_mode says what
// that means today (a suspended organisation is denied whatever its
// subscription says), and the band must not contradict the tag beside it.
const ACCESS_CLAUSE: Record<AccessMode, string> = {
  full: "Access is unchanged",
  read_only: "Access is read-only",
  denied: "Access is denied",
};

/**
 * The invoice the band is about. An organisation in pending_payment or
 * past_due has at most one issued, unpaid invoice that matters - the newest -
 * and the band names it and its due date. A draft is reported as a draft: a
 * draft has no number and no due date, and saying "due on —" would be
 * asserting a bill that was never sent.
 */
function outstanding(invoices: Invoice[]): { issued: Invoice | null; draft: Invoice | null } {
  const byPeriod = [...invoices].sort((a, b) => b.period_start.localeCompare(a.period_start));
  return {
    issued: byPeriod.find((i) => i.status === "issued" && !i.paid_at) ?? null,
    draft: byPeriod.find((i) => i.status === "draft") ?? null,
  };
}

// ---------------------------------------------------------------------------
// The status band under the current plan
// ---------------------------------------------------------------------------

/**
 * Shown for the three statuses where money is the way out and silent for the
 * rest. `invoices` is null when the invoices read failed; the band then says
 * what it can from the subscription alone rather than nothing.
 */
export function AccessBand({
  sub,
  invoices,
  canPay,
}: {
  sub: Subscription;
  invoices: Invoice[] | null;
  canPay: boolean;
}) {
  if (sub.status !== "pending_payment" && sub.status !== "past_due" && sub.status !== "expired") {
    return null;
  }
  const access = ACCESS_TEXT[sub.access_mode] ?? sub.access_mode;
  const clause = ACCESS_CLAUSE[sub.access_mode] ?? `Access is ${sub.access_mode}`;
  const payer = canPay ? "Pay it" : "An owner or admin pays it";
  const { issued, draft } = invoices ? outstanding(invoices) : { issued: null, draft: null };
  const named = issued?.number ? `Invoice ${issued.number}` : "The invoice";

  let tone: Tone = "warn";
  let title: string;
  let body: string;

  if (sub.status === "pending_payment") {
    title = "Payment pending";
    if (issued) {
      body = `${named} is issued${issued.due_at ? ` and due on ${day(issued.due_at)}` : ""}. ${payer} to keep full access.`;
    } else if (draft) {
      body =
        "The invoice for this period is still a draft - it becomes payable once the seller details " +
        "are configured - so there is nothing to pay yet.";
    } else if (!canPay) {
      // Invoices are visible to owners and admins only; an empty list here
      // says nothing about whether one has been raised.
      body = "Invoices are visible to owners and admins; an owner or admin pays this period's invoice.";
    } else if (invoices) {
      body = "The invoice for this period is raised at 00:30 IST. Pay it when it appears to keep full access.";
    } else {
      body = "The invoice for this period could not be read just now. Pay it to keep full access.";
    }
  } else if (sub.status === "past_due") {
    title = "Past due";
    const since = issued?.due_at ? `since ${day(issued.due_at)}` : "since the invoice fell due";
    const grace = sub.grace_ends_at ? `grace ends ${when(sub.grace_ends_at)}` : "no grace end is recorded";
    body =
      `${clause} ${since}; ${grace}. ` +
      `Paying ${issued?.number ? `invoice ${issued.number}` : "the invoice"} restores access the moment ` +
      "an operator confirms the payment.";
  } else {
    tone = "bad";
    title = "Subscription expired";
    body =
      `${clause} until ${issued?.number ? `invoice ${issued.number}` : "the invoice"} is paid. ` +
      "Paying restores access the moment an operator confirms the payment.";
  }

  const frame =
    tone === "bad"
      ? "border-red-300 bg-red-50 dark:border-red-900 dark:bg-red-950/40"
      : "border-amber-300 bg-amber-50 dark:border-amber-900 dark:bg-amber-950/40";

  return (
    <div role="status" className={`mt-4 rounded-md border p-4 text-sm ${frame}`}>
      <p className="flex flex-wrap items-center gap-2 font-medium">
        {title}
        <Tag tone={tone}>access right now: {access}</Tag>
      </p>
      <p className="mt-1 text-slate-700 dark:text-slate-300">{body}</p>
      {issued && (
        <p className="mt-2 text-xs">
          <a href="#invoices" className="underline">
            Go to the invoice
          </a>
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// The payment cell on an invoice row
// ---------------------------------------------------------------------------

const muted = "text-xs text-slate-500 dark:text-slate-400";

/**
 * What the row says about paying. `canPay` is whether the caller's role is
 * owner or admin; a role that could not be established is treated as a
 * member, since a guard with nothing to compare against refuses. The button
 * it hides is still refused by `core.request_payment` for a member who finds
 * a way to press it.
 */
export function PaymentCell({
  ws,
  invoice,
  canPay,
}: {
  ws: AuthorizedWorkspace;
  invoice: Invoice;
  canPay: boolean;
}) {
  const p = invoice.payment;

  if (invoice.status === "paid" || (invoice.status === "issued" && invoice.paid_at)) {
    return p?.status === "approved" ? (
      <span className={muted}>approved {when(p.reviewed_at)}</span>
    ) : (
      <span className={muted}>—</span>
    );
  }
  if (invoice.status !== "issued") {
    // A draft has not been sent; a void one never will be.
    return <span className={muted}>—</span>;
  }

  if (p?.status === "awaiting_payment") {
    return (
      <div className="text-xs">
        <p>awaiting your reference</p>
        <p className={muted}>window ends {when(p.window_ends_at)}</p>
        <a href={`/billing?pay=${encodeURIComponent(p.id)}#pay`} className="underline">
          Where to pay and enter the reference
        </a>
      </div>
    );
  }
  if (p?.status === "submitted") {
    return (
      <div className="text-xs">
        <p>
          reference <span className="font-mono">{p.reference ?? "—"}</span> submitted {when(p.submitted_at)}
        </p>
        <p className={muted}>awaiting the operator&apos;s confirmation</p>
      </div>
    );
  }
  if (p?.status === "approved") {
    return <span className="text-xs">paid</span>;
  }

  // No payment row, or the last one is closed (rejected / expired): the
  // organisation may open another.
  const last =
    p?.status === "rejected"
      ? `last attempt rejected ${when(p.reviewed_at)}${p.review_note ? `: ${p.review_note}` : ""}`
      : p?.status === "expired"
        ? `the previous window closed ${when(p.window_ends_at)}`
        : null;

  if (!canPay) {
    return (
      <div className="text-xs">
        <p className={muted}>an owner or admin pays this</p>
        {last && <p className={muted}>{last}</p>}
      </div>
    );
  }
  return (
    <form action={requestPayment} className="text-xs">
      <input type="hidden" name="workspace_id" value={ws.id} />
      <input type="hidden" name="invoice_id" value={invoice.id} />
      <button type="submit" className={buttonSmall}>
        Pay this invoice
      </button>
      {last && <p className={`mt-1 ${muted}`}>{last}</p>}
    </form>
  );
}

// ---------------------------------------------------------------------------
// The pay panel
// ---------------------------------------------------------------------------

function Detail({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">{label}</dt>
      <dd className="mt-0.5 text-sm">{children}</dd>
    </div>
  );
}

/** The seller's details, each block only when the runtime has it. */
function WhereToPay({ to }: { to: PayTo }) {
  const bank = to.account_name || to.account_number || to.ifsc || to.bank_name;
  return (
    <div className="space-y-3">
      {to.upi_id && (
        <dl>
          <Detail label="UPI id">
            <span className="font-mono">{to.upi_id}</span>
          </Detail>
        </dl>
      )}
      {bank && (
        <dl className="grid gap-3 sm:grid-cols-2">
          <Detail label="Account name">{to.account_name ?? "—"}</Detail>
          <Detail label="Account number">
            <span className="font-mono">{to.account_number ?? "—"}</span>
          </Detail>
          <Detail label="IFSC">
            <span className="font-mono">{to.ifsc ?? "—"}</span>
          </Detail>
          <Detail label="Bank">{to.bank_name ?? "—"}</Detail>
        </dl>
      )}
      <p className="text-sm text-slate-700 dark:text-slate-300">{to.note}</p>
    </div>
  );
}

function ReferenceForm({ ws, payment, to }: { ws: AuthorizedWorkspace; payment: Payment; to: PayTo }) {
  const hasBank = Boolean(to.account_number || to.ifsc);
  const hasUpi = Boolean(to.upi_id);
  // When only one way to pay is published, that is the one the owner used.
  const preset = hasUpi && !hasBank ? "upi" : hasBank && !hasUpi ? "bank_transfer" : null;

  return (
    <form action={submitPayment} className="space-y-3">
      <input type="hidden" name="workspace_id" value={ws.id} />
      <input type="hidden" name="payment_id" value={payment.id} />
      <fieldset>
        <legend className="text-sm text-slate-600 dark:text-slate-400">How you paid</legend>
        <div className="mt-2 grid gap-2 sm:grid-cols-2">
          {(
            [
              { value: "upi", label: "UPI", hint: "the 12-digit UPI transaction / reference id" },
              { value: "bank_transfer", label: "Bank transfer", hint: "the UTR from your bank (NEFT, RTGS or IMPS)" },
            ] as const
          ).map((m) => (
            <label
              key={m.value}
              className="flex items-start gap-2 rounded-md border border-slate-300 p-3 text-sm dark:border-slate-700"
            >
              <input
                type="radio"
                name="method"
                value={m.value}
                required
                defaultChecked={preset === m.value}
                className="mt-1"
              />
              <span>
                <span className="font-medium">{m.label}</span>
                <span className="mt-0.5 block text-xs text-slate-600 dark:text-slate-400">{m.hint}</span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>
      <label className="block text-sm">
        <span className="text-slate-600 dark:text-slate-400">UTR / UPI reference</span>
        <input
          name="reference"
          required
          minLength={4}
          maxLength={64}
          autoComplete="off"
          spellCheck={false}
          className={`${input} font-mono`}
        />
      </label>
      <label className="block text-sm sm:max-w-xs">
        <span className="text-slate-600 dark:text-slate-400">Date paid (optional)</span>
        <input name="paid_on" type="date" className={input} />
      </label>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
        <button type="submit" className={button}>
          Record the reference
        </button>
        <p className="text-xs text-slate-500 dark:text-slate-400">
          Owners and admins only. The window closes {when(payment.window_ends_at)}; after that the
          request expires and a new one can be opened from the invoice row.
        </p>
      </div>
    </form>
  );
}

/**
 * What a closed or moved-on request says instead of a form. Each is the
 * row's own state in a sentence, so a stale link explains itself.
 */
function NotOpen({ payment }: { payment: Payment }) {
  const text: Record<Payment["status"], string> = {
    // Never rendered: an awaiting row gets the form, not this paragraph.
    awaiting_payment: "",
    submitted:
      `Its reference was submitted ${when(payment.submitted_at)}; ` +
      "the operator's confirmation is next, and nothing more is needed from you.",
    approved: `It was approved ${when(payment.reviewed_at)} and the invoice is paid.`,
    rejected:
      `It was rejected ${when(payment.reviewed_at)}` +
      `${payment.review_note ? ` (${payment.review_note})` : ""}. ` +
      "Open a new request from the invoice row once the reason is settled.",
    expired: `Its window closed ${when(payment.window_ends_at)}. Open a new request from the invoice row.`,
  };
  return (
    <p className="text-sm text-slate-600 dark:text-slate-400">
      This payment request for {payment.invoice_number ? `invoice ${payment.invoice_number}` : "the invoice"} is
      no longer awaiting a reference. {text[payment.status]}
    </p>
  );
}

/**
 * The panel `?pay=<id>` opens. It is rendered only for a payment the
 * payments route returned for this organisation - an id from elsewhere
 * finds nothing and the panel says so - and shows the form only while the
 * row is awaiting a reference.
 */
export async function PayPanel({ ws, paymentId }: { ws: AuthorizedWorkspace; paymentId: string }) {
  let data: PaymentsResponse;
  try {
    data = await billing.payments(ws);
  } catch (e) {
    return (
      <Card id="pay" title="Pay">
        <Problem error={e} />
      </Card>
    );
  }

  const payment = data.payments.find((p) => p.id === paymentId) ?? null;
  if (!payment) {
    return (
      <Card id="pay" title="Pay">
        <p className="text-sm text-slate-600 dark:text-slate-400">
          No payment request with that id is open for this organisation. The invoice rows below say
          where each one stands.
        </p>
      </Card>
    );
  }

  const title = payment.invoice_number ? `Pay invoice ${payment.invoice_number}` : "Pay the invoice";

  if (payment.status !== "awaiting_payment") {
    return (
      <Card id="pay" title={title}>
        <NotOpen payment={payment} />
      </Card>
    );
  }

  return (
    <Card
      id="pay"
      title={title}
      hint="Pay outside the product, then record the reference here. Nothing is confirmed until an operator matches it."
    >
      <dl className="mb-4 grid gap-3 sm:grid-cols-3">
        <Detail label="Invoice">
          <span className="font-mono">{payment.invoice_number ?? "—"}</span>
        </Detail>
        <Detail label="Amount to pay">
          <span className="text-lg font-semibold tabular-nums">{inr(payment.amount_inr)}</span>{" "}
          <span className="text-xs text-slate-500 dark:text-slate-400">exactly, GST included</span>
        </Detail>
        <Detail label="Window ends">
          <span className="tabular-nums">{when(payment.window_ends_at)}</span>
        </Detail>
      </dl>
      {data.pay_to ? (
        <div className="space-y-5">
          <WhereToPay to={data.pay_to} />
          <div className="border-t border-slate-200 pt-4 dark:border-slate-800">
            <ReferenceForm ws={ws} payment={payment} to={data.pay_to} />
          </div>
        </div>
      ) : (
        <p className="text-sm text-slate-600 dark:text-slate-400">
          The platform has not published its bank details yet, so there is nowhere to send this
          payment and nothing to record here. Write to Broadmate to settle the invoice.
        </p>
      )}
    </Card>
  );
}
