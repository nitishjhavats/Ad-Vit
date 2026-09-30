import { NotAuthorized, Refused, RuntimeUnreachable } from "@/lib/api";

/**
 * The small pieces the billing page is built from. They mirror the dashboard's
 * Card / Skeleton / Unreachable so the page reads like the rest of the tenant
 * surface, with two additions the dashboard does not need: a Tag for a status
 * word and a Flash for the ?error= / ?ok= a Server Action redirects back with.
 *
 * A status is never carried by colour alone: every Tag and Flash has the word
 * in it, and the marks on a Flash are text (WCAG AA, PRD 16.5).
 */

export function Card({
  id,
  title,
  hint,
  children,
}: {
  /** An anchor, so a redirect or a row can land on this card (`#pay`). */
  id?: string;
  title: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <section
      id={id}
      className="scroll-mt-4 rounded-lg border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900"
    >
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

/**
 * Every runtime read on this page ends in one of these when it fails. The
 * cases are worded differently because they mean different things: the
 * runtime is down; the runtime said this session may not see the workspace;
 * the runtime refused with a reason it stated.
 */
export function Problem({ error }: { error: unknown }) {
  let title = "Something went wrong";
  let message = String(error);
  if (error instanceof RuntimeUnreachable) {
    title = "Cannot reach the agent runtime";
    message = "The agent runtime is not running. Start it with: uvicorn app.main:app --port 8000";
  } else if (error instanceof NotAuthorized) {
    title = "Not available to this session";
    message = "The runtime did not recognise this session as a member of the workspace.";
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

export type Tone = "ok" | "warn" | "bad" | "muted";

export function Tag({ children, tone = "muted" }: { children: React.ReactNode; tone?: Tone }) {
  const text: Record<Tone, string> = {
    ok: "bg-emerald-100 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-200",
    warn: "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200",
    bad: "bg-red-100 text-red-900 dark:bg-red-950 dark:text-red-200",
    muted: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
  };
  return <span className={`rounded px-2 py-0.5 text-xs font-medium ${text[tone]}`}>{children}</span>;
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
        <span className="font-medium">Done: </span>
        {ok}
      </p>
    );
  }
  return null;
}

export const select =
  "mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900";
export const button =
  "rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50 dark:bg-slate-100 dark:text-slate-900";

/** Rupees, Indian grouping, as the rest of the tenant surface prints money. */
export function inr(v: string | number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return `₹${Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

export function day(v: string | null | undefined): string {
  if (!v) return "—";
  return new Date(v).toLocaleDateString("en-IN", { timeZone: "Asia/Kolkata", dateStyle: "medium" });
}

/**
 * Date and time, IST, for the instants where the hour matters: when a
 * payment window closes, when a reference was submitted. The server renders
 * on UTC; the owner does not live there.
 */
export function when(v: string | null | undefined): string {
  if (!v) return "—";
  return new Date(v).toLocaleString("en-IN", {
    timeZone: "Asia/Kolkata",
    dateStyle: "medium",
    timeStyle: "short",
  });
}

export const input =
  "mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900";
export const buttonSmall =
  "rounded-md bg-slate-900 px-2.5 py-1 text-xs font-medium text-white hover:bg-slate-700 disabled:opacity-50 dark:bg-slate-100 dark:text-slate-900";

export const PERIOD_LABEL: Record<string, string> = {
  monthly: "per month",
  quarterly: "per quarter",
  yearly: "per year",
};
