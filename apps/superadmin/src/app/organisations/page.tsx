import { Suspense } from "react";
import Link from "next/link";

import { api } from "@/lib/api";
import { requireOperator, type Operator } from "@/lib/session";
import { Card, Problem, Skeleton, Tag, day } from "@/components/ui";

export const metadata = { title: "Organisations" };

const ORG_TONE: Record<string, "ok" | "warn" | "bad" | "muted"> = {
  active: "ok",
  pending_activation: "warn",
  suspended: "bad",
};
const SUB_TONE: Record<string, "ok" | "warn" | "bad" | "muted"> = {
  active: "ok",
  trialing: "ok",
  pending_payment: "warn",
  past_due: "warn",
  grace: "warn",
  expired: "bad",
  suspended: "bad",
};

async function Table({ op }: { op: Operator }) {
  let orgs;
  try {
    orgs = await api.organisations(op);
  } catch (e) {
    return <Problem error={e} />;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-left text-xs uppercase tracking-wide text-slate-500">
          <tr>
            <th className="py-2 pr-3">Organisation</th>
            <th className="py-2 pr-3">Status</th>
            <th className="py-2 pr-3">Plan</th>
            <th className="py-2 pr-3">Subscription</th>
            <th className="py-2 pr-3">Period ends</th>
            <th className="py-2 pr-3">Members</th>
            <th className="py-2 pr-3">Workspaces</th>
            <th className="py-2 pr-3">Ad accounts</th>
            <th className="py-2 pr-3">Overrides</th>
            <th className="py-2 pr-3">Billing</th>
          </tr>
        </thead>
        <tbody>
          {orgs.map((o) => (
            <tr key={o.id} className="border-t border-slate-200 dark:border-slate-800">
              <td className="py-2 pr-3">
                <a href={`/organisations/${o.id}`} className="font-medium underline underline-offset-2">
                  {o.name}
                </a>
                <p className="text-xs text-slate-500">{o.slug}</p>
              </td>
              <td className="py-2 pr-3">
                <Tag tone={ORG_TONE[o.status] ?? "muted"}>{o.status.replace("_", " ")}</Tag>
              </td>
              <td className="py-2 pr-3">
                {o.plan_name ?? "—"}
                {o.coupon_code && <p className="text-xs text-slate-500">coupon {o.coupon_code}</p>}
              </td>
              <td className="py-2 pr-3">
                {o.subscription_status ? (
                  <Tag tone={SUB_TONE[o.subscription_status] ?? "muted"}>{o.subscription_status.replace("_", " ")}</Tag>
                ) : (
                  <Tag tone="bad">none</Tag>
                )}
              </td>
              <td className="py-2 pr-3 tabular-nums">{day(o.current_period_end)}</td>
              <td className="py-2 pr-3 tabular-nums">{o.members}</td>
              <td className="py-2 pr-3 tabular-nums">{o.workspaces}</td>
              <td className="py-2 pr-3 tabular-nums">{o.ad_accounts}</td>
              <td className="py-2 pr-3 tabular-nums">{o.overrides}</td>
              <td className="py-2 pr-3">
                {o.billing_ready ? (
                  <Tag tone="ok">GSTIN + state</Tag>
                ) : (
                  <Tag tone="warn">{!o.state_code ? "no state code" : "no GSTIN"}</Tag>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default async function OrganisationsPage() {
  const op = await requireOperator();
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Organisations</h1>
      <Card
        title="Every customer"
        hint="Status is the operator's; the plan and its entitlements are read from the same functions the tenant's own plan page reads."
        action={
          <Link href="/organisations/new" className="text-sm underline underline-offset-2">
            New organisation
          </Link>
        }
      >
        <Suspense fallback={<Skeleton rows={4} />}>
          <Table op={op} />
        </Suspense>
      </Card>
    </div>
  );
}
