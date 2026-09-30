import { Suspense } from "react";

import { approveSuggestion, rejectSuggestion } from "@/app/reports/actions";
import {
  Card,
  Flash,
  HeldProposal,
  PeriodPicker,
  Problem,
  Skeleton,
  TABS,
  Tabs,
  Trend,
  Unknown,
  Value,
  fmt,
  marginCaveat,
  why,
  type Tab,
} from "@/app/reports/components";
import { reports, type Learning, type Outcome, type Report } from "@/lib/reports";
import { defaultWorkspace, type AuthorizedWorkspace } from "@/lib/session";

export const metadata = { title: "Reports" };

/**
 * Report, Analytics, Suggestions: the three tabs the owner asked for, over one
 * read of `/api/workspaces/{id}/reports`.
 *
 * Report is the KPI band, money row first (PRD 5.3): what was spent, what was
 * delivered, what each delivered order cost, and what was left - with the
 * week-over-week arrows and the learnings the account has written about
 * itself. Analytics is the daily table and the measured outcomes with their
 * verdicts. Suggestions is the approvals inbox with Approve and Reject, plus
 * the question the CTA gate is holding a proposal behind, read from its own
 * row so it survives the chat turn that raised it.
 *
 * The tab and the period live in the URL and the links are server-rendered.
 * There is no client state to lose on a refresh, and nothing here ships a
 * browser bundle that could hold a token.
 */

const DEFAULT_DAYS = 30;

function pickTab(raw: string | undefined): Tab {
  return (TABS as readonly string[]).includes(raw ?? "") ? (raw as Tab) : "report";
}

function pickDays(raw: string | undefined): number {
  const n = Number(raw);
  // The runtime clamps to 7..90 as well; this only keeps the links honest.
  return Number.isInteger(n) && n >= 7 && n <= 90 ? n : DEFAULT_DAYS;
}

// ---------------------------------------------------------------------------
// Report
// ---------------------------------------------------------------------------

function Kpi({
  label,
  text,
  why,
  caveat,
  trend,
  large,
}: {
  label: string;
  text: string | null;
  why: string;
  caveat?: string;
  trend?: React.ReactNode;
  large?: boolean;
}) {
  return (
    <div className="rounded-md border border-slate-200 p-3 dark:border-slate-800">
      <p className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">{label}</p>
      <p className={`mt-1 font-semibold ${large ? "text-2xl" : "text-lg"}`} title={caveat}>
        <Value text={text} why={why} />
        {caveat && text !== null && (
          <span className="ml-1 align-middle text-xs font-normal text-amber-700 dark:text-amber-300">
            upper bound
          </span>
        )}
      </p>
      {trend && <p className="mt-1">{trend}</p>}
    </div>
  );
}

function Confidence({ value }: { value: Learning["confidence"] }) {
  const pct = fmt.pct(value);
  return <span className="tabular-nums">{pct ?? "confidence unknown"}</span>;
}

