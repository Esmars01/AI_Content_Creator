# syntax=docker/dockerfile:1.7
# The Next.js web app (§9 `web`, §31) as a standalone Node server. Build from the repository root:
#   docker build -f infra/docker/web.Dockerfile -t creator-engine/web .
# Pinned by digest: Node 24.21.0 LTS on Debian bookworm (slim). pnpm comes from the root
# `packageManager` field through corepack.

FROM node@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6 AS build
ENV PNPM_HOME=/pnpm COREPACK_ENABLE_DOWNLOAD_PROMPT=0 NEXT_TELEMETRY_DISABLED=1
ENV PATH=$PNPM_HOME:$PATH
WORKDIR /app
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/package.json
COPY packages/ts/api-client/package.json packages/ts/api-client/package.json
RUN --mount=type=cache,id=pnpm,target=/pnpm/store --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/extra_ca; fi; \
    corepack enable && pnpm install --frozen-lockfile --filter "@ce/web..."
# sources after the install, so dependency layers stay cached across code changes
COPY packages/ts/api-client packages/ts/api-client
COPY apps/web apps/web
RUN pnpm --filter @ce/web build

FROM node@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6 AS runtime
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
WORKDIR /app
COPY --from=build --chown=node:node /app/apps/web/.next/standalone ./
COPY --from=build --chown=node:node /app/apps/web/.next/static ./apps/web/.next/static
USER node
EXPOSE 3000
HEALTHCHECK --interval=10s --timeout=5s --retries=6 CMD ["node", "-e", "fetch('http://127.0.0.1:3000/login').then(r => process.exit(r.ok ? 0 : 1), () => process.exit(1))"]
# The app forwards /api/* to API_INTERNAL_URL (the api service); the browser never calls the API directly.
CMD ["node", "apps/web/server.js"]
