# ADR 0001: Stack and deployment shape

Status: accepted

## Context

HealthSaathi serves small OPD clinics (1 to 10 doctors) and is built and run by a student team with
no budget. It must be cheap to host, simple to operate, and the research code must share its logic
with the product so the paper measures what the product actually does.

## Decision

- **Modular monolith.** One FastAPI process (2 Uvicorn workers), one background worker process and
  Caddy, on a single Azure VM with Docker Compose. Modules: identity, clinic, clinical, billing,
  consent, access, explain, detect, ledger, anchor, notify, fhir.
- **Database.** PostgreSQL 16 (Azure Flexible Server B1MS). Row level security on every operational
  table keyed by `clinic_id`. The background worker polls a `jobs` table with
  `SELECT ... FOR UPDATE SKIP LOCKED`, so there is no Redis.
- **Frontend.** React 19 PWA built with Vite, hosted on Cloudflare Pages. The API lives on a sibling
  subdomain so refresh cookies can be `SameSite=Lax; Secure; HttpOnly`.
- **Realtime.** One WebSocket endpoint `/api/v1/ws`; the JWT is sent in the first message, not the URL.
- **Shared core.** Explanation, forgery, feature, scoring and Merkle logic lives in
  `packages/e2d-core` and is imported by both the API and the experiments.
- **Anchoring.** A job every 15 minutes on GitHub Actions, with a fallback timer on the VM, posts
  signed Merkle checkpoints to a Polygon Amoy contract, a public GitHub witness repo and
  OpenTimestamps.
- **Environments.** Local (docker compose), staging (Render free + Neon free, deployed from `main`),
  prod-demo (Azure, deployed on tags `v*`).
- **Tooling.** Python 3.12 with uv, pnpm workspaces for TypeScript, Foundry for Solidity.

## Consequences

- Everything runs on free tiers, but there is a single VM and no horizontal scaling. That fits the
  target clinic size.
- A single process keeps transactions simple: an access record, its explanation and its audit event
  are written in the same database transaction as the read.
- Rate limiting is in memory, which is fine with one VM but would need a shared store if scaled out.
- TypeScript is pinned to 6.0 because typescript-eslint does not yet support 7.x.
