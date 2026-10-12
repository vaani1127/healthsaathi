# Python image for the local demo: api, worker, migrations, seed and init all run from it.
# Built from the repo root with the root .dockerignore.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
# git: the local witness folder is a git repository.
RUN apt-get update \
  && apt-get install -y --no-install-recommends git \
  && rm -rf /var/lib/apt/lists/*
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /repo

COPY pyproject.toml uv.lock ./
COPY apps/api/pyproject.toml apps/api/
COPY packages/e2d-core/pyproject.toml packages/e2d-core/
COPY sim/pyproject.toml sim/
COPY experiments/pyproject.toml experiments/
COPY infra/demo/pyproject.toml infra/demo/
COPY packages/e2d-core/src packages/e2d-core/src
COPY sim/src sim/src
COPY sim/profiles sim/profiles
COPY experiments/src experiments/src
COPY infra/demo/src infra/demo/src
COPY apps/api apps/api
RUN uv sync --frozen --no-dev --package healthsaathi-demo

COPY infra/demo/entrypoint.sh /usr/local/bin/demo-entrypoint
RUN chmod +x /usr/local/bin/demo-entrypoint
ENV PATH="/repo/.venv/bin:$PATH" DEMO_STATE_DIR=/state SIM_PROFILES_DIR=/repo/sim/profiles
WORKDIR /repo/apps/api
ENTRYPOINT ["demo-entrypoint"]
