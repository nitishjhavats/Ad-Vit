import { Suspense } from "react";
import Link from "next/link";

import { NotAuthorized, RuntimeUnreachable } from "@/lib/api";
import {
  creatives,
  when,
  type CreativeStatus,
  type CreativeSummary,
  type Rubric,
} from "@/lib/creatives";
import { defaultWorkspace, type AuthorizedWorkspace } from "@/lib/session";

import { UploadForm } from "./UploadForm";

export const metadata = { title: "Creatives" };

/**
 * The creative studio: upload a video, get told what is wrong with it.
 *
 * Three things on the page, in the order the owner uses them: the upload form,
 * the list of what has been uploaded and how it scored, and the rubric it was
 * scored against - shown here rather than hidden behind the rating, so an
 * owner can disagree with the rubric as well as with a score.
 *
 * Every runtime read sits behind its own <Suspense> boundary, as on the
 * dashboard: the shell paints first and the data streams in.
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

/**
 * A status is a symbol and a word, never a colour alone (WCAG AA, PRD 16.5).
 * `analysing` is what a second tab would see mid-run; `failed` carries its
 * reason on the creative's own page.
 */
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

function Row({ c }: { c: CreativeSummary }) {
  const score = c.status === "analysed" && c.overall !== null ? Number(c.overall) : null;
  return (
    <li className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-md border border-slate-200 p-3 text-sm dark:border-slate-800">
      <Link
        href={`/creatives/${c.id}`}
        className="min-w-0 flex-1 truncate font-medium underline-offset-2 hover:underline"
      >
        {c.original_name ?? c.id.slice(0, 8)}
      </Link>
      <StatusChip status={c.status} />
      <span className="w-20 text-right tabular-nums">
        {score !== null ? (
          <>
            <span className="text-lg font-semibold">{score}</span>
            <span className="text-xs text-slate-500 dark:text-slate-400"> / 100</span>
          </>
        ) : c.status === "analysed" ? (
          <span className="text-xs text-slate-500 dark:text-slate-400">measured only</span>
        ) : (
          <span className="text-xs text-slate-500 dark:text-slate-400">—</span>
        )}
      </span>
      <span className="w-full text-xs text-slate-500 sm:w-auto dark:text-slate-400">
        uploaded {when(c.created_at)} IST
        {c.product_sku ? ` · ${c.product_sku}` : ""}
        {c.media_type !== "video" ? ` · ${c.media_type}` : ""}
      </span>
    </li>
  );
}

async function List({ ws }: { ws: AuthorizedWorkspace }) {
  let rows: CreativeSummary[];
  try {
    rows = await creatives.list(ws);
  } catch (e) {
    return <Problem error={e} />;
  }

  return (
    <Card
      title="Uploaded"
      hint="Newest first. The score is the weighted judged rating; measured checks gate rather than score."
    >
      {rows.length === 0 ? (
        <p className="text-sm text-slate-500 dark:text-slate-400">
          Nothing uploaded yet. The first one goes in the form above.
        </p>
      ) : (
        <ul className="space-y-2">
          {rows.map((c) => (
            <Row key={c.id} c={c} />
          ))}
        </ul>
      )}
    </Card>
  );
}

function RubricList({ rubric }: { rubric: Rubric }) {
  const measured = rubric.criteria.filter((c) => c.kind === "measured");
  const judged = rubric.criteria.filter((c) => c.kind === "judged");
  return (
    <div className="space-y-5 text-sm">
      <div>
        <h3 className="font-medium">Measured from the file - no model</h3>
        <ul className="mt-2 space-y-2">
          {measured.map((c) => (
            <li key={c.key} className="rounded-md border border-slate-200 p-3 dark:border-slate-800">
              <p className="font-medium">{c.label}</p>
              <p className="mt-1 text-slate-600 dark:text-slate-400">{c.why}</p>
            </li>
          ))}
        </ul>
      </div>
      <div>
        <h3 className="font-medium">Judged, with a reason for each score</h3>
        <ul className="mt-2 space-y-2">
          {judged.map((c) => (
            <li key={c.key} className="rounded-md border border-slate-200 p-3 dark:border-slate-800">
              <div className="flex items-baseline justify-between gap-3">
                <p className="font-medium">{c.label}</p>
                <p className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
                  weight {c.weight}
                </p>
              </div>
              <p className="mt-1 text-slate-600 dark:text-slate-400">{c.why}</p>
            </li>
          ))}
        </ul>
      </div>
      <p className="text-xs text-slate-500 dark:text-slate-400">Rubric version {rubric.version}.</p>
    </div>
  );
}

async function RubricCard({ ws }: { ws: AuthorizedWorkspace }) {
  let rubric: Rubric;
  try {
    rubric = await creatives.rubric(ws);
  } catch (e) {
    return <Problem error={e} />;
  }
  return (
    <Card
      title="What a video is judged on"
      hint="Written for Indian D2C sold on cash-on-delivery, where RTO is the number that decides. Judged weights sum to 100."
    >
      <RubricList rubric={rubric} />
    </Card>
  );
}

export default async function CreativesPage() {
  // Resolved once, from the signed-in session, before the Suspense boundaries -
  // so an account with no workspace gets one honest message, not three.
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
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Creative studio</h1>
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
          Upload a video ad and get told what is wrong with it before any spend.
        </p>
      </div>

      <Card
        title="Upload"
        hint="The file goes from your browser straight to storage; it never passes through this site's server."
      >
        <UploadForm />
      </Card>

      <Suspense fallback={<Skeleton rows={3} />}>
        <List ws={ws} />
      </Suspense>

      <Suspense fallback={<Skeleton rows={4} />}>
        <RubricCard ws={ws} />
      </Suspense>
    </div>
  );
}
