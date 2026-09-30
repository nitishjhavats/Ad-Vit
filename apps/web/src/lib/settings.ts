/**
 * The settings client: what an organisation configures about itself.
 *
 * Server-side only, through `runtimeCall`, so every request carries the
 * caller's own token and this process holds nothing that could act for
 * anybody. Every workspace-scoped method takes an `AuthorizedWorkspace` that
 * only `lib/session.ts` can construct - a page that has not proved ownership
 * cannot spell a call here, and `__type_tests__/settings.ts` keeps it so.
 *
 * Two things this module refuses to do:
 *
 *   * It never has the OpenRouter key on the way back. The runtime's GET
 *     reports presence, the last four characters and whether the key has ever
 *     worked; the key itself goes runtime-ward inside `storeKey` and is not
 *     part of any type this file exports.
 *   * It never writes the CTA decision itself. The one Supabase read here is
 *     the on-file destination, under RLS as the signed-in user, because the
 *     runtime has no GET for it yet; the write goes through `PUT .../cta`,
 *     which supersedes the previous decision and writes the audit row. A
 *     PostgREST insert would do neither.
 */

import "server-only";

import { runtimeCall } from "@/lib/api";
import { supabaseServer } from "@/lib/supabase/server";
import type { AuthorizedWorkspace } from "@/lib/session";

// ---------------------------------------------------------------------------
// Bring your own key
// ---------------------------------------------------------------------------

/** One row of `core.org_secrets` as the runtime reports it. Never the key. */
export type SecretStatus = {
  kind: string;
  hint: string | null;
  key_version: number;
  created_at: string;
  rotated_at: string | null;
  last_verified_at: string | null;
  last_error: string | null;
  is_working: boolean;
};

export type ByokStatus = { secrets: SecretStatus[] };

/** The `kind` under which the runtime stores the OpenRouter key. */
export const OPENROUTER_KIND = "openrouter_api_key";

// ---------------------------------------------------------------------------
// Model tiers, per function
// ---------------------------------------------------------------------------

export type ModelTier = "best" | "value" | "cheap";

export type ModelFunction = {
  key: string;
  /**
   * The name `PUT .../settings/models` expects. The runtime's catalogue does
   * not carry it yet (`Tier.as_dict` omits it), so the page falls back to
   * `key`, which matches the role for every function except chat.
   */
  role?: string;
  label: string;
  recommended: ModelTier;
  why_recommended: string;
  fixed: boolean;
  options: Array<{ tier: ModelTier; note: string; recommended: boolean }>;
  /** Absent when nobody has set it: "use whatever is recommended". */
  chosen: ModelTier | null;
};

export type ModelSettings = { functions: ModelFunction[]; note: string };

export type TierChosen = { role: string; tier: ModelTier; model: string; class: string };

// ---------------------------------------------------------------------------
// Where campaigns send people
// ---------------------------------------------------------------------------

/**
 * The four destinations, by the canonical value the campaign builder uses
 * (`app/agents/cta_model.py::Destination`). The page submits these values;
 * each is itself an accepted spelling in `cta_gate.ACCEPTED`, so a round trip
 * through the runtime returns exactly what was sent.
 */
export const CTA_DESTINATIONS = [
  {
    value: "click_to_call",
    label: "Click to Call",
    hint: "The ad opens the dialler. Your sales team has to pick up.",
  },
  {
    value: "click_to_whatsapp",
    label: "Click to WhatsApp",
    hint: "The ad opens a WhatsApp thread with your business number.",
  },
  {
    value: "meta_instant_form",
    label: "Meta Instant Form",
    hint: "A lead form inside Meta. Someone has to call the leads back.",
  },
  {
    value: "landing_page",
    label: "Landing Page",
    hint: "A page on your website, measured through the pixel.",
  },
] as const;

export type CtaDestination = (typeof CTA_DESTINATIONS)[number]["value"];

/**
 * Every spelling the runtime accepts, mapped to the canonical value - the
 * same table as `cta_gate.ACCEPTED`, so an on-file row written as "call" or
 * "website" is shown under the same label the gate resolves it to.
 */
