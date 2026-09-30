import { Suspense } from "react";

import { api } from "@/lib/api";
import { requireOperator, type Operator } from "@/lib/session";
import { Card, Problem, Skeleton, Tag, inr, day } from "@/components/ui";

export const metadata = { title: "Invoices" };

/**
 * Every invoice the platform has raised, drafts first. An invoice is a
 * snapshot; nothing on this page edits one. Money is collected elsewhere -
 * paid_at exists for a gateway's webhook and nothing here sets it.
 */

const STATUS_TONE: Record<string, "ok" | "warn" | "bad" | "muted"> = {
  paid: "ok",
  issued: "muted",
  draft: "warn",
  void: "bad",
};

async function Table({ op, status }: { op: Operator; status?: string }) {
  let rows;
  try {
    rows = await api.invoices(op, status);
  } catch (e) {
    return <Problem error={e} />;
  }
  if (rows.length === 0) return <p className="text-sm text-slate-500">No invoices{status ? ` with status ${status}` : ""}.</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-left text-xs uppercase tracking-wide text-slate-500">
          <tr>
            <th className="py-2 pr-3">Number</th>
            <th className="py-2 pr-3">Organisation</th>
            <th className="py-2 pr-3">Period</th>
            <th className="py-2 pr-3">Plan</th>
            <th className="py-2 pr-3">Total</th>
            <th className="py-2 pr-3">GST</th>
            <th className="py-2 pr-3">Status</th>
            <th className="py-2 pr-3">Due</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((i) => (
            <tr key={i.id} className="border-t border-slate-200 align-top dark:border-slate-800">
              <td className="py-2 pr-3 font-mono text-xs">{i.number ?? "—"}</td>
              <td className="py-2 pr-3">
                <a href={`/organisations/${i.org_id}`} className="underline underline-offset-2">
                  {i.org_name}
                </a>
              </td>
              <td className="py-2 pr-3 text-xs">
                {i.period_start} → {i.period_end}
              </td>
              <td className="py-2 pr-3">{i.plan_name}</td>
              <td className="py-2 pr-3 tabular-nums">{inr(i.total_inr)}</td>
              <td className="py-2 pr-3 text-xs">{i.gst_split === "cgst_sgst" ? "CGST+SGST" : "IGST"}</td>
              <td className="py-2 pr-3">
                <Tag tone={STATUS_TONE[i.status] ?? "muted"}>{i.status}</Tag>
                {i.draft_reason && <p className="mt-1 max-w-xs text-xs text-slate-500">{i.draft_reason}</p>}
                {i.paid_at && <p className="mt-1 text-xs text-slate-500">paid {day(i.paid_at)}</p>}
              </td>
              <td className="py-2 pr-3 tabular-nums text-xs">{day(i.due_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const FILTERS = ["", "draft", "issued", "paid", "void"];

export default async function InvoicesPage({
  searchParams,
}: {
  searchParams: Promise<{ status?: string }>;
}) {
  const op = await requireOperator();
  const { status } = await searchParams;
  const active = FILTERS.includes(status ?? "") ? (status ?? "") : "";
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Invoices</h1>
      <Card
        title="Raised by the platform"
        hint="00:30 IST daily. A draft says why it could not be issued; fix the organisation’s state code or the seller settings and the next run issues it."
        action={
          <nav className="flex gap-3 text-sm">
            {FILTERS.map((f) => (
              <a
                key={f || "all"}
                href={f ? `/invoices?status=${f}` : "/invoices"}
                className={f === active ? "font-semibold underline underline-offset-2" : "text-slate-500 hover:underline"}
              >
                {f || "all"}
              </a>
            ))}
          </nav>
        }
      >
        <Suspense fallback={<Skeleton rows={4} />}>
          <Table op={op} status={active || undefined} />
        </Suspense>
      </Card>
    </div>
  );
}
