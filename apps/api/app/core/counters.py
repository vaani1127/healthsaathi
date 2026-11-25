"""Fixed-window rate counters in Postgres, shared by every API worker and kept across restarts.

Used for the limits that protect sign-in: login per IP, email codes per address and per IP,
authenticator attempts per user, and the password lockout. Each hit is one statement that adds
one to the current window's row and returns the new count, so two workers counting the same key
at the same moment both count.

Windows are fixed and start on multiples of their length, on the database clock. A fixed window
lets up to twice the limit through in a short burst across a window edge (the end of one window
and the start of the next); over any longer stretch the rate stays at the limit.

Keys are HMAC-SHA256 of the scope and identifiers with the derived key
"healthsaathi/rate-counters/v2" (`core/keys.py`, ADR 0009), so the table holds no IP or email
address. (v1 was HMAC(OTP_HMAC_KEY, "rate-counters-v1"); moving to v2 reset the live counts once.)

If the database cannot be reached the limit fails closed: the request is refused with 503.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from limits import parse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core import keys
from app.core.db import ANONYMOUS, tenant_session
from app.core.errors import ProblemError

logger = logging.getLogger(__name__)

_INCREMENT = text(
    """
    WITH w AS (
        SELECT to_timestamp(
            floor(extract(epoch FROM clock_timestamp()) / CAST(:seconds AS integer))
            * CAST(:seconds AS integer)
        ) AS start
    )
    INSERT INTO rate_counters (key, window_start, count, expires_at)
    SELECT :key, w.start, 1, w.start + CAST(:seconds AS integer) * interval '1 second' FROM w
    ON CONFLICT (key, window_start) DO UPDATE SET count = rate_counters.count + 1
    RETURNING count
    """
)


def counter_key(scope: str, *identifiers: str) -> str:
    return keys.hmac_hex(keys.RATE_COUNTERS, "\x1f".join((scope, *identifiers)).encode())


@asynccontextmanager
async def _fail_closed() -> AsyncIterator[None]:
    try:
        yield
    except (SQLAlchemyError, OSError, TimeoutError) as exc:
        logger.error("rate counter unavailable", extra={"error": type(exc).__name__})
        raise ProblemError(503, "rate-limit-unavailable", "Try again in a minute.") from exc


async def increment(limit: str, scope: str, *identifiers: str) -> int:
    """Add one to the current window of this key and return the new count."""
    seconds = parse(limit).get_expiry()
    async with _fail_closed(), tenant_session(ANONYMOUS) as db:
        count = await db.scalar(
            _INCREMENT, {"key": counter_key(scope, *identifiers), "seconds": seconds}
        )
    return int(count or 0)


async def clear(scope: str, *identifiers: str) -> None:
    """Forget every window of this key."""
    async with _fail_closed(), tenant_session(ANONYMOUS) as db:
        await db.execute(
            text("DELETE FROM rate_counters WHERE key = :key"),
            {"key": counter_key(scope, *identifiers)},
        )


async def prune() -> int:
    """Delete windows that have ended. Run by the worker."""
    async with tenant_session(ANONYMOUS) as db:
        result = await db.execute(
            text("DELETE FROM rate_counters WHERE expires_at < clock_timestamp()")
        )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
