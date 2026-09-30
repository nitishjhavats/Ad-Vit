/**
 * Who is signed in, and which workspace they have been PROVED to own.
 *
 * The branded type below is the TypeScript half of the same idea as
 * `app/auth/scope.py::AuthorizedWorkspace`, with one real difference: this one
 * is enforced by the compiler. `AuthorizedWorkspace` cannot be constructed
 * outside this module, because its brand is a `unique symbol` that is not
 * exported — so a page that forgets to prove ownership does not fail at
 * runtime, it fails to build.
 *
 * That matters because the alternative is what this codebase had: a
 * `WORKSPACE_ID` constant at the top of two page files, read from an
 * environment variable, identical on every request and belonging to whoever the
 * developer last tested with.
 */

import "server-only";

import { cache } from "react";
import { redirect } from "next/navigation";

import { supabaseServer } from "@/lib/supabase/server";

declare const brand: unique symbol;

export type AuthorizedWorkspace = {
  readonly id: string;
  readonly [brand]: "authorized";
};

export type SessionUser = {
  id: string;
  email: string | null;
};

/**
 * `getUser()`, never `getSession()`.
 *
 * `getSession()` returns whatever is in the cookie without asking the auth
 * server whether it is real, so anyone who can write a cookie can be anyone.
 * `getUser()` verifies the token. It costs a round trip and is wrapped in
 * React's `cache()` so a render that asks three times pays for one.
 */
export const currentUser = cache(async (): Promise<SessionUser | null> => {
  const supabase = await supabaseServer();
  const { data, error } = await supabase.auth.getUser();
  if (error || !data.user) return null;
  return { id: data.user.id, email: data.user.email ?? null };
});

export async function requireUser(): Promise<SessionUser> {
  const user = await currentUser();
  if (!user) redirect("/login");
  return user;
}

/**
 * The workspaces this user may act on, read through PostgREST as the user — so
 * the answer comes from the RLS policies in `supabase/migrations` rather than
 * from a filter written here.
 */
export const myWorkspaces = cache(
  async (): Promise<Array<{ id: string; name: string }>> => {
    const supabase = await supabaseServer();
    const { data, error } = await supabase
      .schema("t_advit")
      .from("workspaces")
      .select("id, name")
      .order("name");
    if (error || !data) return [];
    return data as Array<{ id: string; name: string }>;
  },
);

/**
 * Prove ownership, or redirect.
 *
 * Note what this does NOT do: it does not decide anything. The runtime re-proves
 * membership on every request with `t_advit.is_workspace_member` on its own
 * tenant connection, and would refuse regardless of what this function returned.
 *
 * This exists so the UI can show the right thing and so a page cannot
 * accidentally pass an unproved id to the runtime client — not as the
 * authorization boundary. The boundary is on the other side of the network,
 * where the caller cannot reach it.
 */
export async function requireWorkspace(id: string): Promise<AuthorizedWorkspace> {
  await requireUser();
  const mine = await myWorkspaces();
  if (!mine.some((w) => w.id === id)) redirect("/");
  return { id } as AuthorizedWorkspace;
}

/** The first workspace this user can act on, for the bare "/" and "/chat" routes. */
export async function defaultWorkspace(): Promise<AuthorizedWorkspace | null> {
  await requireUser();
  const [first] = await myWorkspaces();
  return first ? ({ id: first.id } as AuthorizedWorkspace) : null;
}

/**
 * The access token, for the Authorization header on runtime calls.
 *
 * Separate from `currentUser` on purpose: this is the only function that
 * touches the raw token, and it is called from exactly one place.
 */
export async function accessToken(): Promise<string | null> {
  const supabase = await supabaseServer();
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token ?? null;
}
