"""Rate limits from SPEC 4.

The limits that protect sign-in (login per IP, email codes per address and per IP, authenticator
attempts per user, and the password lockout in `identity/lockout.py`) are counted in Postgres
through `hit()` and `allow()` (`core/counters.py`), so they hold across API workers and restarts.

slowapi keeps the coarse per-route limits (search, export and print) in each process's memory.
With 2 Uvicorn workers those are counted per worker, so up to twice the limit gets through, and a
restart clears them.
"""

from typing import Any

from fastapi import Depends, Request
from limits import parse
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core import counters
from app.core.config import get_settings
from app.core.errors import ProblemError

LOGIN = "5/minute"
OTP_PER_EMAIL = "3/minute"
SEARCH = "30/minute"
EXPORT_PRINT = "10/minute"
DEFAULT = "120/minute"


def client_ip(request: Request) -> str:
    return f"ip:{get_remote_address(request)}"


def user_or_ip(request: Request) -> str:
    """Key for per-user limits: the user from a valid access token, else the client IP."""
    from app.identity.tokens import TokenError, decode_token

    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() == "bearer" and token:
        try:
            return f"user:{decode_token(token, 'access')['sub']}"
        except TokenError:
            pass
    return client_ip(request)


limiter = Limiter(
    key_func=user_or_ip,
    default_limits=[DEFAULT],
    enabled=get_settings().rate_limit_enabled,
    headers_enabled=False,
)


async def allow(limit: str, scope: str, *identifiers: str) -> bool:
    """Count one request against `limit` for this key in the database. False once the limit for
    the current window is used up. Every call counts, refused ones too. Always true when rate
    limits are switched off (RATE_LIMIT_ENABLED=false)."""
    if not limiter.enabled:
        return True
    count = await counters.increment(limit, scope, *identifiers)
    return count <= parse(limit).amount


async def hit(limit: str, scope: str, *identifiers: str) -> None:
    """Like `allow()`, but raise 429 when the limit is used up."""
    if not await allow(limit, scope, *identifiers):
        raise ProblemError(429, "rate-limited", "Too many requests. Try again in a minute.")


def per_ip(route: str, limit: str = LOGIN) -> Any:
    """A route dependency: `limit` per client IP for this route, counted in the database. The IP
    is the client's as set by the trusted proxy (`TRUSTED_PROXY_IPS`)."""

    async def check(request: Request) -> None:
        await hit(limit, "login-ip", route, client_ip(request))

    return Depends(check)


def reset() -> None:
    limiter.reset()
