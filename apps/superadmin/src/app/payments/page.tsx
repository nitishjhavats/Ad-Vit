import { Suspense } from "react";

import { reviewPayment } from "@/app/actions";
import { api, type AdminPayment } from "@/lib/api";
import { requireOperator, type Operator } from "@/lib/session";
import { Card, Problem, Skeleton, Tag, button, buttonQuiet, input, inr, day, when } from "@/components/ui";

export const metadata = { title: "Payments" };

/**
 * The payments queue: the one place money is confirmed.
 *
 * There is no gateway. A tenant pays an issued invoice by bank transfer or
 * UPI, quotes the UTR / UPI reference on their billing page, and the row
 * sits here as `submitted` until a person finds that reference on the bank
 * statement. Approving is what marks the invoice paid and puts the
 * subscription back on `active`; rejecting closes the row with a reason the
 * tenant reads, and they may request again. The runtime refuses a rejection
 * without a note, so the form asks for one beside the field.
 *
 * Nothing here is automatic and nothing here is reversible from this page:
 * an approval that turns out to be wrong is a new problem for the trail, not
 * an undo button.
 */

const METHOD: Record<string, string> = { upi: "UPI", bank_transfer: "bank transfer" };
const VERDICT_TONE: Record<AdminPayment["status"], "ok" | "warn" | "bad" | "muted"> = {
  approved: "ok",
  rejected: "bad",
  expired: "muted",
  submitted: "warn",
  awaiting_payment: "muted",
};

function Flash({ error, ok }: { error?: string; ok?: string }) {
  if (error) {
    return (
      <p className="rounded-md border border-red-300 bg-red-50 p-3 text-sm dark:border-red-900 dark:bg-red-950/40">
        <span className="font-medium">Refused: </span>
        {error}
      </p>
    );
  }
  if (ok) {
    return (
      <p className="rounded-md border border-emerald-300 bg-emerald-50 p-3 text-sm dark:border-emerald-900 dark:bg-emerald-950/40">
        {ok}
      </p>
    );
  }
  return null;
}

/**
 * The reference is what the operator searches the bank statement for, so it
 * is monospace and one click selects all of it. No client JavaScript: the
 * console has none, and `user-select: all` is enough to copy from.
 */
function Reference({ value }: { value: string | null }) {
  if (!value) return <span className="text-slate-500">—</span>;
  return (
    <code className="select-all rounded bg-slate-100 px-1.5 py-0.5 font-mono text-xs dark:bg-slate-800" title="Click to select, then copy">
      {value}
    </code>
  );
}

function OrgLink({ p }: { p: AdminPayment }) {
  return (
    <>
      <a href={`/organisations/${p.org_id}`} className="underline underline-offset-2">
        {p.org_name}
      </a>
      <p className="text-xs text-slate-500">{p.org_slug}</p>
    </>
  );
}

function QueueRow({ p }: { p: AdminPayment }) {
  return (
    <tr className="border-t border-slate-200 align-top dark:border-slate-800">
      <td className="py-2 pr-3">
        <OrgLink p={p} />
      </td>
      <td className="py-2 pr-3 font-mono text-xs">{p.invoice_number ?? "—"}</td>
      <td className="py-2 pr-3 tabular-nums">{inr(p.amount_inr)}</td>
      <td className="py-2 pr-3 text-xs">{p.method ? (METHOD[p.method] ?? p.method) : "—"}</td>
      <td className="py-2 pr-3">
        <Reference value={p.reference} />
      </td>
      <td className="py-2 pr-3 tabular-nums text-xs">{day(p.paid_on)}</td>
      <td className="py-2 pr-3 text-xs">
        <p className="tabular-nums">{when(p.submitted_at)}</p>
        <p className="text-slate-500">{p.submitted_by_email ?? "submitter unknown"}</p>
      </td>
      <td className="py-2 pr-3">
        <div className="flex min-w-72 flex-col gap-3">
          <form action={reviewPayment} className="flex flex-col gap-1">
            <input type="hidden" name="payment_id" value={p.id} />
            <input type="hidden" name="verdict" value="approved" />
            <label className="text-xs text-slate-600 dark:text-slate-400">
              Note (optional)
              {/* A textarea, not an input: Enter in a single-line field submits
                  the form, and approving is the irreversible act on this page.
                  The only way to approve is the button that says so. */}
              <textarea name="note" maxLength={500} rows={1} placeholder="e.g. statement line 14, 12 Sep" className={`${input} mt-0.5`} />
            </label>
            <button type="submit" className={`${button} self-start`}>
              Approve - matched on the statement
            </button>
          </form>
          <form action={reviewPayment} className="flex flex-col gap-1 border-t border-slate-200 pt-3 dark:border-slate-800">
            <input type="hidden" name="payment_id" value={p.id} />
            <input type="hidden" name="verdict" value="rejected" />
            <label className="text-xs text-slate-600 dark:text-slate-400">
              Reason (required - the tenant reads it, and the runtime refuses a rejection without one)
              <textarea name="note" required minLength={3} maxLength={500} rows={2} placeholder="e.g. no credit for this UTR by 15 Sep; amount ₹500 short" className={`${input} mt-0.5`} />
            </label>
            <button type="submit" className={`${buttonQuiet} self-start`}>
              Reject
            </button>
          </form>
        </div>
      </td>
    </tr>
  );
}

