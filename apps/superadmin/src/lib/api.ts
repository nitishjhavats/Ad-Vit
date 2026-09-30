/**
 * Agent-runtime client for the operator console.
 *
 * Server-side only, for the same reason as apps/web/src/lib/api.ts: every call
 * carries the caller's own token, this process holds no credential that could
 * act for anybody, and there is deliberately no proxy route handler that could
 * forward an arbitrary path.
 *
 * Every method takes an `Operator`, which only `lib/session.ts` can construct.
 * The type is the enforcement: a page that has not been through
 * `requireOperator()` cannot call any of these.
 */

import "server-only";

import { accessToken, type Operator } from "@/lib/session";

const BASE = process.env.AGENT_RUNTIME_URL ?? "http://127.0.0.1:8000";

export class RuntimeUnreachable extends Error {
  constructor(readonly path: string, cause: unknown) {
    super(`agent runtime unreachable at ${BASE}${path}`);
    this.cause = cause;
  }
}

export class NotAuthorized extends Error {}

/** A refusal the runtime explained - a 4xx with a body worth showing. */
export class Refused extends Error {
  constructor(readonly status: number, readonly detail: string) {
    super(detail);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const token = await accessToken();
  if (!token) throw new NotAuthorized(`no session for ${path}`);

  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
        ...(init?.headers ?? {}),
      },
    });
  } catch (cause) {
    throw new RuntimeUnreachable(path, cause);
  }

  if (response.status === 401 || response.status === 403 || response.status === 404) {
    // 404 is what the runtime says to a non-operator on every /api/admin
    // route, and also what it says about a row that is not there. Both are
    // "nothing here"; the page decides how to word it.
    throw new NotAuthorized(`${init?.method ?? "GET"} ${path} -> ${response.status}`);
  }

  if (!response.ok) {
    const body = await response.text();
    let detail = body.slice(0, 300);
    try {
      const parsed = JSON.parse(body) as { detail?: unknown };
      if (typeof parsed.detail === "string") detail = parsed.detail;
    } catch {
      // not JSON; the raw prefix is fine
    }
    throw new Refused(response.status, detail);
  }
  return (await response.json()) as T;
}

// ---------------------------------------------------------------------------
// Types: only the fields the console renders.
// ---------------------------------------------------------------------------

export type Overview = {
  organisations_by_status: Record<string, number>;
  open_findings_by_severity: Record<string, number>;
  draft_invoices: number;
  unpaid_invoices: number;
  live_coupons: number;
  subscriptions_needing_attention: number;
  payments_submitted: number;
};

export type OrganisationRow = {
  id: string;
  name: string;
  slug: string;
  status: string;
  legal_name: string | null;
  gstin: string | null;
  state_code: string | null;
  billing_email: string | null;
  activated_at: string | null;
  suspended_at: string | null;
  suspension_reason: string | null;
  created_at: string;
  plan_key: string | null;
  plan_name: string | null;
  subscription_status: string | null;
  current_period_end: string | null;
  coupon_code: string | null;
  members: number;
  workspaces: number;
  ad_accounts: number;
  overrides: number;
  billing_ready: boolean;
};

export type Entitlement = {
  feature_key: string;
  value: number | boolean | string;
  value_type: "integer" | "boolean" | "string";
  source: "override" | "plan" | "default";
  name: string;
  reason: string | null;
  expires_at: string | null;
  set_by: string | null;
};

export type InvoiceRow = {
  id: string;
  number: string | null;
  status: string;
  org_id?: string;
  org_name?: string;
  period_start: string;
  period_end: string;
  plan_name?: string;
  total_inr: string | number;
  gst_split: string;
  issued_at: string | null;
  due_at: string | null;
  paid_at: string | null;
  buyer_state_code: string | null;
  seller_gstin: string | null;
  draft_reason: string | null;
};

/**
 * One attempt to pay one invoice by bank transfer or UPI. The tenant asks
 * for one (awaiting_payment), quotes the reference they paid with
 * (submitted), and an operator matches it against the bank statement
 * (approved / rejected). A request nobody submitted within its window
 * expires. Nothing on the platform confirms money; a person does.
 */
export type Payment = {
  id: string;
  invoice_id: string;
  invoice_number: string | null;
  amount_inr: string;
  status: "awaiting_payment" | "submitted" | "approved" | "rejected" | "expired";
  method: string | null;
  reference: string | null;
  paid_on: string | null;
  window_ends_at: string | null;
  submitted_at: string | null;
  reviewed_at: string | null;
  review_note: string | null;
  created_at: string;
};

/** A payment as the operator sees it: with whose it is, and who submitted it. */
export type AdminPayment = Payment & {
  org_id: string;
  org_name: string;
  org_slug: string;
  submitted_by_email: string | null;
};

