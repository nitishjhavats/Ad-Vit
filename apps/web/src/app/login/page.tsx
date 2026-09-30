import { redirect } from "next/navigation";

import { BrandLockup } from "@/components/BrandLockup";
import { currentUser } from "@/lib/session";
import { supabaseServer } from "@/lib/supabase/server";

export const metadata = { title: "Sign in" };

/**
 * A Server Action, not a route handler.
 *
 * Next 16 checks Origin against Host for Server Actions; route handlers get
 * none of that. The session cookie is `sameSite=lax`, which stops it riding
 * along on cross-site subresource requests, and between the two this form
 * cannot be submitted from somebody else's page. That is the CSRF cost of
 * keeping the token in an httpOnly cookie instead of handing it to JavaScript,
 * paid here rather than assumed away.
 */
async function signIn(formData: FormData) {
  "use server";

  const email = String(formData.get("email") ?? "").trim();
  const password = String(formData.get("password") ?? "");
  const next = String(formData.get("next") ?? "/");

  const supabase = await supabaseServer();
  const { error } = await supabase.auth.signInWithPassword({ email, password });

  if (error) {
    // One message for every failure. "No such user" versus "wrong password" is
    // an account-enumeration oracle, and it is the same reasoning as the
    // runtime returning 404 rather than 403 for a workspace that is not yours.
    redirect(`/login?error=1&next=${encodeURIComponent(next)}`);
  }

  // Only ever to a path on this site. An open redirect here would let a
  // phishing link land on a real login form and bounce to a fake dashboard.
  redirect(next.startsWith("/") && !next.startsWith("//") ? next : "/");
}

async function signOutAction() {
  "use server";
  const supabase = await supabaseServer();
  await supabase.auth.signOut();
  redirect("/login");
}

export { signOutAction };

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string; next?: string }>;
}) {
  const { error, next } = await searchParams;

  if (await currentUser()) redirect(next ?? "/");

  return (
    <div className="mx-auto flex min-h-[60vh] max-w-sm flex-col justify-center space-y-6">
      <BrandLockup />

      <form action={signIn} className="space-y-3">
        <input type="hidden" name="next" value={next ?? "/"} />

        <label className="block text-sm">
          <span className="text-slate-600 dark:text-slate-400">Email</span>
          <input
            name="email"
            type="email"
            required
            autoComplete="username"
            className="mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
          />
        </label>

        <label className="block text-sm">
          <span className="text-slate-600 dark:text-slate-400">Password</span>
          <input
            name="password"
            type="password"
            required
            autoComplete="current-password"
            className="mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
          />
        </label>

        {error && (
          <p className="rounded-md border border-red-300 bg-red-50 p-2 text-sm dark:border-red-900 dark:bg-red-950/40">
            {/* `invite` is set by /auth/confirm when the token hash was missing,
                refused or spent; every other value is a failed sign-in. */}
            {error === "invite"
              ? "That invitation link is invalid or has been used."
              : "Those details did not match an account."}
          </p>
        )}

        <button
          type="submit"
          className="w-full rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 dark:bg-slate-100 dark:text-slate-900"
        >
          Sign in
        </button>
      </form>
    </div>
  );
}
