# DevGuard AI — frontend production image (Next.js 16, standalone output).
#
# This file did not exist before. A review recorded that
# docker-compose.yml referenced `frontend/Dockerfile` while no such file was in
# the repository, so `docker compose up` could never have succeeded. This is that
# missing file.
#
# Build context is ./frontend.
#
#   docker build -t devguard-frontend ./frontend
#   (or `docker compose build frontend`)
#
# NOTE ON BUILD-TIME ENV: NEXT_PUBLIC_* values are inlined into the client bundle
# at BUILD time, not read at runtime. They are declared as ARGs so an image can be
# built against a specific backend, and default to localhost for the local demo.
# Rebuild the image to change them — setting them only in `environment:` at run
# time will NOT affect an already-built bundle.

# ---- Stage 1: dependencies ----
FROM node:20-alpine AS deps
WORKDIR /app

# npm ci needs both files and installs exactly what the lockfile pins.
COPY package.json package-lock.json ./
RUN npm ci

# ---- Stage 2: build ----
FROM node:20-alpine AS builder
WORKDIR /app

ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ARG NEXT_PUBLIC_SIGNOZ_URL=http://localhost:3301
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL \
    NEXT_PUBLIC_SIGNOZ_URL=$NEXT_PUBLIC_SIGNOZ_URL \
    NEXT_TELEMETRY_DISABLED=1

COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# ---- Stage 3: runtime ----
FROM node:20-alpine AS runtime
WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# Non-root, matching the backend image's posture.
RUN addgroup --system --gid 1001 nodejs \
    && adduser --system --uid 1001 nextjs

# `output: 'standalone'` (frontend/next.config.js) produces a self-contained
# server.js plus a pruned node_modules — these two COPYs are the whole runtime.
#
# There is deliberately NO `COPY /app/public ./public`: this app has no public/
# directory, and COPY of a missing path is a hard build failure. That is the same
# mistake recorded against the original compose file (referencing a
# Dockerfile that did not exist), so it is called out rather than left to bite.
# If a public/ directory is ever added, add the COPY back with it.
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD node -e "require('http').get('http://127.0.0.1:'+(process.env.PORT||3000)+'/',r=>process.exit(r.statusCode<500?0:1)).on('error',()=>process.exit(1))"

CMD ["node", "server.js"]