/**
 * What a review did. The invoice and subscription statuses come back so the
 * flash can say what the money changed, not just that the row closed.
 */
export type PaymentReview = {
  payment: AdminPayment;
  invoice_status: string;
  subscription_status: string;
};

export type OrganisationDetail = OrganisationRow & {
  members: Array<{ user_id: string; email: string; full_name: string | null; role: string; joined_at: string }>;
  workspaces: Array<{
    id: string;
    name: string;
    industry_key: string;
    autonomy_level: number;
    is_paused: boolean;
    daily_cap_inr: string | number;
    monthly_cap_inr: string | number;
    ad_accounts: Array<{ ad_account_id: string; health: string; write_enabled: boolean }>;
  }>;
  entitlements: Entitlement[];
  invoices: InvoiceRow[];
};

export type Plan = {
  plan_id: string;
  key: string;
  name: string;
  description: string | null;
  price_inr: string | number;
  billing_period: string;
  trial_days: number;
  is_active: boolean;
  sort_order: number;
  features: Record<string, unknown>;
  subscriptions: number;
};

export type Industry = {
  key: string;
  display_name: string;
  status: "draft" | "active" | "deprecated";
  pack_id: string;
  summary: string;
};

export type NewOrganisation = {
  name: string;
  slug?: string | null;
  plan_key: string;
  industry_key: string;
  workspace_name: string;
  owner_email: string;
  trial: boolean;
  daily_cap_inr?: number;
  monthly_cap_inr?: number;
};

/**
 * What onboarding made. `action_link` is present only when the owner had no
 * account and GoTrue invited one; nothing sends it - the operator does, by
 * hand, and the page says so beside it.
 */
export type CreatedOrganisation = {
  org_id: string;
  slug: string;
  workspace_id: string;
  subscription_id: string;
  subscription_status: string;
  trial_ends_at: string | null;
  current_period_end: string;
  owner_user_id: string;
  action_link: string | null;
  invited: boolean;
  note: string;
};

/** A fresh one-time sign-in link for a member whose account exists. */
export type SignInLink = {
  org_id: string;
  user_id: string;
  email: string;
  action_link: string;
  note: string;
};

export type Coupon = {
  id: string;
  code: string;
  name: string;
  percent_off: string | number;
  valid_from: string;
  valid_to: string | null;
  max_redemptions: number | null;
  redemptions: number;
  is_active: boolean;
  is_live: boolean;
  created_at: string;
  plan_keys: string[];
};

export type Finding = {
  id: string;
  detected_at: string;
  kind: "source_changed" | "rule_stale" | "knowledge_stale" | "fetch_failed";
  subject: string;
  source_url: string | null;
  severity: "info" | "review" | "urgent";
  detail: Record<string, unknown>;
  acknowledged_at: string | null;
  acknowledged_by: string | null;
};

export type RuleRow = {
  code: string;
  jurisdiction: string;
  instrument: string;
  rule_type: string;
  severity: string;
  title: string;
  explanation: string;
  source_url: string;
  as_of: string;
  is_active: boolean;
  scope: string;
  days_old: number;
  window_days: number;
  stale: boolean;
};

export type KnowledgeRow = {
  id: string;
  topic: string;
  statement: string;
  source_url: string;
  as_of: string;
  severity: string;
  status: string;
  days_old: number;
  window_days: number;
  stale: boolean;
};

export type AuditRow = {
  id: number;
  at: string;
  scope: string;
  org_id: string | null;
  org_name: string | null;
  workspace_id: string | null;
  actor_type: string;
  actor_id: string | null;
  actor_email: string | null;
  impersonated_by: string | null;
  event: string;
  payload: Record<string, unknown>;
};

// ---------------------------------------------------------------------------

const ADMIN = "/api/admin";