const ACCEPTED_SPELLINGS: Record<string, CtaDestination> = {
  call: "click_to_call",
  click_to_call: "click_to_call",
  whatsapp: "click_to_whatsapp",
  click_to_whatsapp: "click_to_whatsapp",
  lead_form: "meta_instant_form",
  instant_form: "meta_instant_form",
  meta_instant_form: "meta_instant_form",
  landing_page: "landing_page",
  website: "landing_page",
};

export function canonicalDestination(raw: unknown): CtaDestination | null {
  if (typeof raw !== "string") return null;
  return ACCEPTED_SPELLINGS[raw.trim().toLowerCase()] ?? null;
}

export type CtaOnFile = {
  destination: CtaDestination;
  /** The raw spelling as stored, for the record beside the label. */
  stored_as: string;
  asserted_at: string;
};

/**
 * What recording a destination returns. `released_proposal` is the open
 * `t_advit.held_proposals` row this answer closed, or null when nothing was
 * waiting - an owner may set a destination before ever asking for a
 * campaign. The runtime does not re-run the released proposal: it was made
 * without a destination, and the next chat turn re-proposes with one.
 */
export type CtaChosen = {
  destination: string;
  account_context_id: string;
  released_proposal: {
    id: string;
    run_id: string | null;
    question: string;
    held_at: string;
    goal: string | null;
    resolved_by: "cta_set";
  } | null;
};

// ---------------------------------------------------------------------------

export const settings = {
  byok: (ws: AuthorizedWorkspace) =>
    runtimeCall<ByokStatus>(`/api/workspaces/${ws.id}/settings/byok`),

  /**
   * The key travels in this request body and nowhere else: not in a redirect,
   * not in a log line, not in the value returned. The response is the
   * runtime's status row for the stored key - hint and version, never the key.
   */
  storeKey: (ws: AuthorizedWorkspace, apiKey: string) =>
    runtimeCall<{ stored: SecretStatus }>(`/api/workspaces/${ws.id}/settings/byok`, {
      method: "PUT",
      body: JSON.stringify({ api_key: apiKey }),
    }),

  models: (ws: AuthorizedWorkspace) =>
    runtimeCall<ModelSettings>(`/api/workspaces/${ws.id}/settings/models`),

  chooseTier: (ws: AuthorizedWorkspace, role: string, tier: string) =>
    runtimeCall<TierChosen>(`/api/workspaces/${ws.id}/settings/models`, {
      method: "PUT",
      body: JSON.stringify({ role, tier }),
    }),

  chooseCta: (ws: AuthorizedWorkspace, destination: string, reason: string | null) =>
    runtimeCall<CtaChosen>(`/api/workspaces/${ws.id}/cta`, {
      method: "PUT",
      body: JSON.stringify({ destination, reason }),
    }),

  /**
   * The owner's current decision, read the way `cta_gate.on_file` reads it:
   * `(sales_operation, primary_cta)`, `owner_asserted` only, still valid. The
   * seed also carries an `inferred` row and that is exactly what this filter
   * leaves out - an inference is what the decision replaces.
   *
   * Read through PostgREST as the signed-in user, so the answer comes from
   * `account_context_select` in the migrations rather than from a filter
   * written here.
   */
  ctaOnFile: async (ws: AuthorizedWorkspace): Promise<CtaOnFile | null> => {
    const supabase = await supabaseServer();
    const { data, error } = await supabase
      .schema("t_advit")
      .from("account_context")
      .select("value_json, valid_from")
      .eq("workspace_id", ws.id)
      .eq("dimension", "sales_operation")
      .eq("key", "primary_cta")
      .eq("source", "owner_asserted")
      .is("valid_to", null)
      .order("valid_from", { ascending: false })
      .limit(1);
    // An error is not an empty result. Folding the two together rendered
    // "Not set" over an RLS denial or an unexposed schema, which is the
    // permissive-guard shape this codebase keeps meeting.
    if (error) throw new Error(`account_context read failed: ${error.message}`);
    if (!data || data.length === 0) return null;

    const row = data[0] as { value_json: unknown; valid_from: string };
    const destination = canonicalDestination(row.value_json);
    if (destination === null) return null;
    return { destination, stored_as: String(row.value_json), asserted_at: row.valid_from };
  },
};
