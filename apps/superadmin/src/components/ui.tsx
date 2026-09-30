import { RuntimeUnreachable, NotAuthorized, Refused } from "@/lib/api";

export function Card({
  title,
  hint,
  children,
  action,
}: {
  title: string;
  hint?: string;
  children: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900">
      <div className="mb-4 flex items-start justify-between gap-4">
        <div>
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
            {title}
          </h2>
          {hint && <p className="mt-1 text-xs text-slate-500">{hint}</p>}
        </div>
        {action}
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
 * Every runtime read on every page ends in one of these. The three cases are
 * worded differently because they mean different things: the runtime is
 * down; the runtime answered "nothing here" (which for /api/admin means "not
 * an operator" or "no such row"); the runtime refused with a reason.
 */
export function Problem({ error }: { error: unknown }) {
  let title = "Something went wrong";
  let message = String(error);
  if (error instanceof RuntimeUnreachable) {
    title = "Cannot reach the agent runtime";
    message = "Start it with: uvicorn app.main:app --port 8000";
  } else if (error instanceof NotAuthorized) {
    title = "Nothing here";
    message = "The runtime answered 404. Either this session is not an operator, or the row is gone.";
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

/**
 * Status chips are diagnostic: each says what is wrong, never colour alone
 * (WCAG AA, PRD 16.5).
 */
const TONE: Record<string, string> = {
  ok: "border-emerald-300 bg-emerald-50 dark:border-emerald-900 dark:bg-emerald-950/40",
  warn: "border-amber-300 bg-amber-50 dark:border-amber-900 dark:bg-amber-950/40",
  bad: "border-red-300 bg-red-50 dark:border-red-900 dark:bg-red-950/40",
  muted: "border-slate-200 bg-slate-50 dark:border-slate-800 dark:bg-slate-900",
};
const MARK: Record<string, string> = { ok: "● ", warn: "▲ ", bad: "✖ ", muted: "○ " };

export function Chip({
  tone,
  label,
  detail,
}: {
  tone: "ok" | "warn" | "bad" | "muted";
  label: string;
  detail?: string;
}) {
  return (
    <div className={`rounded-md border px-3 py-2 text-sm ${TONE[tone]}`}>
      <p className="font-medium">
        <span aria-hidden="true">{MARK[tone]}</span>
        {label}
      </p>
      {detail && <p className="mt-0.5 text-xs text-slate-600 dark:text-slate-400">{detail}</p>}
    </div>
  );
}

export function Tag({ children, tone = "muted" }: { children: React.ReactNode; tone?: "ok" | "warn" | "bad" | "muted" }) {
  const text: Record<string, string> = {
    ok: "bg-emerald-100 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-200",
    warn: "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200",
    bad: "bg-red-100 text-red-900 dark:bg-red-950 dark:text-red-200",
    muted: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
  };
  return (
    <span className={`rounded px-2 py-0.5 text-xs font-medium ${text[tone]}`}>{children}</span>
  );
}

export function Stat({ label, value, hint }: { label: string; value: React.ReactNode; hint?: string }) {
  return (
    <div className="rounded-md border border-slate-200 p-3 dark:border-slate-800">
      <p className="text-xs uppercase tracking-wide text-slate-500">{label}</p>
      <p className="mt-1 text-2xl font-semibold tabular-nums">{value}</p>
      {hint && <p className="mt-0.5 text-xs text-slate-500">{hint}</p>}
    </div>
  );
}

export const input =
  "mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900";
export const button =
  "rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50 dark:bg-slate-100 dark:text-slate-900";
export const buttonQuiet =
  "rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800";

export function inr(v: string | number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return `₹${Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

export function when(v: string | null | undefined): string {
  if (!v) return "—";
  return new Date(v).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", dateStyle: "medium", timeStyle: "short" });
}

export function day(v: string | null | undefined): string {
  if (!v) return "—";
  return new Date(v).toLocaleDateString("en-IN", { timeZone: "Asia/Kolkata", dateStyle: "medium" });
}
