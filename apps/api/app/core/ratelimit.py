"""Rate limits from SPEC 4.

The limits that protect sign-in (login per IP, email codes per address and per IP, authenticator
attempts per user, and the password lockout in `identity/lockout.py`) are counted in Postgres
through `hit()` and `allow()` (`core/counters.py`), so they hold across API workers and restarts.

slowapi keeps the coarse per-route limits (search, export and print) in each process's memory,
through its decorators. `DefaultRateLimitMiddleware` applies the default limit (120 a minute) to
every other API request, in the same memory storage. With 2 Uvicorn workers these are counted per
worker, so up to twice the limit gets through, and a restart clears them.

Every 429 carries Retry-After: the seconds until the current window ends.
"""

import math
import time
from typing import Any

from fastapi import Depends, Request
from limits import parse
from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core import counters
from app.core.config import get_settings
from app.core.errors import ProblemError, problem_response

# Per IP, for password sign-in and the authenticator check. Staff of one clinic often share
# one router address, so this is higher than the per-account limits below it.
LOGIN_SHARED_IP = "20/minute"
# Per IP for email codes and password changes.
LOGIN = "5/minute"
OTP_PER_EMAIL = "3/minute"
SEARCH = "30/minute"
EXPORT_PRINT = "10/minute"
DEFAULT = "120/minute"


def client_ip(request: Request) -> str:
    return f"ip:{get_remote_address(request)}"


def key_for(authorization: str, client_host: str | None) -> str:
    """The user from a valid access token (typ "access"), else the client IP. A login challenge,
    an enrolment token or any other token type counts as no token."""
    from app.identity.tokens import TokenError, decode_token

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token:
        try:
            return f"user:{decode_token(token, 'access')['sub']}"
        except TokenError:
            pass
    return f"ip:{client_host or '127.0.0.1'}"


def user_or_ip(request: Request) -> str:
    """Key for per-user limits: the user from a valid access token, else the client IP."""
    return key_for(
        request.headers.get("authorization", ""), request.client.host if request.client else None
    )


limiter = Limiter(
    key_func=user_or_ip,
    enabled=get_settings().rate_limit_enabled,
    headers_enabled=False,
)


def retry_after(limit: str) -> str:
    """Seconds until the current window of a database counter ends. Those windows start on
    multiples of their length (`core/counters.py`)."""
    seconds = parse(limit).get_expiry()
    return str(max(1, math.ceil(seconds - time.time() % seconds)))


def too_many(retry: str) -> ProblemError:
    return ProblemError(
        429,
        "rate-limited",
        "Too many requests. Try again in a minute.",
        headers={"Retry-After": retry},
    )


# Never limited: health checks (Docker, the deploy check, monitors). WebSocket connections and
# CORS preflights (OPTIONS) are skipped by kind, not by path.
DEFAULT_EXEMPT = frozenset({"/api/v1/health", "/api/v1/ready"})


class DefaultRateLimitMiddleware:
    """The SPEC 4 default limit (120 requests a minute) for every HTTP request, keyed per user
    from a valid access token, else per client IP. In memory, per worker.

    A plain ASGI middleware, so it does not depend on how FastAPI stores its routes (slowapi's
    middleware could not find the routes inside FastAPI's included routers and limited nothing).
    It sits inside the CORS middleware, so a 429 still carries the CORS headers the browser needs
    to read it.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.item = parse(DEFAULT)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] == "OPTIONS"
            or scope["path"] in DEFAULT_EXEMPT
            or not limiter.enabled
        ):
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        client = scope.get("client")
        key = key_for(
            headers.get(b"authorization", b"").decode("latin-1"), client[0] if client else None
        )
        if not limiter.limiter.hit(self.item, "default", key):
            reset, _ = limiter.limiter.get_window_stats(self.item, "default", key)
            retry = str(max(1, math.ceil(reset - time.time())))
            response = problem_response(
                429,
                "rate-limited",
                "Too many requests. Try again in a minute.",
                headers={"Retry-After": retry},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


async def allow(limit: str, scope: str, *identifiers: str) -> bool:
    """Count one request against `limit` for this key in the database. False once the limit for
    the current window is used up. Every call counts, refused ones too. Always true when rate
    limits are switched off (RATE_LIMIT_ENABLED=false)."""
    if not limiter.enabled:
        return True
    count = await counters.increment(limit, scope, *identifiers)
    return count <= parse(limit).amount


async def hit(limit: str, scope: str, *identifiers: str) -> None:
    """Like `allow()`, but raise 429 (with Retry-After) when the limit is used up."""
    if not await allow(limit, scope, *identifiers):
        raise too_many(retry_after(limit))


def per_ip(route: str, limit: str = LOGIN) -> Any:
    """A route dependency: `limit` per client IP for this route, counted in the database. The IP
    is the client's as set by the trusted proxy (`TRUSTED_PROXY_IPS`)."""

    async def check(request: Request) -> None:
        await hit(limit, "login-ip", route, client_ip(request))

    return Depends(check)


def reset() -> None:
    limiter.reset()