async function Queue({ op }: { op: Operator }) {
  let rows;
  try {
    rows = await api.payments(op, "submitted");
  } catch (e) {
    return <Problem error={e} />;
  }
  if (rows.length === 0) {
    return <p className="text-sm text-slate-500">Nothing is waiting. No payment has been submitted for review.</p>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-left text-xs uppercase tracking-wide text-slate-500">
          <tr>
            <th className="py-2 pr-3">Organisation</th>
            <th className="py-2 pr-3">Invoice</th>
            <th className="py-2 pr-3">Amount</th>
            <th className="py-2 pr-3">Method</th>
            <th className="py-2 pr-3">Reference</th>
            <th className="py-2 pr-3">Paid on</th>
            <th className="py-2 pr-3">Submitted (IST)</th>
            <th className="py-2 pr-3">Review</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((p) => (
            <QueueRow key={p.id} p={p} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * When a closed row stopped mattering: the review for approved and
 * rejected, the end of the window for expired (nobody reviewed those).
 */
function closedAt(p: AdminPayment): string {
  return p.reviewed_at ?? p.window_ends_at ?? p.created_at;
}

const CLOSED: ReadonlySet<AdminPayment["status"]> = new Set(["approved", "rejected", "expired"]);

async function Reviewed({ op }: { op: Operator }) {
  let rows;
  try {
    rows = await api.payments(op, "all", 1000);
  } catch (e) {
    return <Problem error={e} />;
  }
  const closed = rows
    .filter((p) => CLOSED.has(p.status))
    // By instant, not by string: two ISO timestamps with different offsets
    // do not order lexically.
    .sort((a, b) => Date.parse(closedAt(b)) - Date.parse(closedAt(a)))
    .slice(0, 50);
  if (closed.length === 0) return <p className="text-sm text-slate-500">No payment has been reviewed yet.</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-left text-xs uppercase tracking-wide text-slate-500">
          <tr>
            <th className="py-2 pr-3">Organisation</th>
            <th className="py-2 pr-3">Invoice</th>
            <th className="py-2 pr-3">Amount</th>
            <th className="py-2 pr-3">Reference</th>
            <th className="py-2 pr-3">Verdict</th>
            <th className="py-2 pr-3">Note</th>
            <th className="py-2 pr-3">When (IST)</th>
          </tr>
        </thead>
        <tbody>
          {closed.map((p) => (
            <tr key={p.id} className="border-t border-slate-200 align-top dark:border-slate-800">
              <td className="py-2 pr-3">
                <OrgLink p={p} />
              </td>
              <td className="py-2 pr-3 font-mono text-xs">{p.invoice_number ?? "—"}</td>
              <td className="py-2 pr-3 tabular-nums">{inr(p.amount_inr)}</td>
              <td className="py-2 pr-3">
                <Reference value={p.reference} />
              </td>
              <td className="py-2 pr-3">
                <Tag tone={VERDICT_TONE[p.status]}>{p.status}</Tag>
              </td>
              <td className="max-w-xs py-2 pr-3 text-xs text-slate-600 dark:text-slate-400">
                {p.review_note ?? (p.status === "expired" ? "nobody submitted a reference before the window ended" : "—")}
              </td>
              <td className="py-2 pr-3 tabular-nums text-xs">
                {p.status === "expired" ? `window ended ${when(p.window_ends_at)}` : when(p.reviewed_at)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default async function PaymentsPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string; ok?: string }>;
}) {
  const op = await requireOperator();
  const { error, ok } = await searchParams;
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Payments</h1>
      <Flash error={error} ok={ok} />
      <Card
        title="Waiting for review"
        hint="A tenant paid an issued invoice by bank transfer or UPI and quoted the reference. Find it on the bank statement and check the amount is the invoice total. Approve marks the invoice paid and puts the subscription on active; Reject closes the row with a reason and the organisation may request again. Nothing is confirmed until you do."
      >
        <Suspense fallback={<Skeleton rows={3} />}>
          <Queue op={op} />
        </Suspense>
      </Card>
      <Card
        title="Recently reviewed"
        hint="The last 50 rows that closed: approved, rejected, or expired because no reference was submitted in time. The full record is in the trail under payment.*."
        action={
          <a href="/audit?prefix=payment." className="text-sm underline underline-offset-2">
            In the trail
          </a>
        }
      >
        <Suspense fallback={<Skeleton rows={3} />}>
          <Reviewed op={op} />
        </Suspense>
      </Card>
    </div>
  );
}
