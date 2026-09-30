/**
 * Agent-runtime client.
 *
 * Server-side only. The runtime reaches Meta and the database, so nothing here
 * may ever run in a browser bundle - which is why every call site is a server
 * component or a Server Action, and why there is deliberately no
 * `/api/proxy/[...path]` route handler. The one way this pattern fails badly is
 * a confused deputy - a handler forwarding an arbitrary path or workspace over
 * the privileged channel - and the only reliable defence is that such a call
 * cannot be spelled.
 *
 * Two things changed when the runtime started requiring authentication:
 *
 *   1. Every call carries the CALLER's access token. The runtime resolves the
 *      tenant from it; this process holds no credential of its own that could
 *      act for anybody.
 *   2. Every workspace-scoped method takes an `AuthorizedWorkspace`, not a
 *      `string`. That type cannot be constructed outside `lib/session.ts`, so a
 *      page that forgets to prove ownership does not fail at runtime - it fails
 *      to compile.
 */

import "server-only";

import { accessToken, type AuthorizedWorkspace } from "@/lib/session";

const BASE = process.env.AGENT_RUNTIME_URL ?? "http://127.0.0.1:8000";

export class RuntimeUnreachable extends Error {
  constructor(readonly path: string, cause: unknown) {
    super(`agent runtime unreachable at ${BASE}${path}`);
    this.cause = cause;
  }
}

export class NotAuthorized extends Error {}

/** A 4xx/5xx the runtime explained. `detail` is the FastAPI `detail` string. */
export class Refused extends Error {
  constructor(readonly status: number, readonly request: string, readonly detail: string) {
    super(`${request} -> ${status}: ${detail}`);
  }
}

/**
 * The one way this process talks to the runtime. Exported as `runtimeCall`
 * for the per-area clients (lib/billing.ts, lib/creatives.ts, ...) so they
 * share the token handling and the error taxonomy here without this file
 * growing a method per route. The rule those files inherit: every
 * workspace-scoped method takes an `AuthorizedWorkspace`, never a string.
 */
export async function runtimeCall<T>(path: string, init?: RequestInit): Promise<T> {
  return call<T>(path, init);
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const token = await accessToken();
  if (!token) {
    // No anonymous fallback. The runtime would refuse anyway; failing here
    // means the reason is legible instead of arriving as a 401 from a service
    // the page author never thinks about.
    throw new NotAuthorized(`no session for ${path}`);
  }

  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
        ...(init?.headers ?? {}),
      },
      // No cache directive needed: Next.js 16 does not cache fetch by
      // default. Freshness comes from that plus a <Suspense> boundary at the
      // call site, which streams the data into a prerendered shell.
    });
  } catch (cause) {
    throw new RuntimeUnreachable(path, cause);
  }

  if (response.status === 401) {
    throw new NotAuthorized(`${init?.method ?? "GET"} ${path} -> ${response.status}`);
  }
  // A 403 carries the runtime's reason ("only an owner or admin may apply a
  // coupon", "not available inside an impersonation session") and the page
  // should say that sentence rather than guess one. It used to be folded into
  // NotAuthorized with the body unread, and two pages then asserted a cause
  // they had never received. 404 stays a plain error: it is the runtime's
  // "nothing here" for a workspace that is not yours, and there is nothing to
  // explain.

  if (!response.ok) {
    const body = await response.text();
    // The token is in the request headers, never in this message. An error
    // string ends up in a log, a Sentry event and sometimes on a screen.
    let detail = body.slice(0, 300);
    try {
      const parsed = JSON.parse(body) as { detail?: unknown };
      if (typeof parsed.detail === "string") detail = parsed.detail;
    } catch {
      // not JSON; the raw prefix is what there is
    }
    throw new Refused(response.status, `${init?.method ?? "GET"} ${path}`, detail);
  }
  return (await response.json()) as T;
}

// ---------------------------------------------------------------------------
// Types. Deliberately narrow - only the fields the UI actually renders, so a
// change in an unread field does not break a build.
// ---------------------------------------------------------------------------

/**
 * Liveness and identity only.
 *
 * `meta_driver`, `write_allowlist` and the database error moved to
 * `/api/health/detail`, which requires a token. The allowlist names the ad
 * accounts the runtime may spend money on and the error string carries the DSN
 * and internal hostnames; `/health` is unauthenticated by necessity, because a
 * container health check cannot hold a session.
 */
export type Health = {
  status: string;
  product: string;
  by: string;
  website: string;
};

export type HealthDetail = {
  meta_driver: string;
  write_allowlist: string[];
  database: { ok: boolean; error: string | null };
  openrouter_key_present: boolean;
};

