import { Suspense } from "react";
import Link from "next/link";
import { notFound } from "next/navigation";

import { NotAuthorized, Refused, RuntimeUnreachable } from "@/lib/api";
import {
  creatives,
  isCreativeId,
  when,
  type ComplianceVerdict,
  type Creative,
  type CreativeStatus,
  type Rating,
  type Rubric,
  type RubricCriterion,
} from "@/lib/creatives";
import { defaultWorkspace, type AuthorizedWorkspace } from "@/lib/session";

import { analyseForm } from "../actions";

export const metadata = { title: "Creative" };

/**
 * One creative and its rating.
 *
 * The rating is shown with its sources kept apart - measured from the file,
 * judged by the model with a reason per score, refused or passed by the
 * compliance gate, compared against this account's own history - because the
 * owner needs to know which is which before deciding to disagree. Every
 * limitation the runtime recorded is printed, and the rubric version the
 * rating was scored against is stated beside it: a score is comparable to the
 * rubric it was scored under, not silently to a newer one.
 *
 * "Not analysed yet" and "failed: <reason>" are states of their own with the
 * runtime's own words, and each carries the button that runs the rating.
 */

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

function Problem({ error }: { error: unknown }) {
  const message =
    error instanceof RuntimeUnreachable
      ? "The agent runtime is not running. Start it with: uvicorn app.main:app --port 8000"
      : error instanceof NotAuthorized
        ? "Your session has expired. Sign in again."
        : String(error);
  return (
    <div className="rounded-md border border-amber-300 bg-amber-50 p-4 text-sm dark:border-amber-900 dark:bg-amber-950/40">
      <p className="font-medium">Cannot reach the agent runtime</p>
      <p className="mt-1 text-slate-700 dark:text-slate-300">{message}</p>
    </div>
  );
}

function Flash({ error, ok }: { error?: string; ok?: string }) {
  if (error) {
    return (
      <p
        role="alert"
        className="rounded-md border border-red-300 bg-red-50 p-3 text-sm dark:border-red-900 dark:bg-red-950/40"
      >
        <span aria-hidden="true">▲ </span>
        <span className="font-medium">Refused: </span>
        {error}
      </p>
    );
  }
  if (ok) {
    return (
      <p className="rounded-md border border-emerald-300 bg-emerald-50 p-3 text-sm dark:border-emerald-900 dark:bg-emerald-950/40">
        <span aria-hidden="true">● </span>
        {ok}
      </p>
    );
  }
  return null;
}

const STATUS: Record<CreativeStatus, { mark: string; label: string; tone: string }> = {
  uploaded: {
    mark: "○",
    label: "not analysed yet",
    tone: "border-slate-300 bg-slate-50 dark:border-slate-700 dark:bg-slate-800",
  },
  analysing: {
    mark: "◔",
    label: "analysing",
    tone: "border-sky-300 bg-sky-50 dark:border-sky-900 dark:bg-sky-950/40",
  },
  analysed: {
    mark: "●",
    label: "rated",
    tone: "border-emerald-300 bg-emerald-50 dark:border-emerald-900 dark:bg-emerald-950/40",
  },
  failed: {
    mark: "▲",
    label: "failed",
    tone: "border-red-300 bg-red-50 dark:border-red-900 dark:bg-red-950/40",
  },
};

function StatusChip({ status }: { status: CreativeStatus }) {
  const s = STATUS[status] ?? STATUS.failed;
  return (
    <span className={`inline-flex items-center gap-1 rounded border px-2 py-0.5 text-xs ${s.tone}`}>
      <span aria-hidden="true">{s.mark}</span>
      {s.label}
    </span>
  );
}

/** The gate's four verdicts, each a word and a mark. */
const VERDICT: Record<ComplianceVerdict, { mark: string; label: string; tone: string }> = {
  pass: {
    mark: "●",
    label: "pass",
    tone: "border-emerald-300 bg-emerald-50 dark:border-emerald-900 dark:bg-emerald-950/40",
  },
  warn: {
    mark: "◆",
    label: "warn",
    tone: "border-amber-300 bg-amber-50 dark:border-amber-900 dark:bg-amber-950/40",
  },
  block: {
    mark: "▲",
    label: "block",
    tone: "border-red-300 bg-red-50 dark:border-red-900 dark:bg-red-950/40",
  },
  not_evaluated: {
    mark: "○",
    label: "not evaluated",
    tone: "border-slate-300 bg-slate-50 dark:border-slate-700 dark:bg-slate-800",
  },
};

