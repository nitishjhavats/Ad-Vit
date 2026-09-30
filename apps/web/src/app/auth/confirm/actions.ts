"use server";

/**
 * The exchange: a one-time token hash becomes a session cookie.
 *
 * A Server Action rather than a GET route handler, and the difference is the
 * whole point. An invitation link is pasted into WhatsApp, Gmail or Outlook,
 * and the first thing that fetches it is that client's link-preview or
 * Safe-Links crawler - not the owner. A handler that spent the hash on GET
 * spent it on the crawler, and the owner then read "this link has been
 * used" with no way to get another (GoTrue refuses a second invite for an
 * existing address). Crawlers do not submit forms. The page renders a
 * Continue button; only a person's click reaches this file.
 *
 * Server Actions may set cookies, so `@supabase/ssr` writes the httpOnly
 * session here through `cookies()` exactly as it would have in a handler.
 * The refusals are one word on /login so the URL says nothing about which
 * check failed, and the hash is never logged: a hash in a log line is a
 * sign-in in a log line.
 */

import { redirect } from "next/navigation";

import { isAcceptedType } from "@/app/auth/confirm/types";
import { supabaseServer } from "@/lib/supabase/server";

export async function confirmInvitation(formData: FormData) {
  const tokenHash = String(formData.get("token_hash") ?? "");
  const type = String(formData.get("type") ?? "");

  // Nothing to verify, or a type we would not have composed: refuse before
  // touching the auth service. `redirect` throws, so it sits outside any try.
  if (!tokenHash || !isAcceptedType(type)) redirect("/login?error=invite");

  const supabase = await supabaseServer();
  const { error } = await supabase.auth.verifyOtp({ type, token_hash: tokenHash });

  // GoTrue said no: unknown, expired or already used. To the person holding
  // the link the next step is the same either way - ask the operator for a
  // fresh one - so the reason stays out of the URL.
  if (error) redirect("/login?error=invite");

  // The session cookie is on this response. The account has no password
  // yet; the dashboard would sign them in once and never again.
  redirect("/auth/set-password");
}
