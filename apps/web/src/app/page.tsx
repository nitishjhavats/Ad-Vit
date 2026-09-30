import { Suspense } from "react";
import { api, RuntimeUnreachable, type AuditReport } from "@/lib/api";
import { defaultWorkspace, type AuthorizedWorkspace } from "@/lib/session";

/**
 * The dashboard.
 *
 * Next.js 16 does not cache `fetch` by default and removed the `dynamic` route
 * segment config, so freshness needs no opt-out. What it does need is a
 * <Suspense> boundary around every runtime read: the fallback ships inside the
 * prerendered shell and the data streams in at request time.
 *
 * That is not a detail here. The PRD targets first contentful paint under 1.5s
 * on a mid-range Android over 4G, and the owner opens this on a phone. Blocking
 * the whole page on three sequential API calls would miss that badly.
 */

const SEVERITY_STYLE: Record<string, string> = {
  blocking: "border-red-300 bg-red-50 dark:border-red-900 dark:bg-red-950/40",
  high: "border-amber-300 bg-amber-50 dark:border-amber-900 dark:bg-amber-950/40",
  medium: "border-slate-300 bg-slate-50 dark:border-slate-700 dark:bg-slate-900",
  low: "border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900",
  info: "border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900",
};

function Card({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900">
      <div className="mb-4">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
          {title}
        </h2>
        {hint && <p className="mt-1 text-xs text-slate-500 dark:text-slate-500">{hint}</p>}
      </div>
      {children}
    </section>
  );
}

function Skeleton({ rows = 2 }: { rows?: number }) {
  return (
    <div className="space-y-2" aria-hidden="true">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="h-12 animate-pulse rounded-md bg-slate-100 dark:bg-slate-800" />
      ))}
    </div>
  );
}

function Unreachable({ error }: { error: unknown }) {
  const message =
    error instanceof RuntimeUnreachable
      ? "The agent runtime is not running. Start it with: uvicorn app.main:app --port 8000"
      : String(error);
  return (
    <div className="rounded-md border border-amber-300 bg-amber-50 p-4 text-sm dark:border-amber-900 dark:bg-amber-950/40">
      <p className="font-medium">Cannot reach the agent runtime</p>
      <p className="mt-1 text-slate-700 dark:text-slate-300">{message}</p>
    </div>
  );
}

/**
 * Status chips are diagnostic, not decorative (PRD 16.2): each states what is
 * wrong. State is never encoded in colour alone - every chip carries a symbol
 * and a text label too (WCAG AA, PRD 16.5).
 */
function Chip({ ok, label, detail }: { ok: boolean; label: string; detail: string }) {
  return (
    <div
      className={`rounded-md border px-3 py-2 text-sm ${
        ok
          ? "border-emerald-300 bg-emerald-50 dark:border-emerald-900 dark:bg-emerald-950/40"
          : "border-red-300 bg-red-50 dark:border-red-900 dark:bg-red-950/40"
      }`}
    >
      <p className="font-medium">
        <span aria-hidden="true">{ok ? "● " : "▲ "}</span>
        {label}
      </p>
      <p className="mt-0.5 text-xs text-slate-600 dark:text-slate-400">{detail}</p>
      <span className="sr-only">{ok ? "healthy" : "needs attention"}</span>
    </div>
  );
}

async function Connections({ ws }: { ws: AuthorizedWorkspace }) {
  let data;
  try {
    data = await api.connections(ws);
  } catch (e) {
    return <Unreachable error={e} />;
  }
  const { automation, workspace, meta_connections, model_access } = data;

  return (
    <>
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{workspace.workspace_name}</h1>
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
          {workspace.business_type} · caps ₹
          {Number(workspace.daily_cap_inr).toLocaleString("en-IN")}/day, ₹
          {Number(workspace.monthly_cap_inr).toLocaleString("en-IN")}/month
        </p>
      </div>

      <Card
        title="Connections"
        hint="Each chip states what is wrong and how to fix it, not merely that something is."
      >
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          <Chip
            ok={automation.access_mode === "full" && !automation.is_paused}
            label="Automation"
            detail={
              automation.is_paused
                ? "Frozen by the owner. Nothing executes."
                : `L${automation.autonomy_level} intent → L${automation.effective_autonomy} effective${
                    automation.capped_by_plan ? " (capped by plan)" : ""
                  }, access ${automation.access_mode}`
            }
          />
          <Chip
            ok={model_access.openrouter_key_present}
            label="Model access"
            detail={
              model_access.openrouter_key_present
                ? "Routing configured. Deterministic paths run without it."
                : "No key. Facts and compliance still work; narration does not."
            }
          />
          {meta_connections.map((c) => (
            <Chip
              key={c.ad_account_id}
              ok={c.measurement_ready}
              label={c.health_detail.account_name ?? c.ad_account_id}
              detail={
                c.measurement_ready
                  ? `${c.dataset_count} dataset(s) · ${c.write_enabled ? "writable" : "read-only"}`
                  : `No dataset. Offline conversions cannot run. ${
                      c.write_enabled ? "Writable" : "Read-only"
                    }.`
              }
            />
          ))}
        </div>
      </Card>
    </>
  );
}