function VerdictChip({ verdict }: { verdict: ComplianceVerdict }) {
  const v = VERDICT[verdict] ?? VERDICT.not_evaluated;
  return (
    <span className={`inline-flex items-center gap-1 rounded border px-2 py-0.5 text-xs ${v.tone}`}>
      <span aria-hidden="true">{v.mark}</span>
      compliance: {v.label}
    </span>
  );
}

function AnalyseButton({ creativeId, again }: { creativeId: string; again: boolean }) {
  return (
    <form action={analyseForm} className="flex flex-wrap items-end gap-2 text-sm">
      <input type="hidden" name="creative_id" value={creativeId} />
      <label>
        <span className="block text-xs text-slate-600 dark:text-slate-400">Objective</span>
        <select
          name="objective"
          defaultValue="conversion"
          className="mt-1 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
        >
          <option value="conversion">Conversion (10-45 s)</option>
          <option value="consideration">Consideration (12-30 s)</option>
          <option value="awareness">Awareness (4-15 s)</option>
        </select>
      </label>
      <button
        type="submit"
        className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 dark:bg-slate-100 dark:text-slate-900"
      >
        {again ? "Rate it again" : "Rate it"}
      </button>
      <span className="basis-full text-xs text-slate-500 dark:text-slate-400">
        Ten to sixty seconds: the file is pulled down for ffmpeg, then judged on your
        organisation&apos;s own model tier.
      </span>
    </form>
  );
}

// ---------------------------------------------------------------------------
// The rating
// ---------------------------------------------------------------------------

function Overall({ rating }: { rating: Rating }) {
  return (
    <div className="flex flex-wrap items-baseline gap-3">
      {rating.overall !== null ? (
        <>
          <span className="text-4xl font-semibold tabular-nums">{rating.overall}</span>
          <span className="text-sm text-slate-500 dark:text-slate-400">/ 100</span>
        </>
      ) : (
        <span className="text-sm text-slate-700 dark:text-slate-300">
          No overall score: nothing was judged. Only what the file itself says is below.
        </span>
      )}
      {rating.compliance && <VerdictChip verdict={rating.compliance.verdict} />}
      <span className="basis-full text-xs text-slate-500 dark:text-slate-400">
        Scored against rubric {rating.rubric_version}
        {rating.model ? ` · ${rating.model}` : ""}
        {rating.cost_inr !== null ? ` · ₹${rating.cost_inr.toFixed(2)} in model spend` : ""}
      </span>
    </div>
  );
}

