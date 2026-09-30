import { Suspense } from "react";
import { api, RuntimeUnreachable, type ChatResponse } from "@/lib/api";
import { defaultWorkspace, type AuthorizedWorkspace } from "@/lib/session";

export const metadata = { title: "Chat" };

function Verdict({ compliance }: { compliance: ChatResponse["compliance"] }) {
  if (compliance.verdict === "not_applicable") {
    return (
      <p className="text-sm text-slate-500 dark:text-slate-400">
        Compliance gate: not applicable — {compliance.reason}
      </p>
    );
  }
  if (compliance.verdict !== "block") {
    return (
      <p className="text-sm text-slate-500 dark:text-slate-400">
        Compliance gate: {compliance.verdict}
      </p>
    );
  }
  // Rendered from the stored rule, never from a model's paraphrase of it. The
  // owner needs the instrument, the span and the source in order to act.
  return (
    <div className="rounded-md border border-red-300 bg-red-50 p-3 text-sm dark:border-red-900 dark:bg-red-950/40">
      <p className="font-medium">Blocked before anything ran — ₹0 of model spend</p>
      <ul className="mt-2 space-y-2">
        {compliance.findings.map((f, i) => (
          <li key={`${f.rule_code}-${i}`}>
            <p>
              <span className="font-medium">{f.instrument}</span>
              {f.offending_span && <> — “{f.offending_span}”</>}
              {f.needs_legal_verification && (
                <span className="ml-2 rounded bg-amber-100 px-1.5 py-0.5 text-xs text-amber-900 dark:bg-amber-950 dark:text-amber-200">
                  needs legal verification
                </span>
              )}
            </p>
            <p className="text-xs text-slate-600 dark:text-slate-400">
              <a
                href={f.source_url}
                target="_blank"
                rel="noopener noreferrer"
                className="underline underline-offset-2"
              >
                source
              </a>{" "}
              · as of {f.as_of}
            </p>
            {f.suggested_rewrite && (
              <p className="mt-1 text-slate-700 dark:text-slate-300">
                <span className="font-medium">Fix:</span> {f.suggested_rewrite}
              </p>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

async function Answer({ q, ws }: { q: string; ws: AuthorizedWorkspace }) {
  let answer: ChatResponse;
  try {
    answer = await api.chat(ws, q);
  } catch (e) {
    return (
      <p className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm dark:border-amber-900 dark:bg-amber-950/40">
        {e instanceof RuntimeUnreachable ? "The agent runtime is not running." : String(e)}
      </p>
    );
  }

  return (
    <div className="space-y-5">
      <Verdict compliance={answer.compliance} />

      <section className="rounded-lg border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900">
        <p className="whitespace-pre-wrap text-sm leading-relaxed">{answer.narration}</p>
      </section>

      {answer.proposal && (
        <section className="rounded-lg border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
            Options
          </h2>
          <ul className="mt-3 space-y-3">
            {answer.proposal.options.map((o) => (
              <li
                key={o.label}
                className={`rounded-md border p-3 text-sm ${
                  o.label === answer.proposal?.recommended
                    ? "border-emerald-300 bg-emerald-50 dark:border-emerald-900 dark:bg-emerald-950/30"
                    : "border-slate-200 dark:border-slate-800"
                }`}
              >
                <p className="font-medium">{o.label}</p>
                <p className="mt-1">{o.what}</p>
                <p className="mt-1 text-slate-600 dark:text-slate-400">
                  Effect: {o.expected_effect}
                </p>
                <p className="text-slate-600 dark:text-slate-400">Risk: {o.risk}</p>
                <p className="text-slate-600 dark:text-slate-400">
                  If wrong: {o.cost_of_being_wrong}
                </p>
              </li>
            ))}
          </ul>
          <p className="mt-3 text-sm">
            <span className="font-medium">Recommended:</span> {answer.proposal.recommended} —{" "}
            {answer.proposal.single_strongest_reason}
          </p>
        </section>
      )}

      {answer.gaps.length > 0 && (
        <section className="rounded-md border border-amber-300 bg-amber-50 p-4 text-sm dark:border-amber-900 dark:bg-amber-950/40">
          <p className="font-medium">Data gaps</p>
          <ul className="mt-2 list-disc space-y-1 pl-5">
            {answer.gaps.map((g, i) => (
              <li key={i}>{g}</li>
            ))}
          </ul>
        </section>
      )}

      {/* Cost and provenance are shown, not hidden. A proposal the owner cannot
          interrogate is one they should not approve. */}
      <p className="text-xs text-slate-500 dark:text-slate-400">
        Run {answer.run_id.slice(0, 8)} · {answer.mode} · ₹{answer.cost.inr.toFixed(2)} in model
        spend · {answer.retrieved_record_ids.length} memory records in context
        {answer.cost.calls.length > 0 && (
          <> · {answer.cost.calls.map((c) => `${c.role}:${c.class}`).join(", ")}</>
        )}
      </p>
    </div>
  );
}

function Thinking() {
  return (
    <div className="space-y-3" aria-live="polite">
      <p className="text-sm text-slate-500 dark:text-slate-400">
        Running — compliance gate first, then the paid agents only if it clears.
      </p>
      <div className="h-24 animate-pulse rounded-md bg-slate-100 dark:bg-slate-800" aria-hidden />
    </div>
  );
}

export default async function ChatPage({
  searchParams,
}: {
  searchParams: Promise<{ q?: string }>;
}) {
  const { q } = await searchParams;
  const ws = await defaultWorkspace();

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Chat</h1>
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
          The control surface. The dashboard is the read model.
        </p>
      </div>

      <form method="GET" className="flex gap-2">
        <input
          name="q"
          defaultValue={q ?? ""}
          placeholder="Budget 5000 se 20000 kar do…"
          aria-label="Ask about this account"
          className="flex-1 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
        />
        <button
          type="submit"
          className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 dark:bg-slate-100 dark:text-slate-900"
        >
          Ask
        </button>
      </form>

      {q && (
        // Keyed on the question so a new one remounts the boundary and shows
        // the fallback again, rather than holding the previous answer on screen.
        <Suspense key={q} fallback={<Thinking />}>
          {ws ? (
            <Answer q={q} ws={ws} />
          ) : (
            <p className="text-sm text-slate-500 dark:text-slate-400">
              This account is not a member of any workspace yet.
            </p>
          )}
        </Suspense>
      )}
    </div>
  );
}
