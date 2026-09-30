"use server";

/**
 * The one mutation the sign-up page makes: give the invited owner's account a
 * password.
 *
 * A Server Action, so Next checks Origin against Host and the httpOnly
 * session cookie set by /auth/confirm cannot be spent from someone else's
 * page. The session is re-proved here (`requireUser`, which is `getUser()`
 * against the auth server, not a cookie read) rather than trusted from the
 * page that rendered the form, because the form is what a browser sends and
 * this is what decides.
 *
 * Refusals come back as `?error=<code>` on the page rather than as a thrown
 * error, and the code is one of the keys in ./policy.ts - never a sentence.
 * A sentence in the URL is a sentence anyone can put there, and GoTrue's own
 * wording ("New password should be different from the old password") is
 * logged here, without the password, where the person reading it is the
 * operator and not whoever holds the link. The password itself never rides a
 * redirect: it goes into `updateUser`'s request body and is not read again.
 */

import { redirect } from "next/navigation";

import { MIN_PASSWORD_LENGTH, type PasswordErrorCode } from "@/app/auth/set-password/policy";
import { requireUser } from "@/lib/session";
import { supabaseServer } from "@/lib/supabase/server";

const PAGE = "/auth/set-password";

function back(code: PasswordErrorCode): never {
  redirect(`${PAGE}?error=${code}`);
}

export async function setPassword(formData: FormData) {
  await requireUser();

  const password = String(formData.get("password") ?? "");
  const confirm = String(formData.get("confirm") ?? "");

  // The browser's minLength is a convenience; this is the check. A form can
  // be posted without the attribute ever having run.
  if (password.length < MIN_PASSWORD_LENGTH) back("short");
  if (password !== confirm) back("mismatch");

  const supabase = await supabaseServer();
  const { error } = await supabase.auth.updateUser({ password });
  if (error) {
    // GoTrue is the only party that knows why. Its sentence goes to the
    // server log - the message names no password - and the owner gets the
    // fixed one.
    console.error(`set-password: auth service refused: ${error.message}`);
    back("refused");
  }

  // The dashboard: defaultWorkspace() finds the workspace the operator created
  // alongside this account, so the owner lands on their own data.
  redirect("/");
}
