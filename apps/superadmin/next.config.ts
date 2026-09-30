import path from "node:path";
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /**
   * Same shape as apps/web/next.config.ts, for the same Dockerfile pattern:
   * standalone output traced from the monorepo root so the bundle keeps its
   * apps/superadmin/ prefix.
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
     * Server Actions are the only mutation path in this app, and Next checks
     * Origin against Host on each one. Behind Coolify the real host arrives as
     * X-Forwarded-Host, so it has to be listed. The console lives on its own
     * hostname rather than under /admin on the tenant app: a separate origin
     * means a separate cookie jar, a separate deploy that can be firewalled to
     * an office IP, and no route on the customer's domain that answers 404 to
     * customers and 200 to staff.
     */
    serverActions: {
      allowedOrigins: [
        "admin.ad-vit.broadmate.org",
        "localhost:3001",
        "127.0.0.1:3001",
      ],
    },
  },
};

export default nextConfig;
