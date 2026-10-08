"""Rate limits from SPEC 4, kept in memory (one VM).

slowapi handles the per-route limits keyed by IP or user. Limits keyed by something in the request
body (OTP requests per email) use the same limiter storage through `hit()`.
"""

from fastapi import Request
from limits import RateLimitItem, parse
from slowapi import Limiter
from slowapi.util import get_remote_address

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


def hit(limit: str, *identifiers: str) -> None:
    """Count one request against `limit` for the given key; raise 429 when it is used up."""
    if not limiter.enabled:
        return
    item: RateLimitItem = parse(limit)
    if not limiter.limiter.hit(item, *identifiers):
        raise ProblemError(429, "rate-limited", "Too many requests. Try again in a minute.")


def reset() -> None:
    limiter.reset()
