/**
 * The session cookie's flags, stated rather than inherited.
 *
 * @supabase/ssr 0.7 defaults to `httpOnly: false` - it is built for apps that
 * also run a browser client, which has to read the cookie. This app has no
 * browser client, and lib/supabase/server.ts promises an httpOnly cookie. Until
 * this constant existed the promise was false: the library's default shipped
 * the access token to JavaScript, and the XSS argument the design rests on
 * held only in the comment. `secure` follows NODE_ENV so local http://localhost
 * still signs in; production is https behind Traefik.
 */
export const SESSION_COOKIE = {
  httpOnly: true,
  secure: process.env.NODE_ENV === "production",
  sameSite: "lax" as const,
  path: "/",
};
