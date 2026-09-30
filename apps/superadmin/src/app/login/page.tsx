import { redirect } from "next/navigation";

import { BrandLockup } from "@/components/BrandLockup";
import { currentUser } from "@/lib/session";
import { supabaseServer } from "@/lib/supabase/server";

export const metadata = { title: "Sign in" };

/**
 * Same Supabase Auth as the tenant app, same Server Action shape, same one
 * message for every failure. Being a superadmin is not decided here or
 * anywhere in this app: it is a row the runtime reads on each request.
 */
async function signIn(formData: FormData) {
  "use server";

  const email = String(formData.get("email") ?? "").trim();
  const password = String(formData.get("password") ?? "");
  const next = String(formData.get("next") ?? "/");

  const supabase = await supabaseServer();
  const { error } = await supabase.auth.signInWithPassword({ email, password });
  if (error) redirect(`/login?error=1&next=${encodeURIComponent(next)}`);

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
      <p className="text-sm text-slate-600 dark:text-slate-400">
        Operator console. Sign in with your platform account.
      </p>
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
            Those details did not match an account.
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
