# Production build of the PWA served by Caddy, with the API proxied on the same origin.
FROM node:22-alpine AS build
WORKDIR /repo
RUN corepack enable
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/
COPY packages/receipt-verify/package.json packages/receipt-verify/
RUN pnpm install --frozen-lockfile
COPY apps/web apps/web
COPY packages/receipt-verify packages/receipt-verify
# Same origin: the API is under /api on the web server.
ENV VITE_API_URL=""
RUN pnpm --filter @healthsaathi/web build

FROM caddy:2.10-alpine
COPY --from=build /repo/apps/web/dist /srv
COPY infra/demo/Caddyfile /etc/caddy/Caddyfile
