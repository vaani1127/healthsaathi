# Changelog

## Unreleased

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
