/**
 * Who is signed in, and whether the runtime agrees they are an operator.
 *
 * The tenant app proves WORKSPACE ownership here and brands the result so a
 * page cannot pass an unproved id to the runtime. This app has one fact to
 * prove instead - "this session belongs to a superadmin" - and it proves it
 * the only way that is honest: by asking the runtime, which reads
 * core.platform_users.is_superadmin on that request. The column's own comment
 * says "never trusted from a JWT claim alone", and there is no claim to trust
 * anyway; nothing in the token says superadmin.
 *
 * `Operator` is branded for the same reason `AuthorizedWorkspace` is: an
 * admin page takes one, only `requireOperator` makes one, so a page that skips
 * the check does not compile.
 */

import "server-only";

import { cache } from "react";
import { redirect } from "next/navigation";

import { supabaseServer } from "@/lib/supabase/server";

declare const brand: unique symbol;

export type Operator = {
  readonly userId: string;
  readonly email: string | null;
  readonly fullName: string | null;
  readonly [brand]: "operator";
};

export type SessionUser = {
  id: string;
  email: string | null;
};

/** `getUser()`, never `getSession()`: the token is verified, not trusted. */
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
 * The access token, for the Authorization header on runtime calls. The only
 * function that touches the raw token, called from exactly one place.
 */
export async function accessToken(): Promise<string | null> {
  const supabase = await supabaseServer();
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token ?? null;
}

export class NotAnOperator extends Error {}

/**
 * Ask the runtime. A 404 from /api/admin/me is the runtime's way of saying
 * "there is nothing here for you", which is exactly the answer a signed-in
 * tenant should get from a console they were never meant to find.
 *
 * Wrapped in cache() so the layout and the page pay for one round trip.
 */
export const currentOperator = cache(async (): Promise<Operator | null> => {
  const user = await currentUser();
  if (!user) return null;
  const token = await accessToken();
  if (!token) return null;

  const base = process.env.AGENT_RUNTIME_URL ?? "http://127.0.0.1:8000";
  const response = await fetch(`${base}/api/admin/me`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (response.status === 404 || response.status === 401 || response.status === 403) return null;
  if (!response.ok) throw new Error(`GET /api/admin/me -> ${response.status}`);
  const me = (await response.json()) as { user_id: string; email: string | null; full_name: string | null };
  return { userId: me.user_id, email: me.email, fullName: me.full_name } as Operator;
});

export async function requireOperator(): Promise<Operator> {
  await requireUser();
  const op = await currentOperator();
  if (!op) redirect("/not-an-operator");
  return op;
}
