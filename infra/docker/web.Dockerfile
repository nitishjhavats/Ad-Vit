# ad-vit tenant surface (Next 16, standalone output). Build context is the
# REPO ROOT because apps/web is an npm workspace and the lockfile lives at the
# root; outputFileTracingRoot in next.config.ts points at the same root so the
# standalone bundle keeps the apps/web/ prefix.
FROM node:22-alpine AS deps
WORKDIR /repo
COPY package.json package-lock.json ./
COPY apps/web/package.json apps/web/
COPY packages/saas-core-db/package.json packages/saas-core-db/
# --ignore-scripts: the root devDependency `supabase` downloads a CLI binary on
# postinstall, which the web image has no use for.
RUN npm ci --workspace=apps/web --ignore-scripts

FROM node:22-alpine AS build
WORKDIR /repo
COPY --from=deps /repo/node_modules ./node_modules
COPY package.json package-lock.json ./
COPY apps/web ./apps/web
COPY packages/saas-core-db/package.json packages/saas-core-db/
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build --workspace=apps/web

FROM node:22-alpine AS run
WORKDIR /repo
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000
RUN addgroup -S -g 10001 advit && adduser -S -u 10001 -G advit advit
COPY --from=build --chown=advit:advit /repo/apps/web/.next/standalone ./
COPY --from=build --chown=advit:advit /repo/apps/web/.next/static ./apps/web/.next/static
COPY --from=build --chown=advit:advit /repo/apps/web/public ./apps/web/public
USER advit
EXPOSE 3000
CMD ["node", "apps/web/server.js"]
