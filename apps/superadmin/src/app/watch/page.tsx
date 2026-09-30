import { Suspense } from "react";

import { acknowledgeFinding, reverifyKnowledge, reverifyRule } from "@/app/actions";
import { api, type Finding } from "@/lib/api";
import { requireOperator, type Operator } from "@/lib/session";
import { Card, Problem, Skeleton, Tag, button, buttonQuiet, when } from "@/components/ui";

export const metadata = { title: "Platform Watch" };

/**
 * The inbox Platform Watch writes to, and the two acts that answer it.
 *
 * Platform Watch DETECTS and never acts: a source page that changed, a rule
 * or a knowledge row past its freshness window, a page it could not fetch.
 * Each is a finding for a person. The person can:
 *
 *   ACKNOWLEDGE - "I have seen this." The finding closes; if the condition
 *   still holds tomorrow, a new one opens. Right for a source_changed on a
 *   page that turned out to be dynamic, or a fetch_failed that recovered.
 *
 *   RE-VERIFY - "I have re-read the source and the rule still holds." Writes
 *   as_of, the ONE column on a rule a session may write, and closes the
 *   stale finding in the same transaction. The pattern is not touched; if
 *   the source changed and the rule must change with it, that is a migration.
 */

const SEVERITY_TONE: Record<Finding["severity"], "bad" | "warn" | "muted"> = {
  urgent: "bad",
  review: "warn",
  info: "muted",
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

function Detail({ f }: { f: Finding }) {
  const d = f.detail;
  switch (f.kind) {
    case "rule_stale":
      return (
        <>
          Rule <span className="font-mono">{f.subject}</span> ({String(d.jurisdiction)}, {String(d.rule_severity)}) is{" "}
          {String(d.days_old)} days old against a {String(d.window_days)}-day window; as_of {String(d.as_of)}.
        </>
      );
    case "knowledge_stale":
      return (
        <>
          Knowledge &ldquo;{String(d.topic)}&rdquo; is {String(d.days_old)} days old against a {String(d.window_days)}-day
          window; as_of {String(d.as_of)}.
        </>
      );
    case "source_changed":
      return <>The page&rsquo;s text changed since it was last read{d.previous_hash ? " (content hash moved)" : ""}.</>;
    case "fetch_failed":
      return <>Could not fetch: {String(d.error ?? d.status ?? "unknown")}.</>;
  }
}

async function Inbox({ op }: { op: Operator }) {
  let findings;
  try {
    findings = await api.findings(op);
  } catch (e) {
    return <Problem error={e} />;
  }
  if (findings.length === 0) return <p className="text-sm text-slate-500">Nothing open. Platform Watch runs at 05:00 IST.</p>;

  return (
    <ul className="space-y-2">
      {findings.map((f) => (
        <li key={f.id} className="rounded-md border border-slate-200 p-3 text-sm dark:border-slate-800">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <p className="font-medium">
              <Tag tone={SEVERITY_TONE[f.severity]}>{f.severity}</Tag>{" "}
              <span className="ml-1">{f.kind.replace("_", " ")}</span>
              <span className="ml-2 font-mono text-xs text-slate-500">{f.subject}</span>
            </p>
            <span className="text-xs text-slate-500">detected {when(f.detected_at)}</span>
          </div>
          <p className="mt-1 text-slate-700 dark:text-slate-300">
            <Detail f={f} />
          </p>
          {f.source_url && (
            <p className="mt-1 text-xs">
              <a href={f.source_url} target="_blank" rel="noopener noreferrer" className="underline underline-offset-2">
                {f.source_url}
              </a>
            </p>
          )}
          <div className="mt-2 flex flex-wrap gap-2">
            {f.kind === "rule_stale" && (
              <form action={reverifyRule}>
                <input type="hidden" name="code" value={f.subject} />
                <button type="submit" className={button} title="I have re-read the source today and the rule still holds">
                  Re-verified today
                </button>
              </form>
            )}
            {f.kind === "knowledge_stale" && (
              <form action={reverifyKnowledge}>
                <input type="hidden" name="knowledge_id" value={f.subject} />
                <button type="submit" className={button}>
                  Re-verified today
                </button>
              </form>
            )}
            <form action={acknowledgeFinding}>
              <input type="hidden" name="finding_id" value={f.id} />
              <button type="submit" className={buttonQuiet}>
                Acknowledge
              </button>
            </form>
          </div>
        </li>
      ))}
    </ul>
  );
}

async function Rules({ op }: { op: Operator }) {
  let body;
  try {
    body = await api.rules(op);
  } catch (e) {
    return <Problem error={e} />;
  }
  const stale = body.rules.filter((r) => r.stale).length;
  return (
    <div className="space-y-4">
      <p className="text-sm text-slate-600 dark:text-slate-400">
        {body.rules.length} rules, {stale} past their window · {body.knowledge.length} knowledge rows,{" "}
        {body.knowledge.filter((k) => k.stale).length} past theirs. Meta rules age on a 90-day window; Indian statute on 365.
      </p>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-left text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="py-2 pr-3">Code</th>
              <th className="py-2 pr-3">Instrument</th>
              <th className="py-2 pr-3">Severity</th>
              <th className="py-2 pr-3">As of</th>
              <th className="py-2 pr-3">Age</th>
              <th className="py-2 pr-3"></th>
            </tr>
          </thead>
          <tbody>
            {body.rules.map((r) => (
              <tr key={r.code} className="border-t border-slate-200 dark:border-slate-800">
                <td className="py-2 pr-3">
                  <p className="font-mono text-xs font-medium">{r.code}</p>
                  <p className="max-w-md text-xs text-slate-500">{r.title}</p>
                </td>
                <td className="py-2 pr-3 text-xs">
                  {r.instrument}
                  <br />
                  <a href={r.source_url} target="_blank" rel="noopener noreferrer" className="text-slate-500 underline underline-offset-2">
                    source
                  </a>
                </td>
                <td className="py-2 pr-3">
                  <Tag tone={r.severity === "block" ? "bad" : "muted"}>{r.severity}</Tag>
                  {!r.is_active && <Tag tone="muted">inactive</Tag>}
                </td>
                <td className="py-2 pr-3 tabular-nums">{r.as_of}</td>
                <td className="py-2 pr-3 tabular-nums">
                  {r.stale ? <Tag tone="warn">{r.days_old} / {r.window_days} d</Tag> : `${r.days_old} / ${r.window_days} d`}
                </td>
                <td className="py-2 pr-3">
                  <form action={reverifyRule}>
                    <input type="hidden" name="code" value={r.code} />
                    <button type="submit" className={buttonQuiet}>
                      Re-verified today
                    </button>
                  </form>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {body.knowledge.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="py-2 pr-3">Knowledge</th>
                <th className="py-2 pr-3">As of</th>
                <th className="py-2 pr-3">Age</th>
                <th className="py-2 pr-3"></th>
              </tr>
            </thead>
            <tbody>
              {body.knowledge.map((k) => (
                <tr key={k.id} className="border-t border-slate-200 dark:border-slate-800">
                  <td className="py-2 pr-3">
                    <p className="font-medium">{k.topic}</p>
                    <p className="max-w-lg text-xs text-slate-500">{k.statement}</p>
                  </td>
                  <td className="py-2 pr-3 tabular-nums">{k.as_of}</td>
                  <td className="py-2 pr-3 tabular-nums">
                    {k.stale ? <Tag tone="warn">{k.days_old} / {k.window_days} d</Tag> : `${k.days_old} / ${k.window_days} d`}
                  </td>
                  <td className="py-2 pr-3">
                    <form action={reverifyKnowledge}>
                      <input type="hidden" name="knowledge_id" value={k.id} />
                      <button type="submit" className={buttonQuiet}>
                        Re-verified today
                      </button>
                    </form>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default async function WatchPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string; ok?: string }>;
}) {
  const op = await requireOperator();
  const { error, ok } = await searchParams;
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Platform Watch</h1>
      <Flash error={error} ok={ok} />
      <Card
        title="Open findings"
        hint="Detected, never acted on. “Re-verified today” means you re-read the source and the rule still holds; it writes the rule’s date and closes the finding. It never changes the rule."
      >
        <Suspense fallback={<Skeleton rows={3} />}>
          <Inbox op={op} />
        </Suspense>
      </Card>
      <Card title="Every rule and its age" hint="What the nightly job compares. The pattern itself is not shown and not editable from here.">
        <Suspense fallback={<Skeleton rows={5} />}>
          <Rules op={op} />
        </Suspense>
      </Card>
    </div>
  );
}
