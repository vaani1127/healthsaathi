import asyncio
import logging
import math
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from starlette.requests import Request
from starlette.responses import Response
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import __version__, health
from app.anchor.router import router as anchor_router
from app.billing.router import router as billing_router
from app.chart.router import router as chart_router
from app.clinic.router import router as clinic_router
from app.clinical.router import router as clinical_router
from app.consent.router import router as consent_router
from app.core.config import get_settings
from app.core.db import wait_for_database
from app.core.errors import install_error_handlers, problem_response
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.core.ratelimit import DefaultRateLimitMiddleware, limiter
from app.core.security_headers import SecurityHeadersMiddleware
from app.detect.router import router as detect_router
from app.identity.router import router as auth_router
from app.identity.router import staff_router
from app.notify.router import push_router
from app.notify.router import router as dev_mail_router
from app.portal.router import router as portal_router
from app.realtime.hub import hub
from app.realtime.router import router as realtime_router
from app.records.router import router as records_router
from app.startup import maintenance_loop, run_startup_tasks

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"


def _rate_limited(request: Request, __: Exception) -> Response:
    """A search, export or print limit (slowapi decorator) is used up."""
    retry = "60"
    stats = getattr(request.state, "view_rate_limit", None)
    if stats is not None:
        reset, _ = limiter.limiter.get_window_stats(stats[0], *stats[1])
        retry = str(max(1, math.ceil(reset - time.time())))
    return problem_response(
        429,
        "rate-limited",
        "Too many requests. Try again in a minute.",
        headers={"Retry-After": retry},
    )


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    try:
        await wait_for_database()
    except Exception:
        logger.exception("database not reachable at startup; /ready reports it")
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

    # Middleware runs bottom-up: the client address first, then request context, security
    # headers, CORS and limits. The address comes first so every later step (limits, access
    # events) sees the client, not the proxy.
    # The default limit (120 a minute per user or IP). Search, export and print have their own
    # slowapi decorators, which need no middleware.
    app.add_middleware(DefaultRateLimitMiddleware)
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
    # X-Forwarded-For is believed only from TRUSTED_PROXY_IPS (Caddy). From any other peer the
    # header is ignored. uvicorn runs with --no-proxy-headers so this is the only place.
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=settings.trusted_proxies)

    app.include_router(health.router, prefix=API_PREFIX)
    app.include_router(auth_router, prefix=API_PREFIX)
    app.include_router(staff_router, prefix=API_PREFIX)
    app.include_router(clinic_router, prefix=API_PREFIX)
    app.include_router(chart_router, prefix=API_PREFIX)
    app.include_router(clinical_router, prefix=API_PREFIX)
    app.include_router(billing_router, prefix=API_PREFIX)
    app.include_router(consent_router, prefix=API_PREFIX)
    app.include_router(portal_router, prefix=API_PREFIX)
    app.include_router(realtime_router, prefix=API_PREFIX)
    app.include_router(anchor_router, prefix=API_PREFIX)
    app.include_router(records_router, prefix=API_PREFIX)
    app.include_router(detect_router, prefix=API_PREFIX)
    app.include_router(dev_mail_router, prefix=API_PREFIX)
    app.include_router(push_router, prefix=API_PREFIX)
    return app


app = create_app()
