import path from "node:path";
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /**
   * Standalone output is what infra/docker/web.Dockerfile ships: server.js
   * plus only the node_modules the build actually traced, instead of the whole
   * workspace install. The tracing root is the monorepo root so the lockfile's
   * hoisted node_modules are found and the bundle keeps its apps/web/ prefix.
   */
  output: "standalone",
  outputFileTracingRoot: path.join(__dirname, "../../"),
  /**
   * Security headers the reverse proxy does not add for us. Verified absent
   * on production on 2026-09-16. No Content-Security-Policy yet: Next's
   * inline runtime needs a per-request nonce for a strict one, and a loose
   * one is decoration - it is a separate change.
   */
  async headers() {
    return [
      {
        source: "/(.*)",
        headers: [
          { key: "Strict-Transport-Security", value: "max-age=63072000; includeSubDomains" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
        ],
      },
    ];
  },
  experimental: {
    /**
     * Next checks Origin against Host for every Server Action, which is what
     * makes sign-in, sign-out and every mutation CSRF-safe while the session
     * lives in an httpOnly cookie.
     *
     * Behind Coolify the app sits behind a reverse proxy, so Host is the
     * internal container name and X-Forwarded-Host is the real one. Without
     * this list the check compares two different things and fails
     * unpredictably - which looks like a flaky deploy rather than like a
     * misconfiguration, and the tempting fix is to turn the check off.
     */
    serverActions: {
      allowedOrigins: [
        "ad-vit.broadmate.org",
        "localhost:3000",
        "127.0.0.1:3000",
      ],
    },
  },
};

export default nextConfig;
