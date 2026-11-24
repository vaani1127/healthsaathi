"""Per-account lockout after repeated sign-in attempts, counted in the database.

Count first: every password attempt for an email adds one to its counter before the password is
checked, in one atomic statement that returns the count. Over the limit, the attempt is refused
as locked without checking the password. A wrong password keeps the count; a correct one deletes
it. Because the count is taken before the check, attempts sent at the same moment cannot all slip
in under the limit.
"""

from app.core import counters
from app.core.errors import ProblemError
from app.core.ratelimit import allow, limiter, retry_after

MAX_FAILURES = "5/15 minutes"
SCOPE = "login-failures"


def _key(email: str) -> str:
    return email.strip().lower()


async def count_attempt(email: str) -> None:
    """Count this attempt; raise 429 account-locked when it is over the limit."""
    if not await allow(MAX_FAILURES, SCOPE, _key(email)):
        raise ProblemError(
            429,
            "account-locked",
            "Too many wrong passwords. Try again in 15 minutes.",
            headers={"Retry-After": retry_after(MAX_FAILURES)},
        )


async def clear(email: str) -> None:
    """A correct password: forget the count."""
    if limiter.enabled:
        await counters.clear(SCOPE, _key(email))
