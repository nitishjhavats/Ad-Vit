import { Suspense } from "react";

import { chooseCta, chooseTier, storeKey } from "@/app/settings/actions";
import { api, NotAuthorized, Refused, RuntimeUnreachable } from "@/lib/api";
import { defaultWorkspace, type AuthorizedWorkspace } from "@/lib/session";
import {
  CTA_DESTINATIONS,
  OPENROUTER_KIND,
  settings,
  type ModelFunction,
  type SecretStatus,
} from "@/lib/settings";

export const metadata = { title: "Settings" };

/**
 * What an organisation configures about itself, on one page.
 *
 * Four sections, each behind its own <Suspense> boundary so one runtime read
 * failing leaves the other three usable: the organisation's own OpenRouter
 * key, the model tier per function, where campaigns send people, and a
 * read-only card of what the plan and the owner's own switches currently
 * allow. Every write is a Server Action in ./actions.ts; every refusal the
 * runtime gives comes back as `?error=` and is shown beside the form.
 *
 * The key is the one value on this page that is never read back: the GET
 * reports that a key is stored and its last four characters, and the form
 * field is `type="password"` with no default value.
 */

const input =
  "mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900";
const button =
  "rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50 dark:bg-slate-100 dark:text-slate-900";

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

/**
 * Every runtime read on this page ends in one of these when it fails. The
 * cases are worded differently because they mean different things: the
 * runtime is down; the runtime answered 401/403; the runtime refused with a
 * reason, which is shown as the runtime wrote it.
 */
function Problem({ error }: { error: unknown }) {
  let title = "Something went wrong";
  let message = String(error);
  if (error instanceof RuntimeUnreachable) {
    title = "Cannot reach the agent runtime";
    message = "Start it with: uvicorn app.main:app --port 8000";
  } else if (error instanceof NotAuthorized) {
    title = "Not authorised";
    message = `The runtime answered: ${error.message}`;
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

function Flash({ error, ok }: { error?: string; ok?: string }) {
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
        <span className="font-medium">Saved: </span>
        {ok}
      </p>
    );
  }
  return null;
}

/** State is never colour alone: every chip carries a mark and a text label. */
const TONE = {
  ok: "border-emerald-300 bg-emerald-50 dark:border-emerald-900 dark:bg-emerald-950/40",
  warn: "border-amber-300 bg-amber-50 dark:border-amber-900 dark:bg-amber-950/40",
  muted: "border-slate-200 bg-slate-50 dark:border-slate-800 dark:bg-slate-900",
} as const;
const MARK = { ok: "● ", warn: "▲ ", muted: "○ " } as const;

