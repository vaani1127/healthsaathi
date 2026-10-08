"""Per-account lockout after repeated wrong passwords, kept in memory like the rate limits."""

from limits import parse

from app.core.ratelimit import limiter

MAX_FAILURES = "5/15 minutes"
_item = parse(MAX_FAILURES)


def _key(email: str) -> str:
    return email.strip().lower()


def is_locked(email: str) -> bool:
    if not limiter.enabled:
        return False
    return not limiter.limiter.test(_item, "login-failures", _key(email))


def record_failure(email: str) -> None:
    if limiter.enabled:
        limiter.limiter.hit(_item, "login-failures", _key(email))


def clear(email: str) -> None:
    if limiter.enabled:
        limiter.limiter.clear(_item, "login-failures", _key(email))