export type ConnectionsHealth = {
  workspace: {
    workspace_name: string;
    business_type: string;
    autonomy_level: number;
    effective_autonomy: number;
    daily_cap_inr: string | number;
    monthly_cap_inr: string | number;
  };
  automation: {
    autonomy_level: number;
    effective_autonomy: number;
    capped_by_plan: boolean;
    access_mode: string;
    is_paused: boolean;
  };
  meta_connections: Array<{
    ad_account_id: string;
    health: string;
    write_enabled: boolean;
    currency: string;
    dataset_count: number;
    measurement_ready: boolean;
    health_detail: { account_name?: string; note?: string; has_payment_method?: boolean };
  }>;
  model_access: { driver: string; openrouter_key_present: boolean };
};

export type AuditReport = {
  ad_account_id: string;
  score: number;
  measurement_ready: boolean;
  counts: Record<string, number>;
  findings: Array<{
    code: string;
    severity: "blocking" | "high" | "medium" | "low" | "info";
    title: string;
    detail: string;
    remedy: string;
  }>;
};

export type Approval = {
  id: string;
  status: string;
  risk_class: string;
  impact_inr: string | number | null;
  expires_at: string;
  expired: boolean;
  decision_type: string;
  reasoning: string | null;
  horizon_days: number;
  confidence: string | number | null;
};

export type Dashboard = {
  money: Array<{
    date: string;
    blended_cac_inr: string | number | null;
    contribution_margin_inr: string | number | null;
    confirm_rate: string | number | null;
    rto_rate: string | number | null;
    delivered_aov_inr: string | number | null;
    mer: string | number | null;
  }>;
  platform: {
    spend_inr: string | number;
    impressions: number;
    link_clicks: number;
    results: number;
    attribution_regime: string | null;
    freshness: string | null;
  } | null;
  data_freshness: string | null;
  note: string;
};

export type ChatResponse = {
  run_id: string;
  intent: string;
  mode: string;
  narration: string;
  proposal: {
    goal: string;
    assumptions: string[];
    options: Array<{
      label: string;
      what: string;
      expected_effect: string;
      risk: string;
      cost_of_being_wrong: string;
    }>;
    recommended: string;
    single_strongest_reason: string;
    questions: string[];
  } | null;
  questions: string[];
  compliance: {
    verdict: string;
    reason?: string;
    findings: Array<{
      rule_code: string;
      instrument: string;
      offending_span: string | null;
      suggested_rewrite: string | null;
      source_url: string;
      as_of: string;
      needs_legal_verification: boolean;
    }>;
  };
  facts: Record<string, unknown>;
  gaps: string[];
  retrieved_record_ids: string[];
  cost: {
    inr: number;
    calls: Array<{
      role: string;
      class: string;
      model: string;
      cost_inr: number;
      fell_back: boolean;
    }>;
  };
};

// ---------------------------------------------------------------------------

/**
 * `/health` and `/api/brand` are the runtime's only public routes, so they are
 * the only two methods here that do not send a token - and `publicCall` exists
 * so that fact is visible in the shape of the code rather than implied by the
 * absence of something.
 */
async function publicCall<T>(path: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`);
  } catch (cause) {
    throw new RuntimeUnreachable(path, cause);
  }
  if (!response.ok) throw new Error(`GET ${path} -> ${response.status}`);
  return (await response.json()) as T;
}

export const api = {
  health: () => publicCall<Health>("/health"),
  brand: () => publicCall<Record<string, string>>("/api/brand"),

  healthDetail: () => call<HealthDetail>("/api/health/detail"),

  // Every one of these takes a workspace that lib/session.ts has proved. The
  // SIGNATURE is the enforcement: passing a bare string does not type-check, so
  // "which tenant is this?" cannot be answered by a constant at the top of a
  // page file again.
  connections: (ws: AuthorizedWorkspace) =>
    call<ConnectionsHealth>(`/api/workspaces/${ws.id}/connections/health`),
  approvals: (ws: AuthorizedWorkspace) =>
    call<Approval[]>(`/api/workspaces/${ws.id}/approvals?status=pending`),
  dashboard: (ws: AuthorizedWorkspace, days = 30) =>
    call<Dashboard>(`/api/workspaces/${ws.id}/dashboard?days=${days}`),
  chat: (ws: AuthorizedWorkspace, message: string) =>
    call<ChatResponse>(`/api/workspaces/${ws.id}/chat`, {
      method: "POST",
      body: JSON.stringify({ message }),
    }),

  // Ad accounts are resolved to their workspace by the runtime, through
  // t_advit.meta_connections under RLS - so this one is keyed by the account and
  // still cannot reach another tenant's.
  audit: (adAccountId: string) => call<AuditReport>(`/api/audit/account/${adAccountId}`),
};
