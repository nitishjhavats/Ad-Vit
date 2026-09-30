"use server";

/**
 * Every mutation the settings page makes, as a Server Action.
 *
 * Next checks Origin against Host on each of these, which is what makes them
 * CSRF-safe while the session lives in an httpOnly cookie. Each one re-proves
 * the workspace (`requireWorkspace`) before calling the runtime, and the
 * runtime re-proves membership again from the database on the request - so
 * the action here contributes the form parsing and the redirect, and nothing
 * that decides.
 *
 * Refusals come back as `?error=` on the page rather than as a thrown error,
 * so the reason the runtime gave - "not a customer choice", "secret storage
 * is not configured", "'phone' is not a destination" - is shown beside the
 * form that caused it. The one field that never rides a redirect is the
 * OpenRouter key: it goes into the request body of `settings.storeKey` and is
 * not read again.
 */

import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";

import { NotAuthorized, Refused, RuntimeUnreachable } from "@/lib/api";
import { requireWorkspace } from "@/lib/session";
import { settings } from "@/lib/settings";

const PAGE = "/settings";

function back(error?: string, ok?: string): never {
  const q = new URLSearchParams();
  if (error) q.set("error", error);
  if (ok) q.set("ok", ok);
  const qs = q.toString();
  revalidatePath(PAGE);
  redirect(qs ? `${PAGE}?${qs}` : PAGE);
}

async function attempt<T>(ok: string | ((result: T) => string), fn: () => Promise<T>): Promise<never> {
  let result: T;
  try {
    result = await fn();
  } catch (e) {
    // A 403 arrives as Refused with the runtime's own sentence ("only an
    // owner or an admin may change this"), which is what the owner sees.
    if (e instanceof Refused) back(e.detail);
    // Only a missing or expired session is NotAuthorized now. The message
    // carries the workspace id and the route; neither belongs in a URL.
    if (e instanceof NotAuthorized) back("your session has expired; sign in again");
    if (e instanceof RuntimeUnreachable) back("the agent runtime is not reachable; nothing was changed");
    back(e instanceof Error ? e.message : String(e));
  }
  // The ok sentence may depend on what the runtime said happened - the
  // destination action reads whether a held proposal was released.
  back(undefined, typeof ok === "function" ? ok(result) : ok);
}

const str = (fd: FormData, k: string) => String(fd.get(k) ?? "").trim();

export async function storeKey(formData: FormData) {
  const ws = await requireWorkspace(str(formData, "workspace_id"));
  const apiKey = str(formData, "api_key");
  if (!apiKey) back("paste the key before saving");
  // The one field that must never appear in a URL or a log. The runtime's
  // 422 for a malformed key echoes the input in pydantic's detail, so the
  // length rule is checked here first and a 422 from the runtime is reported
  // without its body.
  if (apiKey.length < 16 || apiKey.length > 512) back("the key must be between 16 and 512 characters");
  try {
    await settings.storeKey(ws, apiKey);
  } catch (e) {
    if (e instanceof Refused && e.status === 422) back("the runtime rejected the key as malformed");
    if (e instanceof Refused) back(e.detail);
    if (e instanceof NotAuthorized) back("your session has expired; sign in again");
    if (e instanceof RuntimeUnreachable) back("the agent runtime is not reachable; the key was not stored");
    back("the key could not be stored");
  }
  back(undefined, "key stored; model calls are now billed to your OpenRouter account");
}

export async function chooseTier(formData: FormData) {
  const ws = await requireWorkspace(str(formData, "workspace_id"));
  const role = str(formData, "role");
  const tier = str(formData, "tier");
  const label = str(formData, "label") || role;
  return attempt(`${label} will use the ${tier} tier from the next call`, () =>
    settings.chooseTier(ws, role, tier),
  );
}

export async function chooseCta(formData: FormData) {
  const ws = await requireWorkspace(str(formData, "workspace_id"));
  const destination = str(formData, "destination");
  const reason = str(formData, "reason") || null;
  return attempt(
    (chosen) =>
      chosen.released_proposal
        ? `destination recorded; the held proposal "${chosen.released_proposal.goal ?? chosen.released_proposal.question}" is released - ask for it again in Chat and it will be built to send people there`
        : "destination recorded; campaigns built from now on send people there",
    () => settings.chooseCta(ws, destination, reason),
  );
}
