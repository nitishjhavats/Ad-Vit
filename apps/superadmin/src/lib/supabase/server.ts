/**
 * The Supabase client for server code.
 *
 * Anon key only. `SUPABASE_SERVICE_ROLE_KEY` must never be read here — it
 * carries BYPASSRLS on a cluster this product shares with an unrelated HRMS
 * database, and a page that used it would return the right data for the wrong
 * reason and keep doing so after the policies changed.
 *
 * There is deliberately no browser client. The access token stays in an
 * httpOnly cookie and in this server's memory; nothing hands it to JavaScript,
 * so an XSS in the dashboard cannot become a stealable, replayable credential
 * for every tenant route. The cost of that choice is stated where it is paid:
 * no realtime subscriptions, and a server round trip per interaction.
 */

import { createServerClient } from "@supabase/ssr";
import { cookies } from "next/headers";

import { SESSION_COOKIE } from "@/lib/supabase/cookie";

const SUPABASE_URL =
  process.env.NEXT_PUBLIC_SUPABASE_URL ??
  process.env.SUPABASE_URL ??
  "http://127.0.0.1:54321";

const SUPABASE_ANON_KEY =
  process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? process.env.SUPABASE_ANON_KEY ?? "";

/**
 * `cookies()` is async in Next 16, and the returned store is only writable
 * inside a Server Action or a Route Handler. During a Server Component render
 * a `set` throws, which is expected and harmless: the proxy has already
 * refreshed the session on the way in, so the failed write is a duplicate of one
 * that already happened. Swallowing it anywhere else would hide a real problem,
 * which is why the catch says so rather than being empty.
 */
export async function supabaseServer() {
  const store = await cookies();

  return createServerClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
    cookieOptions: SESSION_COOKIE,
    cookies: {
      getAll: () => store.getAll(),
      setAll: (toSet) => {
        try {
          toSet.forEach(({ name, value, options }) => store.set(name, value, options));
        } catch {
          // Server Component render: not writable, and the proxy has already
          // written the refreshed session on this request.
        }
      },
    },
  });
}
