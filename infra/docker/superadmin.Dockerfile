# ad-vit operator console (Next 16, standalone output). Same shape as
# web.Dockerfile: build context is the REPO ROOT because apps/superadmin is an
# npm workspace and the lockfile lives at the root; outputFileTracingRoot in
# next.config.ts points at the same root so the standalone bundle keeps the
# apps/superadmin/ prefix. Deployed on its own hostname
# (admin.ad-vit.broadmate.org), so it can be firewalled to staff separately.
FROM node:22-alpine AS deps
WORKDIR /repo
COPY package.json package-lock.json ./
COPY apps/superadmin/package.json apps/superadmin/
COPY packages/saas-core-db/package.json packages/saas-core-db/
# --ignore-scripts: the root devDependency `supabase` downloads a CLI binary on
# postinstall, which the web image has no use for.
RUN npm ci --workspace=apps/superadmin --ignore-scripts

FROM node:22-alpine AS build
WORKDIR /repo
COPY --from=deps /repo/node_modules ./node_modules
COPY package.json package-lock.json ./
COPY apps/superadmin ./apps/superadmin
COPY packages/saas-core-db/package.json packages/saas-core-db/
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build --workspace=apps/superadmin

FROM node:22-alpine AS run
WORKDIR /repo
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3001
RUN addgroup -S -g 10001 advit && adduser -S -u 10001 -G advit advit
COPY --from=build --chown=advit:advit /repo/apps/superadmin/.next/standalone ./
COPY --from=build --chown=advit:advit /repo/apps/superadmin/.next/static ./apps/superadmin/.next/static
COPY --from=build --chown=advit:advit /repo/apps/superadmin/public ./apps/superadmin/public
USER advit
EXPOSE 3001
CMD ["node", "apps/superadmin/server.js"]
