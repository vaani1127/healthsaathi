from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app import __version__, health
from app.core.config import get_settings
from app.core.errors import install_error_handlers, problem_response
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.core.ratelimit import limiter
from app.core.security_headers import SecurityHeadersMiddleware
from app.identity.router import router as auth_router
from app.identity.router import staff_router

API_PREFIX = "/api/v1"


def _rate_limited(_: Request, __: Exception) -> Response:
    return problem_response(429, "rate-limited", "Too many requests. Try again in a minute.")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="HealthSaathi API",
        version=__version__,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url="/docs" if settings.env in ("local", "test") else None,
        redoc_url=None,
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
    return app


app = create_app()
