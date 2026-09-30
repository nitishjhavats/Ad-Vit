/**
 * Session refresh and an anonymous-traffic bounce.
 *
 * `middleware.ts` is deprecated in Next 16 and renamed to `proxy.ts` exporting
 * `proxy()`; the functionality is unchanged.
 *
 * **This is UX, not the authorization boundary.** The Next docs are explicit
 * that Proxy is meant to be invoked separately from render code and may be
 * deployed to a CDN, so from the API's point of view it is a client-side check —
 * and a client-side check is a suggestion. The real boundary is
 * `PrincipalMiddleware` plus `assert_every_route_is_guarded` in the agent
 * runtime, which refuse an unauthenticated or unscoped request regardless of
 * whether anything ever reached this file.
 *
 * What it is genuinely for: refreshing an expiring access token so the cookie
 * the render path reads is current, and sending a signed-out visitor to /login
 * instead of showing them an empty dashboard.
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

  // getUser(), never getSession(): getSession() reports whatever is in the
  // cookie without asking the auth server, so anyone able to write a cookie
  // would sail past this. Calling it here is also what triggers the refresh.
  const {
    data: { user },
  } = await supabase.auth.getUser();

  const path = request.nextUrl.pathname;
  const isPublic = PUBLIC_PREFIXES.some((p) => path === p || path.startsWith(`${p}/`));

  if (!user && !isPublic) {
    const url = request.nextUrl.clone();
    url.pathname = "/login";
    // So the bounce returns them to what they asked for rather than to the
    // dashboard root.
    url.searchParams.set("next", path + request.nextUrl.search);
    return NextResponse.redirect(url);
  }

  return response;
}

export const config = {
  // Everything except static assets. Listed as an exclusion rather than an
  // inclusion deliberately: a new route is covered the moment it exists, which
  // is the same default-deny shape as PUBLIC_PATHS on the runtime side.
  matcher: ["/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)"],
};