function Chip({
  tone,
  label,
  detail,
}: {
  tone: keyof typeof TONE;
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

function when(v: string | null | undefined): string {
  if (!v) return "—";
  return new Date(v).toLocaleString("en-IN", {
    timeZone: "Asia/Kolkata",
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function inr(v: string | number): string {
  return `₹${Number(v).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

// ---------------------------------------------------------------------------
// 1. Bring your own key
// ---------------------------------------------------------------------------

function KeyStatus({ stored }: { stored: SecretStatus | null }) {
  if (!stored) {
    return (
      <Chip
        tone="warn"
        label="No key stored"
        detail="Facts, the audit and the compliance gate work without one. Narration, strategy and creative analysis do not."
      />
    );
  }
  if (stored.last_error && !stored.is_working) {
    return (
      <Chip
        tone="warn"
        label={`Key ending …${stored.hint ?? "????"} has never worked`}
        detail={`Version ${stored.key_version}, stored ${when(stored.created_at)}. Last error: ${stored.last_error}`}
      />
    );
  }
  if (stored.last_error) {
    return (
      <Chip
        tone="warn"
        label={`Key ending …${stored.hint ?? "????"} stopped working`}
        detail={`Last verified ${when(stored.last_verified_at)}. Last error: ${stored.last_error}. A revoked key or a spend limit on your OpenRouter account looks like this.`}
      />
    );
  }
  return (
    <Chip
      tone={stored.is_working ? "ok" : "muted"}
      label={`Key ending …${stored.hint ?? "????"} is stored`}
      detail={
        stored.is_working
          ? `Verified by a real call ${when(stored.last_verified_at)}. Version ${stored.key_version}${
              stored.rotated_at ? `, replaced ${when(stored.rotated_at)}` : ""
            }.`
          : `Stored ${when(stored.created_at)}; not used by a call yet, so not verified yet.`
      }
    />
  );
}

async function BringYourOwnKey({ ws }: { ws: AuthorizedWorkspace }) {
  let status;
  try {
    status = await settings.byok(ws);
  } catch (e) {
    return <Problem error={e} />;
  }
  const stored = status.secrets.find((s) => s.kind === OPENROUTER_KIND) ?? null;

  return (
    <Card
      title="Bring your own key"
      hint="Model calls are billed to your organisation's own OpenRouter account. ad-vit does not resell tokens, and there is no platform key to fall back to."
    >
      <div className="space-y-4">
        <KeyStatus stored={stored} />
        <form action={storeKey} className="space-y-3">
          <input type="hidden" name="workspace_id" value={ws.id} />
          <label className="block text-sm">
            <span className="text-slate-600 dark:text-slate-400">
              {stored ? "Replace the key" : "OpenRouter API key"}
            </span>
            <input
              name="api_key"
              type="password"
              required
              minLength={16}
              maxLength={512}
              autoComplete="off"
              spellCheck={false}
              placeholder="sk-or-v1-…"
              className={input}
            />
          </label>
          <p className="text-xs text-slate-500 dark:text-slate-400">
            Stored encrypted; shown back only as its last four characters. Only an owner or an
            admin of the organisation may set it.
          </p>
          <button type="submit" className={button}>
            {stored ? "Replace key" : "Store key"}
          </button>
        </form>
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// 2. Model tiers
// ---------------------------------------------------------------------------

function TierRow({ fn, ws }: { fn: ModelFunction; ws: AuthorizedWorkspace }) {
  const current = fn.chosen ?? fn.recommended;
  return (
    <li className="rounded-md border border-slate-200 p-3 text-sm dark:border-slate-800">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <p className="font-medium">{fn.label}</p>
          <p className="mt-1 text-xs text-slate-600 dark:text-slate-400">{fn.why_recommended}</p>
          <p className="mt-1 text-xs text-slate-500 dark:text-slate-500">
            {fn.fixed
              ? "Not a customer choice."
              : fn.chosen
                ? `You chose ${fn.chosen}; recommended is ${fn.recommended}.`
                : `Using the recommendation (${fn.recommended}) until you choose.`}
          </p>
        </div>
        {fn.fixed ? (
          <span className="rounded bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-700 dark:bg-slate-800 dark:text-slate-300">
            fixed · {fn.recommended}
          </span>
        ) : (
          <form action={chooseTier} className="flex items-end gap-2">
            <input type="hidden" name="workspace_id" value={ws.id} />
            <input type="hidden" name="role" value={fn.role ?? fn.key} />
            <input type="hidden" name="label" value={fn.label} />
            <label className="text-xs text-slate-600 dark:text-slate-400">
              Tier
              <select name="tier" defaultValue={current} className={`${input} min-w-48`}>
                {fn.options.map((o) => (
                  <option key={o.tier} value={o.tier}>
                    {o.tier}
                    {o.recommended ? " (recommended)" : ""}
                    {o.note ? ` — ${o.note}` : ""}
                  </option>
                ))}
              </select>
            </label>
            <button type="submit" className={button}>
              Save
            </button>
          </form>
        )}
      </div>
    </li>
  );
}

async function ModelTiers({ ws }: { ws: AuthorizedWorkspace }) {
  let models;
  try {
    models = await settings.models(ws);
  } catch (e) {
    return <Problem error={e} />;
  }
  return (
    <Card title="Model tiers" hint={models.note}>
      <ul className="space-y-2">
        {models.functions.map((fn) => (
          <TierRow key={fn.key} fn={fn} ws={ws} />
        ))}
      </ul>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// 3. Where campaigns send people
// ---------------------------------------------------------------------------

async function Destination({ ws }: { ws: AuthorizedWorkspace }) {
  let onFile;
  try {
    onFile = await settings.ctaOnFile(ws);
  } catch (e) {
    // A read that failed is not "not set". Rendering the empty state here
    // would invite the owner to re-assert a destination that may already be
    // on file; the other three sections show the problem, and so does this.
    return <Problem error={e} />;
  }
  const current = CTA_DESTINATIONS.find((d) => d.value === onFile?.destination) ?? null;

  return (
    <Card
      title="Where do your campaigns send people?"
      hint="No campaign is built until this is set. The destination decides the funnel, what gets measured and what a result even means - so the strategy holds any proposal that would build something until you have answered."
    >
      <div className="space-y-4">
        {current && onFile ? (
          <Chip
            tone="ok"
            label={`On file: ${current.label}`}
            detail={`Asserted ${when(onFile.asserted_at)}${
              onFile.stored_as !== current.value ? ` (stored as "${onFile.stored_as}")` : ""
            }. Choosing again supersedes it; campaigns already built keep the destination they were built for.`}
          />
        ) : (
          <Chip
            tone="warn"
            label="Not set"
            detail="Proposals that would create a campaign or an ad set are held at this question until you choose."
          />
        )}
        <form action={chooseCta} className="space-y-3">
          <input type="hidden" name="workspace_id" value={ws.id} />
          <fieldset>
            <legend className="text-sm text-slate-600 dark:text-slate-400">Destination</legend>
            <div className="mt-2 grid gap-2 sm:grid-cols-2">
              {CTA_DESTINATIONS.map((d) => (
                <label
                  key={d.value}
                  className="flex items-start gap-2 rounded-md border border-slate-300 p-3 text-sm dark:border-slate-700"
                >
                  <input
                    type="radio"
                    name="destination"
                    value={d.value}
                    required
                    defaultChecked={current?.value === d.value}
                    className="mt-1"
                  />
                  <span>
                    <span className="font-medium">{d.label}</span>
                    <span className="mt-0.5 block text-xs text-slate-600 dark:text-slate-400">
                      {d.hint}
                    </span>
                  </span>
                </label>
              ))}
            </div>
          </fieldset>
          <label className="block text-sm">
            <span className="text-slate-600 dark:text-slate-400">
              Why this one (optional; kept as a training signal, like a rejection reason)
            </span>
            <input name="reason" maxLength={500} className={input} />
          </label>
          <button type="submit" className={button}>
            {current ? "Change destination" : "Set destination"}
          </button>
        </form>
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// 4. What the plan and your switches currently allow (read-only)
// ---------------------------------------------------------------------------

async function Automation({ ws }: { ws: AuthorizedWorkspace }) {
  let data;
  try {
    data = await api.connections(ws);
  } catch (e) {
    return <Problem error={e} />;
  }
  const { automation, workspace } = data;

  return (
    <Card
      title="Automation and caps"
      hint="Read-only here. The autonomy ceiling and the caps come from the plan; raising them is a billing change."
    >
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Chip
          tone={automation.is_paused || automation.capped_by_plan ? "warn" : "ok"}
          label={`Autonomy L${automation.autonomy_level} intent → L${automation.effective_autonomy} effective`}
          detail={
            // Ordered by cause, the way t_advit.effective_autonomy decides it:
            // a pause freezes everything; a non-full access mode floors the
            // level to L0 before the plan is consulted; only then is the plan's
            // ceiling the reason. The runtime's capped_by_plan is simply
            // "effective < intent", so it is true for the access-mode floor as
            // well and cannot be read as the cause on its own.
            automation.is_paused
              ? "Frozen by the owner. Nothing executes until it is resumed."
              : automation.access_mode !== "full"
                ? `Floored to L0 by the organisation's access mode (${automation.access_mode}).`
                : automation.capped_by_plan
                  ? "Capped by the plan's autonomy ceiling."
                  : "Nothing is holding the effective level below the intent."
          }
        />
        <Chip
          tone="muted"
          label={`Daily cap ${inr(workspace.daily_cap_inr)}`}
          detail="An action that would breach it is refused outright; nobody is asked to approve a known breach."
        />
        <Chip
          tone="muted"
          label={`Monthly cap ${inr(workspace.monthly_cap_inr)}`}
          detail="The ceiling on what automation may spend in a month, across every connected account."
        />
        <Chip
          tone={automation.access_mode === "full" ? "ok" : "warn"}
          label={`Access mode: ${automation.access_mode}`}
          detail={
            automation.access_mode === "full"
              ? "Full access. Effective autonomy is min(intent, plan)."
              : "Not full access: effective autonomy is floored to L0 until the subscription is in order."
          }
        />
      </div>
      <p className="mt-4 text-sm text-slate-600 dark:text-slate-400">
        Plan, subscription and invoices are on{" "}
        <a href="/billing" className="underline underline-offset-2">
          Billing
        </a>
        .
      </p>
    </Card>
  );
}

// ---------------------------------------------------------------------------

export default async function SettingsPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string; ok?: string }>;
}) {
  const { error, ok } = await searchParams;

  // Resolved once, here, from the signed-in session - and awaited before the
  // Suspense boundaries, so an account with no workspace gets one honest
  // message instead of four identical errors. `defaultWorkspace()` redirects
  // to /login when there is no session.
  const ws = await defaultWorkspace();

  if (!ws) {
    return (
      <div className="rounded-md border border-slate-200 bg-white p-5 text-sm dark:border-slate-800 dark:bg-slate-900">
        <p className="font-medium">No workspace yet</p>
        <p className="mt-1 text-slate-600 dark:text-slate-400">
          This account is signed in but is not a member of any workspace. An owner or admin of
          the organisation can add you.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
          Your key, your model choices, and where your campaigns send people.
        </p>
      </div>

      <Flash error={error} ok={ok} />

      <Suspense fallback={<Skeleton rows={2} />}>
        <BringYourOwnKey ws={ws} />
      </Suspense>
      <Suspense fallback={<Skeleton rows={4} />}>
        <ModelTiers ws={ws} />
      </Suspense>
      <Suspense fallback={<Skeleton rows={3} />}>
        <Destination ws={ws} />
      </Suspense>
      <Suspense fallback={<Skeleton rows={2} />}>
        <Automation ws={ws} />
      </Suspense>
    </div>
  );
}
