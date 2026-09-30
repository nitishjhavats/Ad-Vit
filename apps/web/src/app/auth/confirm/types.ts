/**
 * Which token-hash flows a link of ours may carry. Its own module because a
 * `"use server"` file may export only async functions, and the page and the
 * action both need this check.
 */

import type { EmailOtpType } from "@supabase/supabase-js";

/**
 * The email flows GoTrue verifies from a token hash and that a link of ours
 * might carry. `invite` is the only one the runtime composes today; the
 * other three are the shapes a recovery or a magic link would take.
 */
const ACCEPTED_TYPES: ReadonlySet<string> = new Set(["invite", "recovery", "magiclink", "email"]);

export function isAcceptedType(value: string | null | undefined): value is EmailOtpType {
  return typeof value === "string" && ACCEPTED_TYPES.has(value);
}

