/**
 * The creative studio's runtime client.
 *
 * Server-side only, like `lib/api.ts`, and built on its `runtimeCall` so the
 * token handling and the error taxonomy (Refused / RuntimeUnreachable /
 * NotAuthorized) are the same here as everywhere else. What this file adds is
 * the shape of the five creative routes and the one rule they all share: a
 * workspace is an `AuthorizedWorkspace`, never a string. A page that has not
 * proved ownership cannot spell a call to this module.
 *
 * What it refuses: a creative id that is not a UUID. The id is interpolated
 * into a path, and the runtime would answer 422 to `../rubric` anyway - but a
 * guard that validates here means the malformed case never leaves this
 * process, and `[id]/page.tsx` can answer 404 without a round trip.
 *
 * The file itself never comes through here. The runtime signs a one-shot
 * Storage URL and the browser PUTs the bytes to it (see
 * `app/creatives/UploadForm.tsx`); this module only carries the declaration
 * and, later, the request to analyse.
 */

import "server-only";

import { runtimeCall } from "@/lib/api";
import type { AuthorizedWorkspace } from "@/lib/session";

// ---------------------------------------------------------------------------
// The contract, as the runtime states it in app/routes_creative.py
// ---------------------------------------------------------------------------

/** The bucket's `allowed_mime_types`; anything else is refused at declaration. */
export const ACCEPTED_TYPES = [
  "video/mp4",
  "video/quicktime",
  "video/webm",
  "image/jpeg",
  "image/png",
  "image/webp",
] as const;
export type AcceptedType = (typeof ACCEPTED_TYPES)[number];

/** Meta's ceiling for a video ad, and the bucket's `file_size_limit`. */
export const MAX_SIZE_BYTES = 4 * 1024 * 1024 * 1024;

export const OBJECTIVES = ["awareness", "consideration", "conversion"] as const;
export type Objective = (typeof OBJECTIVES)[number];

export type CreativeStatus = "uploaded" | "analysing" | "analysed" | "failed";

export type ComplianceVerdict = "pass" | "warn" | "block" | "not_evaluated";

export type UploadDeclaration = {
  original_name: string;
  content_type: AcceptedType;
  size_bytes: number;
  product_sku: string | null;
  ai_generated: boolean | null;
};

/** What `POST .../creatives/uploads` returns: the row's id and where to PUT. */
export type SignedUpload = {
  creative_id: string;
  upload: {
    /** Absolute. The Storage token is a query parameter on it, not a header. */
    url: string;
    method: "PUT";
    /** Exactly the Content-Type the upload was declared with. */
    headers: { "Content-Type": AcceptedType };
  };
  next: string;
};

export type CreativeSummary = {
  id: string;
  original_name: string | null;
  media_type: string;
  status: CreativeStatus;
  created_at: string;
  analysed_at: string | null;
  analysis_error: string | null;
  /** `rating_json ->> 'overall'`: text from SQL, so a string even when numeric. */
  overall: string | null;
  compliance_verdict: ComplianceVerdict | null;
  product_sku: string | null;
};

/** One measured criterion. `passed: null` means the file did not say. */
export type Measured = {
  key: string;
  passed: boolean | null;
  observed: string | null;
  note: string | null;
};

/** One judged criterion: the score AND the sentence the owner can disagree with. */
export type Judged = {
  key: string;
  score: number;
  reason: string;
};

export type ComplianceFinding = {
  rule: string;
  severity: string;
  field: string;
  span: string | null;
  title: string;
  suggested_rewrite: string | null;
};

/** `Rating.as_dict()` in app/creative/analyse.py. */
export type Rating = {
  rubric_version: string;
  /** 0-100, weighted over what was judged; null when nothing was. */
  overall: number | null;
  measured: Measured[];
  judged: Judged[];
  on_screen_text: string;
  what_is_sold: string;
  strongest: string;
  weakest: string;
  rewrite: string;
  compliance: {
    verdict: ComplianceVerdict;
    findings: ComplianceFinding[];
    stages_not_fully_checked: unknown;
    checked_against: string;
  } | null;
  history: {
    available: boolean;
    reason?: string;
    window_days?: number;
    creatives: Array<{
      creative_id: string;
      name: string | null;
      overall_rating: number | null;
      spend_inr: number;
      results: number;
      cost_per_result_inr: number | null;
    }>;
  };
  limitations: string[];
  comparisons_unavailable: string[];
  model: string | null;
  cost_inr: number | null;
};

export type Creative = Omit<CreativeSummary, "overall"> & {
  rating_json: Rating | null;
  ai_generated: boolean | null;
};

export type RubricCriterion = {
  key: string;
  label: string;
  kind: "measured" | "judged";
  weight: number;
  why: string;
};

export type Rubric = {
  version: string;
  criteria: RubricCriterion[];
};

export type AnalyseResult = {
  creative_id: string;
  status: "analysed";
  rating: Rating;
};

// ---------------------------------------------------------------------------

/**
 * Timestamps as the owner reads them. The product is Indian and the pages are
 * rendered on a server whose clock zone is whatever the container's is, so the
 * zone is stated rather than inherited. Both creative pages use it.
 */
export function when(iso: string): string {
  return new Date(iso).toLocaleString("en-IN", {
    timeZone: "Asia/Kolkata",
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isCreativeId(value: string): boolean {
  return UUID.test(value);
}

export class NotACreativeId extends Error {
  constructor(value: string) {
    super(`${JSON.stringify(value.slice(0, 80))} is not a creative id`);
  }
}

function idPath(ws: AuthorizedWorkspace, creativeId: string): string {
  if (!isCreativeId(creativeId)) throw new NotACreativeId(creativeId);
  return `/api/workspaces/${ws.id}/creatives/${creativeId}`;
}

export const creatives = {
  list: (ws: AuthorizedWorkspace) =>
    runtimeCall<CreativeSummary[]>(`/api/workspaces/${ws.id}/creatives`),

  rubric: (ws: AuthorizedWorkspace) =>
    runtimeCall<Rubric>(`/api/workspaces/${ws.id}/creatives/rubric`),

  get: (ws: AuthorizedWorkspace, creativeId: string) =>
    runtimeCall<Creative>(idPath(ws, creativeId)),

  /** Step 1 of an upload: the row is written and a one-shot URL comes back. */
  declareUpload: (ws: AuthorizedWorkspace, declaration: UploadDeclaration) =>
    runtimeCall<SignedUpload>(`/api/workspaces/${ws.id}/creatives/uploads`, {
      method: "POST",
      body: JSON.stringify(declaration),
    }),

  /**
   * Step 3: rate it. Ten to sixty seconds, because the runtime pulls the file
   * down for ffmpeg and then asks the organisation's own model tier. A 503
   * means the runtime has no SUPABASE_SERVICE_ROLE_KEY; its `detail` says so
   * and is shown verbatim, because there is nothing the owner can do here
   * except tell the operator exactly that.
   */
  analyse: (ws: AuthorizedWorkspace, creativeId: string, objective: Objective) =>
    runtimeCall<AnalyseResult>(`${idPath(ws, creativeId)}/analyse`, {
      method: "POST",
      body: JSON.stringify({ objective }),
    }),
};