async function Approvals({ ws }: { ws: AuthorizedWorkspace }) {
  let approvals;
  try {
    approvals = await api.approvals(ws);
  } catch (e) {
    return <Unreachable error={e} />;
  }

  return (
    <Card title="Approvals" hint="Nothing structural executes without one, at any autonomy level.">
      {approvals.length === 0 ? (
        <p className="text-sm text-slate-500 dark:text-slate-400">Nothing waiting on you.</p>
      ) : (
        <ul className="space-y-2">
          {approvals.map((a) => (
            <li
              key={a.id}
              className="rounded-md border border-slate-200 p-3 text-sm dark:border-slate-800"
            >
              <div className="flex items-baseline justify-between gap-3">
                <span className="font-medium">{a.decision_type}</span>
                <span className="text-xs uppercase tracking-wide text-slate-500">
                  {a.risk_class}
                </span>
              </div>
              {a.reasoning && (
                <p className="mt-1 text-slate-700 dark:text-slate-300">{a.reasoning}</p>
              )}
              <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                {a.impact_inr ? `₹${Number(a.impact_inr).toLocaleString("en-IN")} impact · ` : ""}
                measured at {a.horizon_days} days ·{" "}
                {a.expired ? "expired — will be re-derived" : "awaiting a decision"}
              </p>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function AuditFindings({ report }: { report: AuditReport }) {
  const blocking = report.findings.filter((f) => f.severity === "blocking");
  return (
    <div className="space-y-3">
      <div className="flex items-baseline gap-3">
        <span className="text-3xl font-semibold tabular-nums">{report.score}</span>
        <span className="text-sm text-slate-500 dark:text-slate-400">/ 100</span>
        {blocking.length > 0 && (
          <span className="rounded bg-red-100 px-2 py-0.5 text-xs font-medium text-red-800 dark:bg-red-950 dark:text-red-200">
            {blocking.length} blocking
          </span>
        )}
      </div>
      <ul className="space-y-2">
        {report.findings.map((f) => (
          <li key={f.code} className={`rounded-md border p-3 text-sm ${SEVERITY_STYLE[f.severity]}`}>
            <p className="font-medium">
              <span className="mr-2 text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
                {f.severity}
              </span>
              {f.title}
            </p>
            <p className="mt-1 text-slate-700 dark:text-slate-300">{f.detail}</p>
            <p className="mt-2 text-slate-600 dark:text-slate-400">
              <span className="font-medium">Fix:</span> {f.remedy}
            </p>
          </li>
        ))}
      </ul>
    </div>
  );
}

async function Audits({ ws }: { ws: AuthorizedWorkspace }) {
  let reports: AuditReport[];
  try {
    const connections = await api.connections(ws);
    reports = await Promise.all(
      connections.meta_connections.map((c) => api.audit(c.ad_account_id)),
    );
  } catch (e) {
    return <Unreachable error={e} />;
  }

  return (
    <>
      {reports.map((report) => (
        <Card
          key={report.ad_account_id}
          title={`Account audit · ${report.ad_account_id}`}
          hint="Derived by reading the account, not by asking a model."
        >
          <AuditFindings report={report} />
        </Card>
      ))}
    </>
  );
}

export default async function DashboardPage() {
  // Resolved once, here, from the signed-in session - and awaited before the
  // Suspense boundaries rather than inside them, so an account with no
  // workspace gets one honest message instead of three identical errors.
  //
  // `defaultWorkspace()` redirects to /login when there is no session, so
  // everything below this line has a principal.
  const ws = await defaultWorkspace();

  if (!ws) {
    return (
      <div className="rounded-md border border-slate-200 bg-white p-5 text-sm dark:border-slate-800 dark:bg-slate-900">
        <p className="font-medium">No workspace yet</p>
        <p className="mt-1 text-slate-600 dark:text-slate-400">
          This account is signed in but is not a member of any workspace. An owner or
          admin of the organisation can add you.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <Suspense fallback={<Skeleton rows={3} />}>
        <Connections ws={ws} />
      </Suspense>
      <Suspense fallback={<Skeleton />}>
        <Approvals ws={ws} />
      </Suspense>
      <Suspense fallback={<Skeleton rows={4} />}>
        <Audits ws={ws} />
      </Suspense>
    </div>
  );
}
