FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /repo

COPY pyproject.toml uv.lock ./
COPY apps/api/pyproject.toml apps/api/
COPY packages/e2d-core/pyproject.toml packages/e2d-core/
COPY sim/pyproject.toml sim/
COPY experiments/pyproject.toml experiments/
COPY apps/api/app apps/api/app
COPY packages/e2d-core/src packages/e2d-core/src
COPY sim/src sim/src
COPY experiments/src experiments/src

FROM base AS dev
RUN uv sync --frozen --package healthsaathi-api
WORKDIR /repo/apps/api

FROM base AS prod
RUN uv sync --frozen --no-dev --package healthsaathi-api
COPY apps/api apps/api
WORKDIR /repo/apps/api
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers"]