function JudgedList({ rating, rubric }: { rating: Rating; rubric: Rubric | null }) {
  // Weights come from the rubric as it is NOW. They are only shown when that
  // is the rubric this rating was scored under; a newer rubric's weights
  // beside an older rating would be a number with nothing behind it.
  const sameVersion = rubric !== null && rubric.version === rating.rubric_version;
  const byKey = new Map<string, RubricCriterion>(
    (rubric?.criteria ?? []).map((c) => [c.key, c] as const),
  );
  const judgedKeys = new Set(rating.judged.map((j) => j.key));
  const unjudged = (rubric?.criteria ?? []).filter(
    (c) => c.kind === "judged" && !judgedKeys.has(c.key),
  );

  if (rating.judged.length === 0) {
    return (
      <p className="text-sm text-slate-500 dark:text-slate-400">
        Nothing was judged. The reason is under Limitations.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      {!sameVersion && (
        <p className="text-xs text-slate-500 dark:text-slate-400">
          This was scored under rubric {rating.rubric_version}; the current rubric is{" "}
          {rubric?.version ?? "unavailable"}. The weights of that version are not on record,
          so none are shown.
        </p>
      )}
      <ul className="space-y-2">
        {rating.judged.map((j) => {
          const c = byKey.get(j.key);
          return (
            <li key={j.key} className="rounded-md border border-slate-200 p-3 text-sm dark:border-slate-800">
              <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
                <p className="font-medium">{c?.label ?? j.key}</p>
                <p className="tabular-nums">
                  <span className="text-lg font-semibold">{j.score}</span>
                  <span className="text-xs text-slate-500 dark:text-slate-400"> / 10</span>
                  {sameVersion && c && (
                    <span className="ml-3 text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
                      weight {c.weight}
                    </span>
                  )}
                </p>
              </div>
              <p className="mt-1 text-slate-700 dark:text-slate-300">{j.reason || "No reason was given."}</p>
            </li>
          );
        })}
        {unjudged.map((c) => (
          <li
            key={c.key}
            className="rounded-md border border-dashed border-slate-300 p-3 text-sm dark:border-slate-700"
          >
            <p className="font-medium">{c.label}</p>
            <p className="mt-1 text-slate-500 dark:text-slate-400">
              Not scored. The overall was computed over what was judged.
            </p>
          </li>
        ))}
      </ul>
    </div>
  );
}

function MeasuredList({ rating, rubric }: { rating: Rating; rubric: Rubric | null }) {
  const byKey = new Map((rubric?.criteria ?? []).map((c) => [c.key, c] as const));
  return (
    <ul className="space-y-2">
      {rating.measured.map((m) => (
        <li key={m.key} className="rounded-md border border-slate-200 p-3 text-sm dark:border-slate-800">
          <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
            <p className="font-medium">{byKey.get(m.key)?.label ?? m.key}</p>
            <p className="text-xs">
              <span aria-hidden="true">
                {m.passed === true ? "● " : m.passed === false ? "▲ " : "○ "}
              </span>
              {m.passed === true ? "passes" : m.passed === false ? "flagged" : "could not be read"}
              {m.observed ? ` · ${m.observed}` : ""}
            </p>
          </div>
          {m.note && <p className="mt-1 text-slate-700 dark:text-slate-300">{m.note}</p>}
        </li>
      ))}
    </ul>
  );
}

function Compliance({ compliance }: { compliance: NonNullable<Rating["compliance"]> }) {
  const stages = Array.isArray(compliance.stages_not_fully_checked)
    ? compliance.stages_not_fully_checked.map(String)
    : [];
  return (
    <div className="space-y-3 text-sm">
      <VerdictChip verdict={compliance.verdict} />
      <p className="text-xs text-slate-500 dark:text-slate-400">
        Checked against: {compliance.checked_against}
      </p>
      {compliance.findings.length === 0 ? (
        <p className="text-slate-500 dark:text-slate-400">No findings on the on-screen text.</p>
      ) : (
        <ul className="space-y-2">
          {compliance.findings.map((f, i) => (
            <li
              key={`${f.rule}-${i}`}
              className={`rounded-md border p-3 ${
                f.severity === "block"
                  ? "border-red-300 bg-red-50 dark:border-red-900 dark:bg-red-950/40"
                  : "border-amber-300 bg-amber-50 dark:border-amber-900 dark:bg-amber-950/40"
              }`}
            >
              <p className="font-medium">
                <span className="mr-2 text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
                  {f.severity}
                </span>
                {f.title}
              </p>
              <p className="text-xs text-slate-600 dark:text-slate-400">
                {f.rule}
                {f.span && <> — &ldquo;{f.span}&rdquo;</>}
              </p>
              {f.suggested_rewrite && (
                <p className="mt-1 text-slate-700 dark:text-slate-300">
                  <span className="font-medium">Fix:</span> {f.suggested_rewrite}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
      {stages.length > 0 && (
        <p className="text-xs text-slate-500 dark:text-slate-400">
          Not fully checked: {stages.join(", ")}
        </p>
      )}
    </div>
  );
}

function Reading({ rating }: { rating: Rating }) {
  const rows: Array<[string, string]> = [
    ["What is being sold", rating.what_is_sold],
    ["Strongest", rating.strongest],
    ["Weakest", rating.weakest],
    ["The one change", rating.rewrite],
  ].filter((r): r is [string, string] => Boolean(r[1]));
  if (rows.length === 0 && !rating.on_screen_text) return null;
  return (
    <dl className="space-y-3 text-sm">
      {rows.map(([k, v]) => (
        <div key={k}>
          <dt className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">{k}</dt>
          <dd className="mt-0.5 text-slate-700 dark:text-slate-300">{v}</dd>
        </div>
      ))}
      {rating.on_screen_text && (
        <div>
          <dt className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
            On-screen text, as read from the frames
          </dt>
          <dd className="mt-0.5 whitespace-pre-wrap text-slate-700 dark:text-slate-300">
            {rating.on_screen_text}
          </dd>
        </div>
      )}
    </dl>
  );
}

function History({ history }: { history: Rating["history"] }) {
  if (!history.available) {
    return (
      <p className="text-sm text-slate-500 dark:text-slate-400">
        Not enough history: {history.reason}
      </p>
    );
  }
  return (
    <div className="text-sm">
      <p className="mb-2 text-xs text-slate-500 dark:text-slate-400">
        This account&apos;s own creatives that have run as ads, last {history.window_days} days,
        cheapest result first.
      </p>
      <ul className="space-y-1">
        {history.creatives.map((h) => (
          <li key={h.creative_id} className="flex flex-wrap justify-between gap-x-3">
            <Link href={`/creatives/${h.creative_id}`} className="underline-offset-2 hover:underline">
              {h.name ?? h.creative_id.slice(0, 8)}
            </Link>
            <span className="tabular-nums text-slate-600 dark:text-slate-400">
              {h.overall_rating !== null ? `rated ${h.overall_rating} · ` : ""}₹
              {h.spend_inr.toLocaleString("en-IN")} · {h.results} results
              {h.cost_per_result_inr !== null
                ? ` · ₹${h.cost_per_result_inr.toLocaleString("en-IN", { maximumFractionDigits: 0 })} each`
                : ""}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Limitations({ rating }: { rating: Rating }) {
  const items = [...rating.limitations, ...rating.comparisons_unavailable];
  if (items.length === 0) return null;
  return (
    <ul className="list-disc space-y-1 pl-5 text-sm text-slate-700 dark:text-slate-300">
      {items.map((l, i) => (
        <li key={i}>{l}</li>
      ))}
    </ul>
  );
}

function RatingSection({
  creative,
  rating,
  rubric,
}: {
  creative: Creative;
  rating: Rating;
  rubric: Rubric | null;
}) {
  return (
    <>
      <Card title="Rating" hint="Weighted over the judged criteria; measured checks gate rather than score.">
        <Overall rating={rating} />
      </Card>
      <Card title="Judged" hint="Each score carries the sentence that would let you disagree with it.">
        <JudgedList rating={rating} rubric={rubric} />
      </Card>
      <Card title="Measured from the file" hint="ffmpeg, not a model.">
        <MeasuredList rating={rating} rubric={rubric} />
      </Card>
      {rating.compliance && (
        <Card
          title="Compliance pre-check"
          hint="The same gate that governs a live ad, run on the on-screen text before any spend."
        >
          <Compliance compliance={rating.compliance} />
        </Card>
      )}
      <Card title="What the model read">
        <Reading rating={rating} />
      </Card>
      <Card title="Against this account's own history" hint="Arithmetic, not opinion.">
        <History history={rating.history} />
      </Card>
      <Card title="Limitations" hint="What was not analysed is said, not hidden.">
        <Limitations rating={rating} />
      </Card>
      <Card title="Rate it again" hint="A new run replaces this rating; the objective sets the length band.">
        <AnalyseButton creativeId={creative.id} again />
      </Card>
    </>
  );
}

function CurrentRubric({ rubric }: { rubric: Rubric }) {
  return (
    <Card
      title="The rubric"
      hint={`Version ${rubric.version}. Written for Indian D2C sold on cash-on-delivery; judged weights sum to 100.`}
    >
      <ul className="space-y-2 text-sm">
        {rubric.criteria.map((c) => (
          <li key={c.key} className="rounded-md border border-slate-200 p-3 dark:border-slate-800">
            <div className="flex flex-wrap items-baseline justify-between gap-x-3">
              <p className="font-medium">{c.label}</p>
              <p className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
                {c.kind === "judged" ? `judged · weight ${c.weight}` : "measured · gate"}
              </p>
            </div>
            <p className="mt-1 text-slate-600 dark:text-slate-400">{c.why}</p>
          </li>
        ))}
      </ul>
    </Card>
  );
}

// ---------------------------------------------------------------------------

async function Detail({ ws, id }: { ws: AuthorizedWorkspace; id: string }) {
  let creative: Creative;
  let rubric: Rubric | null = null;
  try {
    // The rubric is fetched alongside but is not allowed to take the page
    // down: a rating without its labels is still a rating.
    const [c, r] = await Promise.all([
      creatives.get(ws, id),
      creatives.rubric(ws).catch(() => null),
    ]);
    creative = c;
    rubric = r;
  } catch (e) {
    // The runtime answers 404 for a creative that is not this workspace's,
    // and this page says the same - the existence of another tenant's row is
    // not this tenant's to learn.
    if (e instanceof Refused && e.status === 404) notFound();
    return <Problem error={e} />;
  }

  const rating = creative.status === "analysed" ? creative.rating_json : null;

  return (
    <div className="space-y-6">
      <div>
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-semibold tracking-tight">
            {creative.original_name ?? creative.id.slice(0, 8)}
          </h1>
          <StatusChip status={creative.status} />
        </div>
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
          {creative.media_type} · uploaded {when(creative.created_at)} IST
          {creative.analysed_at ? ` · rated ${when(creative.analysed_at)} IST` : ""}
          {creative.product_sku ? ` · ${creative.product_sku}` : ""}
          {creative.ai_generated ? " · declared AI-generated" : ""}
        </p>
      </div>

      {creative.status === "uploaded" && (
        <Card title="Not analysed yet" hint="The file is in storage; nothing has been concluded about it.">
          {creative.media_type === "video" ? (
            <AnalyseButton creativeId={creative.id} again={false} />
          ) : (
            <p className="text-sm text-slate-500 dark:text-slate-400">
              Only video creatives can be rated at the moment. This one is stored.
            </p>
          )}
        </Card>
      )}

      {creative.status === "analysing" && (
        <Card title="Analysing">
          <p className="text-sm text-slate-700 dark:text-slate-300">
            A run is in progress. Reload in a minute; a second run is refused while this one is
            going.
          </p>
        </Card>
      )}

      {creative.status === "failed" && (
        <Card title="Failed" hint="The runtime's own words, recorded on the creative.">
          <p
            role="alert"
            className="rounded-md border border-red-300 bg-red-50 p-3 text-sm dark:border-red-900 dark:bg-red-950/40"
          >
            <span aria-hidden="true">▲ </span>
            <span className="font-medium">failed: </span>
            {creative.analysis_error ?? "no reason was recorded"}
          </p>
          <div className="mt-4">
            <AnalyseButton creativeId={creative.id} again />
          </div>
        </Card>
      )}

      {rating && <RatingSection creative={creative} rating={rating} rubric={rubric} />}

      {rubric && <CurrentRubric rubric={rubric} />}
    </div>
  );
}

export default async function CreativePage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ error?: string; ok?: string }>;
}) {
  const [{ id }, { error, ok }] = await Promise.all([params, searchParams]);

  // A malformed id is a 404 here, before it is ever spelled into a runtime
  // path. There is nothing to compare it against, so it is refused.
  if (!isCreativeId(id)) notFound();

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
      <p className="text-sm">
        <Link
          href="/creatives"
          className="text-slate-600 underline-offset-2 hover:underline dark:text-slate-400"
        >
          ← All creatives
        </Link>
      </p>
      <Flash error={error} ok={ok} />
      <Suspense fallback={<Skeleton rows={4} />}>
        <Detail ws={ws} id={id} />
      </Suspense>
    </div>
  );
}
