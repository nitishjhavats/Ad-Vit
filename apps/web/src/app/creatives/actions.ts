"use server";

/**
 * Every mutation the creative studio makes, as a Server Action.
 *
 * Next checks Origin against Host on each of these, which is what keeps them
 * CSRF-safe while the session lives in an httpOnly cookie. Each one re-proves
 * the workspace (`defaultWorkspace`) before calling the runtime, and the
 * runtime re-proves membership again from the database on the request - so
 * what an action contributes is input narrowing and the reply, and nothing
 * that decides.
 *
 * Two of them answer a client component rather than a form. The upload form
 * has to run in the browser, because the file bytes go straight from the
 * browser to Storage on a URL the runtime signed and must never pass through
 * this process; so `signUpload` and `analyseCreative` RETURN their outcome for
 * that component to show, instead of redirecting. The third, `analyseForm`,
 * is the ordinary form-shaped kind and redirects back with `?error=` / `?ok=`
 * the way the console's actions do - it is the "analyse again" button on a
 * creative's own page.
 *
 * Refusals keep the runtime's own words. The 503 for a missing
 * SUPABASE_SERVICE_ROLE_KEY is the one the owner needs to relay verbatim.
 */

import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";

import { NotAuthorized, Refused, RuntimeUnreachable } from "@/lib/api";
import {
  ACCEPTED_TYPES,
  MAX_SIZE_BYTES,
  OBJECTIVES,
  creatives,
  isCreativeId,
  type AcceptedType,
  type Objective,
  type SignedUpload,
} from "@/lib/creatives";
import { defaultWorkspace } from "@/lib/session";

/** What a client component gets back: the value, or the reason in words. */
export type ActionResult<T> = { ok: true; value: T } | { ok: false; error: string };

function explain(e: unknown): string {
  if (e instanceof Refused) return e.detail;
  if (e instanceof RuntimeUnreachable) return "The agent runtime is not running.";
  if (e instanceof NotAuthorized) return "Your session has expired. Sign in again.";
  return e instanceof Error ? e.message : String(e);
}

async function attempt<T>(fn: () => Promise<T>): Promise<ActionResult<T>> {
  try {
    return { ok: true, value: await fn() };
  } catch (e) {
    return { ok: false, error: explain(e) };
  }
}

const isAcceptedType = (v: string): v is AcceptedType =>
  (ACCEPTED_TYPES as readonly string[]).includes(v);
const isObjective = (v: string): v is Objective => (OBJECTIVES as readonly string[]).includes(v);

// ---------------------------------------------------------------------------
// From the upload form (a client component)
// ---------------------------------------------------------------------------

export type SignUploadInput = {
  name: string;
  size: number;
  mime: string;
  /** A SKU in the workspace's catalogue, or empty. The runtime resolves it. */
  productHint: string;
  aiGenerated: boolean;
};

/**
 * Step 1: declare the upload and get the one-shot URL back.
 *
 * The narrowing here mirrors the runtime's `UploadRequest` so a wrong file
 * type is refused with a sentence rather than a 422 body. The runtime still
 * has the last word.
 */
export async function signUpload(input: SignUploadInput): Promise<ActionResult<SignedUpload>> {
  const ws = await defaultWorkspace();
  if (!ws) return { ok: false, error: "This account is not a member of any workspace yet." };

  const name = String(input.name ?? "").trim().slice(0, 255);
  const mime = String(input.mime ?? "");
  const size = Number(input.size);

  if (!name) return { ok: false, error: "The file has no name." };
  if (!isAcceptedType(mime)) {
    return {
      ok: false,
      error: `${mime || "an unknown type"} is not accepted. Upload ${ACCEPTED_TYPES.join(", ")}.`,
    };
  }
  if (!Number.isInteger(size) || size <= 0) return { ok: false, error: "The file is empty." };
  if (size > MAX_SIZE_BYTES) {
    return { ok: false, error: "Larger than 4 GB, which is Meta's ceiling for a video ad." };
  }

  const sku = String(input.productHint ?? "").trim();
  return attempt(() =>
    creatives.declareUpload(ws, {
      original_name: name,
      content_type: mime,
      size_bytes: size,
      product_sku: sku || null,
      ai_generated: Boolean(input.aiGenerated),
    }),
  );
}

/** Step 3, after the browser has PUT the bytes: rate it. */
export async function analyseCreative(
  creativeId: string,
  objective: string,
): Promise<ActionResult<{ creativeId: string }>> {
  const ws = await defaultWorkspace();
  if (!ws) return { ok: false, error: "This account is not a member of any workspace yet." };
  if (!isCreativeId(creativeId)) return { ok: false, error: "That is not a creative id." };
  if (!isObjective(objective)) return { ok: false, error: "Pick an objective." };

  const result = await attempt(() => creatives.analyse(ws, creativeId, objective));
  if (result.ok) {
    revalidatePath("/creatives");
    revalidatePath(`/creatives/${creativeId}`);
    return { ok: true, value: { creativeId } };
  }
  return result;
}

// ---------------------------------------------------------------------------
// From a plain form on /creatives/[id]
// ---------------------------------------------------------------------------

function back(path: string, error?: string, ok?: string): never {
  const q = new URLSearchParams();
  if (error) q.set("error", error);
  if (ok) q.set("ok", ok);
  const qs = q.toString();
  revalidatePath(path);
  redirect(qs ? `${path}?${qs}` : path);
}

export async function analyseForm(formData: FormData) {
  const creativeId = String(formData.get("creative_id") ?? "").trim();
  const objective = String(formData.get("objective") ?? "conversion").trim();
  if (!isCreativeId(creativeId)) redirect("/creatives");
  const path = `/creatives/${creativeId}`;

  const result = await analyseCreative(creativeId, objective);
  if (!result.ok) back(path, result.error);
  back(path, undefined, "analysed");
}
