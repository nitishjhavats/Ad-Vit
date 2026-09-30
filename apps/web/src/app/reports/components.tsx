import { NotAuthorized, Refused, RuntimeUnreachable } from "@/lib/api";
import type { Comparison, CtaGate, Money, MoneyRow } from "@/lib/reports";

/**
 * The pieces the three tabs share: the frame, the number formatting, and the
 * one component this page is really about - `Unknown`, which is what a null
 * renders as.
 *
 * A null in the report is a fact: the input was not there. Rendering it as 0
 * would print a CAC of ₹0 (acquisition looks free), an RTO of 0% (returns
 * look absent) or a margin of ₹0 (break-even), each of which invites exactly
 * the decision the number should have prevented. So every unknown is an em
 * dash with a `title` and an accessible label that say WHY it is unknown, in
 * the words of the gap the economics engine recorded.
 */

export const TABS = ["report", "analytics", "suggestions"] as const;
export type Tab = (typeof TABS)[number];

export function Card({
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

export function Skeleton({ rows = 2 }: { rows?: number }) {
  return (
    <div className="space-y-2" aria-hidden="true">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="h-12 animate-pulse rounded-md bg-slate-100 dark:bg-slate-800" />
      ))}
    </div>
  );
}

export function Problem({ error }: { error: unknown }) {
  let title = "Something went wrong";
  let message = String(error);
  if (error instanceof RuntimeUnreachable) {
    title = "Cannot reach the agent runtime";
    message = "Start it with: uvicorn app.main:app --port 8000";
  } else if (error instanceof NotAuthorized) {
    title = "Nothing here";
    message = "The runtime answered 404: this session is not a member of the workspace.";
  } else if (error instanceof Refused) {
    title = `Refused (${error.status})`;
    message = error.detail;
  }
  return (
    <div className="rounded-md border border-amber-300 bg-amber-50 p-4 text-sm dark:border-amber-900 dark:bg-amber-950/40">
      <p className="font-medium">{title}</p>
      <p className="mt-1 text-slate-700 dark:text-slate-300">{message}</p>
    </div>
  );
}

export function Flash({ error, ok }: { error?: string; ok?: string }) {
  if (error) {
    return (
      <p
        role="alert"
        className="rounded-md border border-red-300 bg-red-50 p-3 text-sm dark:border-red-900 dark:bg-red-950/40"
      >
        <span className="font-medium">Refused: </span>
        {error}
      </p>
    );
  }
  if (ok) {
    return (
      <p
        role="status"
        className="rounded-md border border-emerald-300 bg-emerald-50 p-3 text-sm dark:border-emerald-900 dark:bg-emerald-950/40"
      >
        {ok}
      </p>
    );
  }
  return null;
}

/**
 * Server-rendered links, not client state: the tab is in the URL, so a
 * refresh, a shared link and the back button all land where the owner was.
 */
export function Tabs({ active, days }: { active: Tab; days: number }) {
  const label: Record<Tab, string> = {
    report: "Report",
    analytics: "Analytics",
    suggestions: "Suggestions",
  };
  return (
    <nav aria-label="Report sections" className="flex gap-1 border-b border-slate-200 dark:border-slate-800">
      {TABS.map((tab) => {
        const current = tab === active;
        return (
          <a
            key={tab}
            href={`/reports?tab=${tab}&days=${days}`}
            aria-current={current ? "page" : undefined}
            className={`-mb-px border-b-2 px-3 py-2 text-sm ${
              current
                ? "border-slate-900 font-medium text-slate-900 dark:border-slate-100 dark:text-slate-100"
                : "border-transparent text-slate-500 hover:text-slate-900 dark:text-slate-400 dark:hover:text-slate-100"
            }`}
          >
            {label[tab]}
          </a>
        );
      })}
    </nav>
  );
}