export const api = {
  overview: (_op: Operator) => call<Overview>(`${ADMIN}/overview`),

  organisations: (_op: Operator) => call<OrganisationRow[]>(`${ADMIN}/organisations`),
  organisation: (_op: Operator, id: string) =>
    call<OrganisationDetail>(`${ADMIN}/organisations/${encodeURIComponent(id)}`),
  createOrganisation: (_op: Operator, body: NewOrganisation) =>
    call<CreatedOrganisation>(`${ADMIN}/organisations`, { method: "POST", body: JSON.stringify(body) }),
  industries: (_op: Operator) => call<Industry[]>(`${ADMIN}/industries`),
  issueSignInLink: (_op: Operator, orgId: string, userId: string) =>
    call<SignInLink>(
      `${ADMIN}/organisations/${encodeURIComponent(orgId)}/members/${encodeURIComponent(userId)}/sign-in-link`,
      { method: "POST" },
    ),
  setOrganisationStatus: (_op: Operator, id: string, status: string, reason: string | null) =>
    call<{ status: string }>(`${ADMIN}/organisations/${encodeURIComponent(id)}/status`, {
      method: "PATCH",
      body: JSON.stringify({ status, reason }),
    }),
  patchSubscription: (
    _op: Operator,
    id: string,
    body: { plan_key?: string; status?: string; reason?: string },
  ) =>
    call<{ status: string; plan_key: string }>(
      `${ADMIN}/organisations/${encodeURIComponent(id)}/subscription`,
      { method: "PATCH", body: JSON.stringify(body) },
    ),
  setOverride: (
    _op: Operator,
    id: string,
    featureKey: string,
    body: { value: number | boolean | string; reason: string; expires_at?: string | null },
  ) =>
    call<Entitlement>(
      `${ADMIN}/organisations/${encodeURIComponent(id)}/entitlements/${encodeURIComponent(featureKey)}`,
      { method: "PUT", body: JSON.stringify(body) },
    ),
  clearOverride: (_op: Operator, id: string, featureKey: string) =>
    call<{ previous: unknown }>(
      `${ADMIN}/organisations/${encodeURIComponent(id)}/entitlements/${encodeURIComponent(featureKey)}`,
      { method: "DELETE" },
    ),

  plans: (_op: Operator) => call<Plan[]>(`${ADMIN}/plans`),

  coupons: (_op: Operator) => call<Coupon[]>(`${ADMIN}/coupons`),
  createCoupon: (
    _op: Operator,
    body: {
      code: string;
      name: string;
      percent_off: number;
      plan_keys: string[];
      valid_to?: string | null;
      max_redemptions?: number | null;
    },
  ) => call<{ id: string; code: string }>(`${ADMIN}/coupons`, { method: "POST", body: JSON.stringify(body) }),
  patchCoupon: (_op: Operator, id: string, body: { is_active?: boolean; valid_to?: string; max_redemptions?: number }) =>
    call<Coupon>(`${ADMIN}/coupons/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(body) }),

  findings: (_op: Operator, includeAcknowledged = false) =>
    call<Finding[]>(`${ADMIN}/watch/findings?include_acknowledged=${includeAcknowledged}`),
  acknowledge: (_op: Operator, id: string) =>
    call<{ id: string }>(`${ADMIN}/watch/findings/${encodeURIComponent(id)}/acknowledge`, { method: "POST" }),
  rules: (_op: Operator) => call<{ rules: RuleRow[]; knowledge: KnowledgeRow[] }>(`${ADMIN}/rules`),
  reverifyRule: (_op: Operator, code: string) =>
    call<{ as_of: string; finding_closed: boolean }>(`${ADMIN}/rules/${encodeURIComponent(code)}/reverify`, {
      method: "POST",
      body: JSON.stringify({}),
    }),
  reverifyKnowledge: (_op: Operator, id: string) =>
    call<{ as_of: string; finding_closed: boolean }>(`${ADMIN}/knowledge/${encodeURIComponent(id)}/reverify`, {
      method: "POST",
      body: JSON.stringify({}),
    }),

  invoices: (_op: Operator, status?: string) =>
    call<InvoiceRow[]>(`${ADMIN}/invoices${status ? `?status=${encodeURIComponent(status)}` : ""}`),

  // "submitted" is the queue - what needs a person; "all" is the history.
  // The runtime accepts nothing else, so neither does the signature.
  // The runtime pages at 200 by default; the history asks for its bound
  // explicitly so "the last 50 that closed" is computed over the ledger, not
  // over whatever the default page happened to hold.
  payments: (_op: Operator, status: "submitted" | "all" = "submitted", limit = 200) =>
    call<AdminPayment[]>(`${ADMIN}/payments?status=${status}&limit=${limit}`),
  reviewPayment: (_op: Operator, id: string, body: { verdict: "approved" | "rejected"; note?: string }) =>
    call<PaymentReview>(`${ADMIN}/payments/${encodeURIComponent(id)}/review`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  audit: (_op: Operator, filters: { org_id?: string; event_prefix?: string; limit?: number } = {}) => {
    const q = new URLSearchParams();
    if (filters.event_prefix) q.set("event_prefix", filters.event_prefix);
    if (filters.limit) q.set("limit", String(filters.limit));
    const qs = q.toString();
    // The organisation goes in the path, never the query: the runtime's
    // boot-time route audit refuses an org_id query parameter anywhere.
    const base = filters.org_id
      ? `${ADMIN}/organisations/${encodeURIComponent(filters.org_id)}/audit`
      : `${ADMIN}/audit`;
    return call<AuditRow[]>(`${base}${qs ? `?${qs}` : ""}`);
  },
};