function LearningsList({ report }: { report: Report }) {
  const own = report.learnings.filter((l) => l.tier === "account");
  const shared = report.learnings.filter((l) => l.tier !== "account");
  return (
    <div className="space-y-4">
      {own.length === 0 ? (
        <p className="text-sm text-slate-500 dark:text-slate-400">
          Nothing learned yet. A learning needs at least two measured outcomes that agree about
          the same kind of decision; the account has not accumulated that.
        </p>
      ) : (
        <ul className="space-y-2">
          {own.map((l) => (
            <li key={l.id} className="rounded-md border border-slate-200 p-3 text-sm dark:border-slate-800">
              <p>{l.statement}</p>
              <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                <Confidence value={l.confidence} /> confidence · {l.evidence_n} measured outcome
                {l.evidence_n === 1 ? "" : "s"} ·{" "}
                {l.status === "contested" ? "contested - the evidence disagrees" : "active"} · updated{" "}
                {new Date(l.updated).toLocaleDateString("en-IN", { day: "numeric", month: "short" })}
              </p>
            </li>
          ))}
        </ul>
      )}

      <div>
        <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
          Shared learnings
        </h3>
        {!report.entitlements.industry_intelligence ? (
          <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
            Industry intelligence is a Growth feature. On this plan the account learns from itself
            only.
          </p>
        ) : shared.length === 0 ? (
          <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
            No shared learning has cleared the independence gate yet (three workspaces, two owners,
            an operator&rsquo;s approval).
          </p>
        ) : (
          <ul className="mt-2 space-y-2">
            {shared.map((l) => (
              <li key={l.id} className="rounded-md border border-slate-200 p-3 text-sm dark:border-slate-800">
                <p>{l.statement}</p>
                <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                  {l.tier} tier · <Confidence value={l.confidence} /> confidence · {l.evidence_n}{" "}
                  measured outcomes
                </p>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function ReportTab({ report }: { report: Report }) {
  const t = report.totals;
  const w = report.week_over_week;
  const c = t.coverage;
  // Totals have no per-day gaps; the reason for an unknown total is the
  // coverage count that is zero.
  const noTruth = `Nothing was reported for ${c.reported_days === 0 ? "any" : "enough"} of the ${c.period_days} days. Unknown, not zero.`;
  const noSpend = `No spend was ingested for ${c.spend_days === 0 ? "any" : "enough"} of the ${c.period_days} days. Unknown, not zero.`;
  const marginRows = report.money.filter((r) => r.contribution_margin_inr !== null);
  const marginIsBound = marginRows.length > 0 && marginRows.every((r) => marginCaveat(r) !== undefined);

  return (
    <div className="space-y-6">
      <Card
        title="Money"
        hint={`${fmt.date(report.period.from)} – ${fmt.date(report.period.to)} · business truth reported on ${c.reported_days} of ${c.period_days} days, spend ingested on ${c.spend_days}.`}
      >
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
          <Kpi label="Spend" text={fmt.inr(t.spend_inr)} why={noSpend} trend={<Trend c={w.spend_inr} />} large />
          <Kpi label="Delivered revenue" text={fmt.inr(t.delivered_revenue_inr)} why={noTruth} large />
          <Kpi
            label="Blended CAC"
            text={fmt.inrPrecise(t.blended_cac_inr)}
            why={t.spend_inr === null ? noSpend : `Blended CAC needs delivered orders; ${noTruth}`}
            trend={<Trend c={w.blended_cac_inr} />}
            large
          />
          <Kpi
            label="MER"
            text={fmt.ratio(t.mer)}
            why={t.spend_inr === null ? noSpend : `MER needs delivered revenue; ${noTruth}`}
            trend={<Trend c={w.mer} />}
            large
          />
          <Kpi
            label="Contribution margin (period)"
            text={fmt.inr(t.contribution_margin_inr)}
            why={
              c.margin_days === 0
                ? "No day in the period has a computable margin: it needs a product margin rate, ingested spend and delivered orders together. Unknown, not zero."
                : noTruth
            }
            caveat={
              marginIsBound
                ? "Upper bound: fulfilment cost or return freight is not on file, so the true margin is lower."
                : c.margin_days < c.reported_days
                  ? `Summed over the ${c.margin_days} days it could be computed, not the whole period.`
                  : undefined
            }
            large
          />
        </div>
        <div className="mt-3 grid gap-3 sm:grid-cols-3">
          <Kpi label="RTO rate" text={fmt.pct(t.rto_rate)} why={`RTO orders were not reported. ${noTruth}`} />
          <Kpi label="Confirm rate" text={fmt.pct(t.confirm_rate)} why={`Confirmed and total orders were not both reported. ${noTruth}`} />
          <Kpi label="Delivered orders" text={fmt.count(t.delivered_orders)} why={noTruth} />
        </div>
        <p className="mt-3 text-xs text-slate-500 dark:text-slate-400">
          Week over week compares {fmt.date(w.windows.this.from)}–{fmt.date(w.windows.this.to)} with{" "}
          {fmt.date(w.windows.previous.from)}–{fmt.date(w.windows.previous.to)}. {report.note}
        </p>
      </Card>

      <Card
        title="What this account has learned"
        hint="Written from measured outcomes by the learning loop, never by a model. Confidence is Laplace-smoothed, so two for two reads 75%, not 100%."
      >
        <LearningsList report={report} />
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Analytics
// ---------------------------------------------------------------------------

const VERDICT: Record<Outcome["verdict"], { mark: string; label: string; tone: string }> = {
  beat: { mark: "●", label: "beat", tone: "border-emerald-300 bg-emerald-50 dark:border-emerald-900 dark:bg-emerald-950/40" },
  met: { mark: "●", label: "met", tone: "border-emerald-200 bg-white dark:border-emerald-900 dark:bg-slate-900" },
  missed: { mark: "▲", label: "missed", tone: "border-red-300 bg-red-50 dark:border-red-900 dark:bg-red-950/40" },
  unmeasurable: { mark: "○", label: "unmeasurable", tone: "border-slate-200 bg-slate-50 dark:border-slate-800 dark:bg-slate-900" },
};

function OutcomesList({ outcomes }: { outcomes: Outcome[] }) {
  if (outcomes.length === 0) {
    return (
      <p className="text-sm text-slate-500 dark:text-slate-400">
        No decision has reached its horizon and been measured yet.
      </p>
    );
  }
  return (
    <ul className="space-y-2">
      {outcomes.map((o) => {
        const v = VERDICT[o.verdict];
        const readings = o.before_value !== null && o.after_value !== null;
        return (
          <li key={o.id} className={`rounded-md border p-3 text-sm ${v.tone}`}>
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <span className="font-medium">
                {o.decision_type.replace(/_/g, " ")}
                {o.chosen_option && <span className="font-normal text-slate-500"> · {o.chosen_option}</span>}
              </span>
              <span className="text-xs uppercase tracking-wide">
                <span aria-hidden="true">{v.mark} </span>
                {v.label}
              </span>
            </div>
            <p className="mt-1 text-slate-700 dark:text-slate-300">
              {o.metric_label ?? o.metric ?? "no metric"}
              {o.predicted_direction && <> predicted {o.predicted_direction}</>}
              {readings ? (
                <>
                  : <span className="tabular-nums">{fmt.ratio(o.before_value)}</span> →{" "}
                  <span className="tabular-nums">{fmt.ratio(o.after_value)}</span>
                  {o.target !== null && <> (target {fmt.ratio(o.target)})</>}
                </>
              ) : (
                <>
                  : <Unknown why={o.notes ?? "No before-and-after reading was possible."} /> before and
                  after
                </>
              )}
            </p>
            <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
              measured over {o.horizon_days} days, on{" "}
              {new Date(o.measured_at).toLocaleDateString("en-IN", { day: "numeric", month: "short" })}
              {o.notes && <> · {o.notes}</>}
            </p>
          </li>
        );
      })}
    </ul>
  );
}

const COLUMNS = [
  ["spend_inr", "Spend", "inr"],
  ["delivered_revenue_inr", "Delivered revenue", "inr"],
  ["delivered_orders", "Delivered", "count"],
  ["blended_cac_inr", "Blended CAC", "inrPrecise"],
  ["mer", "MER", "ratio"],
  // Per delivered order on each day (t_advit.contribution_margin_per_delivered_order);
  // the period figure in the KPI band is this times the day's delivered orders.
  ["contribution_margin_inr", "Margin / delivered order", "inr"],
  ["rto_rate", "RTO", "pct"],
  ["confirm_rate", "Confirm", "pct"],
] as const;

function AnalyticsTab({ report }: { report: Report }) {
  return (
    <div className="space-y-6">
      <Card
        title="Daily"
        hint="Every row is a day the owner reported. A dash is an unknown - hover it for which input was missing - and is never a zero."
      >
        {report.money.length === 0 ? (
          <p className="text-sm text-slate-500 dark:text-slate-400">
            No day in this period has been reported. The series has gaps, not zeros, so an empty
            table means the evening report was not sent - not that nothing was sold.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
                  <th scope="col" className="py-2 pr-3 font-medium">Date</th>
                  {COLUMNS.map(([key, label]) => (
                    <th key={key} scope="col" className="py-2 pr-3 text-right font-medium">
                      {label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {report.money.map((row) => (
                  <tr key={row.date} className="border-t border-slate-100 dark:border-slate-800">
                    <th scope="row" className="py-2 pr-3 text-left font-normal">
                      {fmt.date(row.date)}
                    </th>
                    {COLUMNS.map(([key, , kind]) => (
                      <td
                        key={key}
                        className="py-2 pr-3 text-right"
                        title={key === "contribution_margin_inr" ? marginCaveat(row) : undefined}
                      >
                        <Value text={fmt[kind](row[key])} why={why(key, row)} />
                        {key === "contribution_margin_inr" &&
                          row.contribution_margin_inr !== null &&
                          marginCaveat(row) && <span aria-hidden="true">*</span>}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
            {report.money.some((r) => r.contribution_margin_inr !== null && marginCaveat(r)) && (
              <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
                * an upper bound: fulfilment cost or return freight is not on file for every
                product, so the true margin is lower.
              </p>
            )}
          </div>
        )}
      </Card>

      <Card
        title="Measured outcomes"
        hint="Each decision pre-registered a prediction and a horizon. This is what happened at the horizon, compared with the equal window before it."
      >
        <OutcomesList outcomes={report.outcomes} />
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Suggestions
// ---------------------------------------------------------------------------

function SuggestionsTab({ report }: { report: Report }) {
  const pending = report.suggestions.pending_approvals;
  const gate = report.suggestions.cta_gate;
  const button =
    "rounded-md px-3 py-1.5 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50";

  return (
    <div className="space-y-6">
      <Card
        title="Waiting on you"
        hint="The same rows as the approvals inbox. Approving executes the proposal through the policy layer, with the guardrails re-checked against today's spend."
      >
        {pending.length === 0 ? (
          <p className="text-sm text-slate-500 dark:text-slate-400">Nothing waiting on you.</p>
        ) : (
          <ul className="space-y-3">
            {pending.map((a) => (
              <li key={a.id} className="rounded-md border border-slate-200 p-4 text-sm dark:border-slate-800">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className="font-medium">{a.decision_type.replace(/_/g, " ")}</span>
                  <span className="text-xs uppercase tracking-wide text-slate-500">
                    {a.risk_class} risk
                    {a.confidence !== null && <> · {fmt.pct(a.confidence)} confidence</>}
                  </span>
                </div>
                {a.reasoning && <p className="mt-1 text-slate-700 dark:text-slate-300">{a.reasoning}</p>}
                <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                  {a.impact_inr !== null && <>{fmt.inr(a.impact_inr)} impact · </>}
                  measured at {a.horizon_days} days ·{" "}
                  {a.expired
                    ? "expired - it will be re-derived from today's numbers rather than executed"
                    : `expires ${new Date(a.expires_at).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}`}
                </p>

                <div className="mt-3 flex flex-wrap items-start gap-3">
                  <form action={approveSuggestion}>
                    <input type="hidden" name="approval_id" value={a.id} />
                    <button
                      type="submit"
                      disabled={a.expired}
                      className={`${button} bg-slate-900 text-white hover:bg-slate-700 dark:bg-slate-100 dark:text-slate-900`}
                    >
                      Approve and execute
                    </button>
                  </form>
                  <form action={rejectSuggestion} className="flex flex-1 flex-wrap gap-2">
                    <input type="hidden" name="approval_id" value={a.id} />
                    <input
                      name="reason"
                      required
                      placeholder="Why not? Stored as a training signal."
                      aria-label="Rejection reason"
                      className="min-w-48 flex-1 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-900"
                    />
                    <button
                      type="submit"
                      className={`${button} border border-slate-300 hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800`}
                    >
                      Reject
                    </button>
                  </form>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card
        title="Held at the CTA gate"
        hint="A proposal that would build something is held until you have said where your campaigns send people. Nothing here can be approved: it was proposed without a destination, and answering re-proposes it with one."
      >
        <HeldProposal gate={gate} />
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------------
// The page
// ---------------------------------------------------------------------------

async function Body({ ws, tab, days }: { ws: AuthorizedWorkspace; tab: Tab; days: number }) {
  let report: Report;
  try {
    report = await reports.get(ws, days);
  } catch (e) {
    return <Problem error={e} />;
  }
  switch (tab) {
    case "report":
      return <ReportTab report={report} />;
    case "analytics":
      return <AnalyticsTab report={report} />;
    case "suggestions":
      return <SuggestionsTab report={report} />;
  }
}

export default async function ReportsPage({
  searchParams,
}: {
  searchParams: Promise<{ tab?: string; days?: string; error?: string; ok?: string }>;
}) {
  const params = await searchParams;
  const tab = pickTab(params.tab);
  const days = pickDays(params.days);
  // Redirects to /login without a session, so everything below has a principal.
  const ws = await defaultWorkspace();

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Reports</h1>
          <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
            Money first, then what the account learned, then what it proposes.
          </p>
        </div>
        <PeriodPicker active={days} tab={tab} />
      </div>

      <Tabs active={tab} days={days} />

      <Flash error={params.error} ok={params.ok} />

      {ws ? (
        // Keyed on tab and period so a change remounts the boundary and shows
        // the fallback, rather than holding the previous tab on screen.
        <Suspense key={`${tab}-${days}`} fallback={<Skeleton rows={4} />}>
          <Body ws={ws} tab={tab} days={days} />
        </Suspense>
      ) : (
        <div className="rounded-md border border-slate-200 bg-white p-5 text-sm dark:border-slate-800 dark:bg-slate-900">
          <p className="font-medium">No workspace yet</p>
          <p className="mt-1 text-slate-600 dark:text-slate-400">
            This account is signed in but is not a member of any workspace. An owner or admin of
            the organisation can add you.
          </p>
        </div>
      )}
    </div>
  );
}