export function PeriodPicker({ active, tab }: { active: number; tab: Tab }) {
  return (
    <div className="flex items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
      <span>Period</span>
      {[7, 14, 30, 60, 90].map((d) => (
        <a
          key={d}
          href={`/reports?tab=${tab}&days=${d}`}
          aria-current={d === active ? "true" : undefined}
          className={`rounded px-2 py-0.5 ${
            d === active
              ? "bg-slate-900 font-medium text-white dark:bg-slate-100 dark:text-slate-900"
              : "hover:text-slate-900 dark:hover:text-slate-100"
          }`}
        >
          {d}d
        </a>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Numbers
// ---------------------------------------------------------------------------

const INR = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 0,
});
const INR_PRECISE = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 2,
});
const PCT = new Intl.NumberFormat("en-IN", { style: "percent", maximumFractionDigits: 1 });
const RATIO = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 });
const COUNT = new Intl.NumberFormat("en-IN");

function num(value: Money): number | null {
  if (value === null || value === undefined) return null;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

function format(value: Money, f: Intl.NumberFormat): string | null {
  const n = num(value);
  return n === null ? null : f.format(n);
}

export const fmt = {
  inr: (v: Money) => format(v, INR),
  inrPrecise: (v: Money) => format(v, INR_PRECISE),
  pct: (v: Money) => format(v, PCT),
  ratio: (v: Money) => format(v, RATIO),
  count: (v: Money) => format(v, COUNT),
  date: (iso: string) =>
    new Date(`${iso}T00:00:00`).toLocaleDateString("en-IN", {
      day: "numeric",
      month: "short",
    }),
};

/** An unknown, with its reason where a pointer or a screen reader will find it. */
export function Unknown({ why }: { why: string }) {
  return (
    <span
      title={why}
      aria-label={`unknown: ${why}`}
      className="cursor-help text-slate-400 dark:text-slate-500"
    >
      —
    </span>
  );
}

/** A formatted value, or the em dash with the reason it is missing. */
export function Value({ text, why }: { text: string | null; why: string }) {
  return text === null ? <Unknown why={why} /> : <span className="tabular-nums">{text}</span>;
}

// ---------------------------------------------------------------------------
// Why a day's figure is unknown, from the gaps the economics engine recorded.
// ---------------------------------------------------------------------------

export type MoneyMetric = keyof Pick<
  MoneyRow,
  | "spend_inr"
  | "delivered_revenue_inr"
  | "blended_cac_inr"
  | "mer"
  | "contribution_margin_inr"
  | "rto_rate"
  | "confirm_rate"
  | "delivered_orders"
>;

const NOT_ZERO = "Unknown, not zero.";

export function why(metric: MoneyMetric, row: MoneyRow): string {
  const gaps = new Set(row.gaps ?? []);
  const noTruth = gaps.has("business_truth_not_reported");
  const noSpend = gaps.has("spend_not_ingested");

  switch (metric) {
    case "spend_inr":
      return noSpend
        ? `No spend was ingested for this day. ${NOT_ZERO}`
        : `Spend is not on file for this day. ${NOT_ZERO}`;
    case "delivered_revenue_inr":
      return noTruth
        ? `The day's business truth was never reported. ${NOT_ZERO}`
        : `Delivered revenue was not reported for this day. ${NOT_ZERO}`;
    case "delivered_orders":
      return noTruth
        ? `The day's business truth was never reported. ${NOT_ZERO}`
        : `Delivered orders were not reported for this day. ${NOT_ZERO}`;
    case "blended_cac_inr":
      if (noSpend) return `Blended CAC needs ingested spend, which is missing for this day. ${NOT_ZERO}`;
      if (noTruth) return `Blended CAC needs delivered orders; the day was never reported. ${NOT_ZERO}`;
      return `Blended CAC needs delivered orders; none were reported for this day. ${NOT_ZERO}`;
    case "mer":
      if (noSpend) return `MER needs ingested spend, which is missing for this day. ${NOT_ZERO}`;
      if (noTruth) return `MER needs delivered revenue; the day was never reported. ${NOT_ZERO}`;
      return `MER needs delivered revenue and non-zero spend; one is missing for this day. ${NOT_ZERO}`;
    case "contribution_margin_inr":
      if (gaps.has("margin_rate_unknown")) return `Contribution margin needs a product margin rate; none is on file. ${NOT_ZERO}`;
      if (noSpend) return `Contribution margin needs ingested spend, which is missing for this day. ${NOT_ZERO}`;
      if (noTruth) return `Contribution margin needs delivered orders; the day was never reported. ${NOT_ZERO}`;
      return `Contribution margin could not be computed for this day. ${NOT_ZERO}`;
    case "rto_rate":
      return gaps.has("rto_not_reported") || noTruth
        ? `RTO orders were not reported for this day; unreported returns are not zero returns.`
        : `RTO rate needs confirmed orders; none were reported for this day. ${NOT_ZERO}`;
    case "confirm_rate":
      return gaps.has("confirm_rate_unmeasurable") || noTruth
        ? `Confirmed or total orders were not reported for this day. ${NOT_ZERO}`
        : `Confirm rate could not be computed for this day. ${NOT_ZERO}`;
  }
}

/** A margin computed without fulfilment or return-freight costs is a ceiling, and says so. */
export function marginCaveat(row: MoneyRow): string | undefined {
  if ((row.gaps ?? []).includes("costs_incomplete_margin_is_an_upper_bound")) {
    return "Upper bound: fulfilment cost or return freight is not on file, so the true margin is lower.";
  }
  if ((row.gaps ?? []).includes("margin_rate_partial")) {
    return "Some products have no margin rate on file; computed from the ones that do.";
  }
  return undefined;
}

// ---------------------------------------------------------------------------
// Week over week
// ---------------------------------------------------------------------------

/**
 * The arrow, the percentage and a word. Never the arrow alone, and never
 * colour alone: "▲ 12% · worse" reads the same in monochrome and to a screen
 * reader (PRD 16.5). Whether up is good comes from the runtime, per metric.
 */
export function Trend({ c }: { c: Comparison }) {
  if (c.direction === null) {
    return (
      <span className="text-xs text-slate-500 dark:text-slate-400">
        <Unknown why="A week-over-week change needs both weeks; one of them is unknown." /> vs last week
      </span>
    );
  }
  const arrow = c.direction === "up" ? "▲" : c.direction === "down" ? "▼" : "▶";
  const judgement =
    c.direction === "flat" || c.better_when === null
      ? null
      : c.direction === c.better_when
        ? "better"
        : "worse";
  const pct = c.change_pct === null ? null : PCT.format(Math.abs(c.change_pct));
  return (
    <span className="text-xs text-slate-600 dark:text-slate-400">
      <span aria-hidden="true">{arrow} </span>
      <span className="sr-only">{c.direction} </span>
      {pct ?? (c.direction === "flat" ? "no change" : "from nothing")} vs last week
      {judgement && <> · {judgement}</>}
    </span>
  );
}

// ---------------------------------------------------------------------------
// The question the CTA gate is holding a proposal behind
// ---------------------------------------------------------------------------

// The row's held_at is a timestamptz; the server that renders this page is
// on UTC. The owner is not, and the reports page already reads every date
// on the IST day (metrics_daily is keyed on it), so this clock says the same.
const HELD_AT = new Intl.DateTimeFormat("en-IN", {
  day: "numeric",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
  timeZone: "Asia/Kolkata",
});

/**
 * A proposal held at the CTA gate, read from its row. The question comes
 * first because it is the only thing the owner can act on here: the
 * proposal has no destination, so there is nothing to approve. Answering is
 * done on the settings page, which writes the owner's own assertion; the
 * runtime then closes this row and the next chat turn re-proposes with the
 * destination written into the action. Nothing on this card can execute.
 */
export function HeldProposal({ gate }: { gate: CtaGate }) {
  if (!gate.held) {
    return (
      <p className="text-sm text-slate-500 dark:text-slate-400">
        Nothing is held. A proposal that would build a campaign or an ad set is held here
        until a destination is on file; {gate.reason}.
      </p>
    );
  }

  // proposal_json is what the strategy model produced, as the gate stored
  // it. The schema asks for these fields; a row written from a turn whose
  // model dropped one must still render, so the shape is normalised here
  // rather than trusted.
  const p = {
    ...gate.proposal,
    goal: gate.proposal.goal ?? "",
    options: Array.isArray(gate.proposal.options) ? gate.proposal.options : [],
  };
  const rec = gate.recommendation;
  const recommendedOption = p.options.find((o) => o.label === p.recommended) ?? null;

  return (
    <div className="space-y-4 text-sm">
      <div className="rounded-md border border-amber-300 bg-amber-50 p-4 dark:border-amber-900 dark:bg-amber-950/40">
        <p className="text-xs font-semibold uppercase tracking-wide text-amber-800 dark:text-amber-300">
          Waiting on your answer
        </p>
        <p className="mt-1 text-slate-800 dark:text-slate-200">{gate.question}</p>
        <p className="mt-3">
          <a
            href="/settings"
            className="inline-block rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700 dark:bg-slate-100 dark:text-slate-900"
          >
            Answer in Settings
          </a>
          <span className="ml-3 text-xs text-slate-600 dark:text-slate-400">
            Then ask again in{" "}
            <a href="/chat" className="underline underline-offset-2">
              Chat
            </a>{" "}
            and it is re-proposed with the destination written in.
          </span>
        </p>
      </div>

      <div>
        <p className="text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
          What was proposed
        </p>
        <p className="mt-1 font-medium">{p.goal}</p>
        {p.single_strongest_reason && (
          <p className="mt-1 text-slate-600 dark:text-slate-400">{p.single_strongest_reason}</p>
        )}
        {p.options.length > 0 && (
          <ul className="mt-2 space-y-2">
            {p.options.map((o) => {
              const recommended = o.label === p.recommended;
              return (
                <li
                  key={o.label}
                  className={`rounded-md border p-3 ${
                    recommended
                      ? "border-slate-900 dark:border-slate-100"
                      : "border-slate-200 dark:border-slate-800"
                  }`}
                >
                  <div className="flex flex-wrap items-baseline justify-between gap-2">
                    <span className="font-medium">
                      {o.label}
                      {recommended && (
                        <span className="ml-2 text-xs font-normal uppercase tracking-wide text-slate-500">
                          recommended
                        </span>
                      )}
                    </span>
                    {o.action?.tool && (
                      <span className="text-xs text-slate-500">{o.action.tool.replace(/_/g, " ")}</span>
                    )}
                  </div>
                  <p className="mt-1 text-slate-700 dark:text-slate-300">{o.what}</p>
                  {(o.expected_effect || o.risk) && (
                    <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                      {o.expected_effect && <>expected: {o.expected_effect}</>}
                      {o.expected_effect && o.risk && <> · </>}
                      {o.risk && <>risk: {o.risk}</>}
                    </p>
                  )}
                </li>
              );
            })}
          </ul>
        )}
        {recommendedOption === null && p.recommended && (
          <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
            Recommended option: {p.recommended}
          </p>
        )}
      </div>

      {rec && (
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
            What the CTA model would choose
          </p>
          <p className="mt-1">
            <span className="font-medium">{rec.recommended_label ?? rec.recommended}</span>
            {rec.ranking && rec.ranking.length > 1 && (
              <span className="text-slate-500 dark:text-slate-400">
                {" "}
                · ranked {rec.ranking.join(", ")}
              </span>
            )}
          </p>
          {rec.rationale && rec.rationale.length > 0 && (
            <ul className="mt-1 list-disc space-y-0.5 pl-5 text-slate-700 dark:text-slate-300">
              {rec.rationale.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          )}
          {rec.qualifying_questions && rec.qualifying_questions.length > 0 && (
            <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
              To be surer it would need to know: {rec.qualifying_questions.join(" ")}
            </p>
          )}
        </div>
      )}

      <p className="text-xs text-slate-500 dark:text-slate-400">
        Held {HELD_AT.format(new Date(gate.held_at))} · {gate.reason}
        {gate.run_id && <> · run {gate.run_id.slice(0, 8)}</>}
      </p>
    </div>
  );
}
