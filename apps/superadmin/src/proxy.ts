/**
 * Session refresh and an anonymous-traffic bounce. Identical in purpose to
 * apps/web/src/proxy.ts and, like it, NOT the authorization boundary: that is
 * `authorized_superadmin` on the agent runtime, which reads
 * core.is_superadmin() from the database on every request and answers 404 to
 * anyone else. This file only keeps the cookie fresh and sends a signed-out
 * visitor to /login.
 *
 * What it deliberately does not do is decide who is an operator. A signed-in
 * tenant who types this hostname gets through here, reaches the layout, and is
 * told by the runtime that there is nothing at /api/admin/me for them. That
 * answer comes from a row in core.platform_users, not from a claim in a cookie.
 */

import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";

import { SESSION_COOKIE } from "@/lib/supabase/cookie";

const PUBLIC_PREFIXES = ["/login", "/auth", "/_next", "/favicon.ico"];

export async function proxy(request: NextRequest) {
  let response = NextResponse.next({ request });

  const supabase = createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL ?? "http://127.0.0.1:54321",
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? "",
    {
      cookieOptions: SESSION_COOKIE,
      cookies: {
        getAll: () => request.cookies.getAll(),
        setAll: (toSet) => {
          toSet.forEach(({ name, value }) => request.cookies.set(name, value));
          response = NextResponse.next({ request });
          toSet.forEach(({ name, value, options }) =>
            response.cookies.set(name, value, options),
          );
        },
      },
    },
  );

  // getUser(), never getSession(): the latter trusts the cookie without asking
  // the auth server. Calling it here is also what triggers the refresh.
  const {
    data: { user },
  } = await supabase.auth.getUser();

  const path = request.nextUrl.pathname;
  const isPublic = PUBLIC_PREFIXES.some((p) => path === p || path.startsWith(`${p}/`));

  if (!user && !isPublic) {
    const url = request.nextUrl.clone();
    url.pathname = "/login";
    url.searchParams.set("next", path + request.nextUrl.search);
    return NextResponse.redirect(url);
  }

  return response;
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico|.*\.(?:svg|png|jpg|jpeg|gif|webp)$).*)"],
};
