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
RUN useradd --system --uid 10001 app
ENV PATH="/repo/.venv/bin:$PATH"
USER app
WORKDIR /repo/apps/api
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3   CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4)"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers"]
