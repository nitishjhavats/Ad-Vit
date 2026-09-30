import { Suspense } from "react";

import { api } from "@/lib/api";
import { requireOperator, type Operator } from "@/lib/session";
import { Card, Chip, Problem, Skeleton, Stat, when } from "@/components/ui";

/**
 * The first screen says what needs a person. Every number comes from one
 * query on the operator's own session, and each block links to the page
 * where the thing is dealt with.
 */

async function Counts({ op }: { op: Operator }) {
  let o;
  try {
    o = await api.overview(op);
  } catch (e) {
    return <Problem error={e} />;
  }
  const urgent = o.open_findings_by_severity.urgent ?? 0;
  const review = o.open_findings_by_severity.review ?? 0;
  const info = o.open_findings_by_severity.info ?? 0;
  const orgs = o.organisations_by_status;

  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
      <Stat
        label="Organisations"
        value={Object.values(orgs).reduce((a, b) => a + b, 0)}
        hint={Object.entries(orgs)
          .map(([k, v]) => `${v} ${k.replace("_", " ")}`)
          .join(" · ")}
      />
      <Stat
        label="Platform Watch"
        value={urgent + review + info}
        hint={`${urgent} urgent · ${review} to review · ${info} info`}
      />
      <Stat label="Invoices" value={o.draft_invoices} hint={`drafts · ${o.unpaid_invoices} issued and unpaid`} />
      {/* A submitted payment is a tenant waiting on a person: nothing confirms
          money but an operator on the Payments page, so this one is a link. */}
      <a href="/payments" className="block rounded-md hover:bg-slate-50 dark:hover:bg-slate-900">
        <Stat label="Payments" value={o.payments_submitted} hint="awaiting review · open the queue" />
      </a>
      <Stat
        label="Subscriptions"
        value={o.subscriptions_needing_attention}
        hint={`needing attention · ${o.live_coupons} live coupon(s)`}
      />
    </div>
  );
}

async function Urgent({ op }: { op: Operator }) {
  let findings;
  try {
    findings = await api.findings(op);
  } catch (e) {
    return <Problem error={e} />;
  }
  const urgent = findings.filter((f) => f.severity === "urgent").slice(0, 8);
  return (
    <Card
      title="Urgent findings"
      hint="A stale BLOCK rule is a statutory check running on an unverified reading. Re-verify or acknowledge on the Platform Watch page."
      action={
        <a href="/watch" className="text-sm underline underline-offset-2">
          Open the inbox
        </a>
      }
    >
      {urgent.length === 0 ? (
        <p className="text-sm text-slate-500">Nothing urgent is open.</p>
      ) : (
        <div className="grid gap-2 sm:grid-cols-2">
          {urgent.map((f) => (
            <Chip
              key={f.id}
              tone="bad"
              label={`${f.kind.replace("_", " ")} · ${f.subject}`}
              detail={`detected ${when(f.detected_at)}${
                typeof f.detail.days_old === "number" ? ` · ${f.detail.days_old} days old` : ""
              }`}
            />
          ))}
        </div>
      )}
    </Card>
  );
}

async function Drafts({ op }: { op: Operator }) {
  let drafts;
  try {
    drafts = await api.invoices(op, "draft");
  } catch (e) {
    return <Problem error={e} />;
  }
  return (
    <Card
      title="Invoices that could not be issued"
      hint="A draft is an invoice with no number because the GST document would be wrong. Each says why."
      action={
        <a href="/invoices" className="text-sm underline underline-offset-2">
          All invoices
        </a>
      }
    >
      {drafts.length === 0 ? (
        <p className="text-sm text-slate-500">No drafts. Every due invoice was issued.</p>
      ) : (
        <ul className="space-y-2 text-sm">
          {drafts.slice(0, 8).map((i) => (
            <li key={i.id} className="flex flex-wrap items-baseline justify-between gap-2 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 dark:border-amber-900 dark:bg-amber-950/40">
              <span>
                <a href={`/organisations/${i.org_id}`} className="font-medium underline underline-offset-2">
                  {i.org_name}
                </a>{" "}
                · {i.period_start} → {i.period_end}
              </span>
              <span className="text-xs text-slate-600 dark:text-slate-400">{i.draft_reason}</span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

export default async function OverviewPage() {
  const op = await requireOperator();
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Overview</h1>
        <p className="mt-1 text-sm text-slate-500">
          Signed in as {op.email}. Every act here is written to the trail under your name.
        </p>
      </div>
      <Suspense fallback={<Skeleton />}>
        <Counts op={op} />
      </Suspense>
      <Suspense fallback={<Skeleton rows={3} />}>
        <Urgent op={op} />
      </Suspense>
      <Suspense fallback={<Skeleton rows={3} />}>
        <Drafts op={op} />
      </Suspense>
    </div>
  );
}
