# Changelog

## Unreleased

### P1 - Data model, migrations, RLS, DB roles

- SQLAlchemy models for every table in SPEC 3.1 with UUIDv7 ids and `timestamptz` columns.
- Alembic migration `0001` creates the roles `migrator`, `app_rw`, `anchor_job` and `researcher_ro`,
  row level security on every clinic scoped table, composite same-clinic foreign keys, monthly
  partitions for `access_events` and `audit_events` with `create_month_partitions()`, append-only
  triggers, and the `research.access_events_deid` view. See ADR 0002.
- Every app transaction runs as `app_rw` with `app.clinic_id`, `app.user_id` and `app.role` set
  locally (`app.core.db.tenant_session`).
- Tests: per-table read, insert, update and delete isolation between two clinics, append-only
  checks, role checks, and a schema drift check against `information_schema` and Alembic.
- `make migrate` and `make seed` (two demo clinics, staff for every role, 50 synthetic patients,
  schedules, services and consent notice v1 in English and Hindi).
- New env vars: `MIGRATOR_DATABASE_URL`, `SEED_DEMO_PASSWORD`.
- Still to do: call `create_month_partitions()` from the background worker on a schedule (P5).

### P0 - Repo scaffold, tooling, CI

- Monorepo with a uv workspace (apps/api, packages/e2d-core, sim, experiments) and a pnpm workspace
  (apps/web, packages/receipt-verify).
- API: FastAPI app with `/api/v1/health` and `/api/v1/ready` (checks the database), settings from
  environment, JSON logs with a request id on every line and in the `x-request-id` header.
- Web: Vite, React 19, strict TypeScript, Tailwind, shadcn/ui base components, TanStack Router and
  Query, i18next with English and Hindi, installable PWA with an update prompt. The home page shows
  the API health.
- Contracts: empty Foundry project with forge-std.
- Dev stack in `infra/docker-compose.dev.yml` (postgres 16, api with hot reload, web).
- Makefile, CI workflow (ruff, mypy, pytest with postgres, eslint, tsc, vitest, forge, pip-audit,
  pnpm audit), pre-commit hooks, editorconfig, `.env.example`, ADR 0001.
