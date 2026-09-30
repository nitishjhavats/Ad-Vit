/**
 * The reports client: one read, one write.
 *
 * `GET /api/workspaces/{id}/reports` is the read model behind all three tabs -
 * Report, Analytics, Suggestions - so the page fetches it once and the tabs
 * are three renderings of the same moment. `respond` is the one mutation on
 * this surface, and it is the approvals inbox's own route: approving a
 * suggestion here is the same act, on the same row, as approving it on the
 * dashboard.
 *
 * Every numeric field below is `string | number | null`. Postgres numerics
 * arrive as strings, the route's own arithmetic arrives as numbers, and null
 * is an UNKNOWN - a day with no ingested spend, a period with nothing
 * reported. The rendering layer owes each null an em dash and a reason, and
 * must never coerce it to 0.
 */

import "server-only";

import { runtimeCall, type Approval } from "@/lib/api";
import type { AuthorizedWorkspace } from "@/lib/session";

export type Money = string | number | null;

export type MoneyRow = {
  date: string;
  spend_inr: Money;
  delivered_revenue_inr: Money;
  delivered_orders: number | null;
  blended_cac_inr: Money;
  mer: Money;
  contribution_margin_inr: Money;
  rto_rate: Money;
  confirm_rate: Money;
  delivered_aov_inr: Money;
  /** Named inputs the economics engine could not find that day. */
  gaps: string[];
  computed_at: string;
};

export type Totals = {
  spend_inr: number | null;
  delivered_revenue_inr: number | null;
  delivered_orders: number | null;
  blended_cac_inr: number | null;
  mer: number | null;
  contribution_margin_inr: number | null;
  rto_rate: number | null;
  confirm_rate: number | null;
  coverage: {
    period_days: number;
    reported_days: number;
    spend_days: number;
    margin_days: number;
  };
};

export type Direction = "up" | "down" | "flat";

export type Comparison = {
  this: number | null;
  previous: number | null;
  change_pct: number | null;
  /** null when either side is unknown: a direction needs two numbers. */
  direction: Direction | null;
  /** Which way is good. null for spend, which is not good or bad on its own. */
  better_when: "up" | "down" | null;
};

export type Learning = {
  id: string;
  tier: "account" | "industry" | "global";
  statement: string;
  confidence: string | number;
  evidence_n: number;
  status: "active" | "contested";
  effect_size: Money;
  updated: string;
};

export type Outcome = {
  id: string;
  decision_id: string;
  decision_type: string;
  chosen_option: string | null;
  verdict: "beat" | "met" | "missed" | "unmeasurable";
  horizon_days: number;
  measured_at: string;
  metric: string | null;
  metric_label: string | null;
  predicted_direction: "up" | "down" | null;
  better_when: "up" | "down" | null;
  before_value: number | null;
  after_value: number | null;
  delta: number | null;
  target: number | null;
  notes: string | null;
};

/**
 * The strategy model's proposal as it said it, held at the CTA gate. It has
 * no destination - that is exactly what it lacks - so nothing here can be
 * approved or executed; when the owner answers, the next chat turn re-proposes
 * it with the destination written into the action.
 */
export type HeldOption = {
  label: string;
  what: string;
  expected_effect?: string;
  risk?: string;
  cost_of_being_wrong?: string;
  action?: { tool: string; ad_account_id?: string; params?: Record<string, unknown> };
};

export type HeldProposalBody = {
  goal: string;
  assumptions?: string[];
  options: HeldOption[];
  recommended: string;
  single_strongest_reason?: string;
  questions?: string[];
};

/** The CTA model's own argument, from `CTARecommendation.as_dict()`. */
export type CtaRecommendation = {
  recommended: string;
  recommended_label?: string;
  ranking?: string[];
  rationale?: string[];
  requirements?: Array<{ item: string; satisfied: boolean; detail: string }>;
  warnings?: string[];
  qualifying_questions?: string[];
};

/**
 * The one open row in `t_advit.held_proposals` for this workspace, or the
 * same keys with `held: false` when the table was checked and nothing is
 * open. `held` is a bool, never null: the runtime can answer for it now.
 */
export type CtaGate =
  | {
      held: true;
      id: string;
      run_id: string | null;
      proposal: HeldProposalBody;
      question: string;
      reason: string;
      recommendation: CtaRecommendation | null;
      held_at: string;
    }
  | {
      held: false;
      id: null;
      run_id: null;
      proposal: null;
      question: null;
      reason: string;
      recommendation: null;
      held_at: null;
    };

export type Report = {
  period: { from: string; to: string; days: number };
  money: MoneyRow[];
  totals: Totals;
  week_over_week: {
    windows: { this: { from: string; to: string }; previous: { from: string; to: string } };
    spend_inr: Comparison;
    blended_cac_inr: Comparison;
    mer: Comparison;
  };
  learnings: Learning[];
  entitlements: { industry_intelligence: boolean };
  outcomes: Outcome[];
  suggestions: {
    pending_approvals: Approval[];
    cta_gate: CtaGate;
  };
  note: string;
};

export type ApprovalAction = "approve" | "reject";

export type ApprovalResult = {
  id: string;
  status: string;
  execution?: {
    attempted: boolean;
    reason?: string | null;
    decision?: string;
    message?: string | null;
    verified?: boolean;
  };
};

export const reports = {
  get: (ws: AuthorizedWorkspace, days = 30) =>
    runtimeCall<Report>(`/api/workspaces/${ws.id}/reports?days=${days}`),

  // Keyed by the approval, the way `api.audit` is keyed by the ad account:
  // the runtime resolves which workspace owns it through `authorized_approval`
  // on the tenant connection, and a row that is not this caller's is a 404.
  // There is no workspace to pass because there is no workspace in the path;
  // inventing one here would be a parameter the runtime never reads.
  respond: (approvalId: string, action: ApprovalAction, reason: string | null) =>
    runtimeCall<ApprovalResult>(`/api/approvals/${approvalId}/respond`, {
      method: "POST",
      body: JSON.stringify({ action, reason }),
    }),
};
