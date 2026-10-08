import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app import __version__, health
from app.chart.router import router as chart_router
from app.clinic.router import router as clinic_router
from app.core.config import get_settings
from app.core.errors import install_error_handlers, problem_response
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.core.ratelimit import limiter
from app.core.security_headers import SecurityHeadersMiddleware
from app.identity.router import router as auth_router
from app.identity.router import staff_router
from app.realtime.hub import hub
from app.realtime.router import router as realtime_router
from app.startup import maintenance_loop, run_startup_tasks

API_PREFIX = "/api/v1"


def _rate_limited(_: Request, __: Exception) -> Response:
    return problem_response(429, "rate-limited", "Too many requests. Try again in a minute.")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await run_startup_tasks()
    maintenance = asyncio.create_task(maintenance_loop())
    yield
    maintenance.cancel()
    await hub.close()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="HealthSaathi API",
        version=__version__,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url="/docs" if settings.env in ("local", "test") else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.limiter = limiter
    install_error_handlers(app)
    app.add_exception_handler(RateLimitExceeded, _rate_limited)

    # Middleware runs bottom-up: request context first, then security headers, CORS, limits.
    app.add_middleware(SlowAPIMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["authorization", "content-type", "x-device-id", "x-request-id"],
        expose_headers=["x-request-id"],
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)

    app.include_router(health.router, prefix=API_PREFIX)
    app.include_router(auth_router, prefix=API_PREFIX)
    app.include_router(staff_router, prefix=API_PREFIX)
    app.include_router(clinic_router, prefix=API_PREFIX)
    app.include_router(chart_router, prefix=API_PREFIX)
    app.include_router(realtime_router, prefix=API_PREFIX)
    return app


app = create_app()
