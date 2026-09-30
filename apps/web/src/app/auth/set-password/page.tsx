import { redirect } from "next/navigation";

import { setPassword } from "@/app/auth/set-password/actions";
import { MIN_PASSWORD_LENGTH, passwordErrorText } from "@/app/auth/set-password/policy";
import { BrandLockup } from "@/components/BrandLockup";
import { currentUser } from "@/lib/session";

export const metadata = { title: "Choose a password" };

/**
 * The invited owner's first and only sign-up step.
 *
 * /auth/confirm has just exchanged the invitation's token hash for a session,
 * so the person here is signed in to an account that has no password. This
 * page asks for one, twice, and hands the pair to the Server Action in
 * ./actions.ts; the action re-proves the session and calls GoTrue, and the
 * dashboard follows.
 *
 * It requires a session rather than a token: an invitation that was not
 * confirmed, or one that has expired, has no session to set a password on,
 * and /login is where that person is sent - the same place an invitation
 * GoTrue refused lands. Nothing here reads the token hash; it was spent on
 * the way in.
 *
 * Not built, deliberately: a "forgot password" path. It needs email GoTrue
 * can send, and no SMTP is configured anywhere in this product. Until it is,
 * a lost password is a fresh invitation from the operator.
 */

const input =
  "mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900";

export default async function SetPasswordPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string }>;
}) {
  const user = await currentUser();
  if (!user) redirect("/login");
  const { error: code } = await searchParams;
  // A code the action did not issue renders nothing: the URL does not get
  // to write on this page.
  const error = passwordErrorText(code);

  return (
    <div className="mx-auto flex min-h-[60vh] max-w-sm flex-col justify-center space-y-6">
      <BrandLockup />

      <div>
        <h1 className="text-lg font-semibold tracking-tight">Choose a password</h1>
        <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
          You are signed in as <span className="font-medium">{user.email ?? "your account"}</span>.
          Set a password now so you can sign in again later; the invitation link only works once.
        </p>
      </div>

      <form action={setPassword} className="space-y-3">
        <label className="block text-sm">
          <span className="text-slate-600 dark:text-slate-400">Password</span>
          <input
            name="password"
            type="password"
            required
            minLength={MIN_PASSWORD_LENGTH}
            autoComplete="new-password"
            className={input}
          />
          <span className="mt-1 block text-xs text-slate-500">
            At least {MIN_PASSWORD_LENGTH} characters. This account can approve ad spend, so it
            gets a longer minimum than a sign-in form would ask for.
          </span>
        </label>

        <label className="block text-sm">
          <span className="text-slate-600 dark:text-slate-400">Confirm password</span>
          <input
            name="confirm"
            type="password"
            required
            minLength={MIN_PASSWORD_LENGTH}
            autoComplete="new-password"
            className={input}
          />
        </label>

        {error && (
          <p className="rounded-md border border-red-300 bg-red-50 p-2 text-sm dark:border-red-900 dark:bg-red-950/40">
            {error}
          </p>
        )}

        <button
          type="submit"
          className="w-full rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 dark:bg-slate-100 dark:text-slate-900"
        >
          Save password and continue
        </button>
      </form>
    </div>
  );
}
