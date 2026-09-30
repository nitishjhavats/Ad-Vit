import { Suspense } from "react";

import { api } from "@/lib/api";
import { requireOperator, type Operator } from "@/lib/session";
import { Card, Problem, Skeleton, Tag, input, buttonQuiet, when } from "@/components/ui";

export const metadata = { title: "Trail" };

/**
 * core.audit_log, newest first. Append-only, written by core.log_audit and by
 * nothing else; the actor is stamped from the session, and a superadmin who
 * tried to describe themselves differently is overruled by the forgery guard.
 * This page filters; it cannot write.
 */

const ACTOR_TONE: Record<string, "ok" | "warn" | "bad" | "muted"> = {
  superadmin: "warn",
  user: "muted",
  agent: "ok",
  automation: "ok",
  system: "muted",
};

async function Rows({ op, org, prefix }: { op: Operator; org?: string; prefix?: string }) {
  let rows;
  try {
    rows = await api.audit(op, { org_id: org, event_prefix: prefix, limit: 200 });
  } catch (e) {
    return <Problem error={e} />;
  }
  if (rows.length === 0) return <p className="text-sm text-slate-500">Nothing matches.</p>;
  return (
    <ul className="space-y-1">
      {rows.map((r) => (
        <li key={r.id} className="rounded-md border border-slate-200 px-3 py-2 text-sm dark:border-slate-800">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <p>
              <span className="font-mono text-xs font-medium">{r.event}</span>
              <Tag tone={ACTOR_TONE[r.actor_type] ?? "muted"}>{r.actor_type}</Tag>
              {r.actor_email && <span className="ml-1 text-xs text-slate-500">{r.actor_email}</span>}
              {r.impersonated_by && <Tag tone="bad">impersonated</Tag>}
              {r.org_name && (
                <a href={`/organisations/${r.org_id}`} className="ml-2 text-xs underline underline-offset-2">
                  {r.org_name}
                </a>
              )}
            </p>
            <span className="text-xs text-slate-500">
              #{r.id} · {r.scope} · {when(r.at)}
            </span>
          </div>
          {Object.keys(r.payload).length > 0 && (
            <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-all text-xs text-slate-600 dark:text-slate-400">
              {JSON.stringify(r.payload)}
            </pre>
          )}
        </li>
      ))}
    </ul>
  );
}

export default async function AuditPage({
  searchParams,
}: {
  searchParams: Promise<{ org?: string; prefix?: string }>;
}) {
  const op = await requireOperator();
  const { org, prefix } = await searchParams;
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Trail</h1>
      <Card title="core.audit_log" hint="Append-only. Who did this, and under whose authority.">
        <form method="get" className="mb-4 flex flex-wrap items-end gap-2">
          <label className="text-sm">
            <span className="text-slate-600 dark:text-slate-400">Organisation id</span>
            <input name="org" defaultValue={org ?? ""} placeholder="uuid" className={`${input} w-80 font-mono text-xs`} />
          </label>
          <label className="text-sm">
            <span className="text-slate-600 dark:text-slate-400">Event prefix</span>
            <input name="prefix" defaultValue={prefix ?? ""} placeholder="organisation. / coupon. / rule." className={`${input} w-64`} />
          </label>
          <button type="submit" className={buttonQuiet}>
            Filter
          </button>
        </form>
        <Suspense fallback={<Skeleton rows={6} />}>
          <Rows op={op} org={org || undefined} prefix={prefix || undefined} />
        </Suspense>
      </Card>
    </div>
  );
}
