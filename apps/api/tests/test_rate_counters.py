"""Rate counters in Postgres: shared by workers, kept across restarts, fixed windows."""

import asyncio
import hashlib
import hmac
import importlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core import counters
from app.core.config import get_settings
from app.core.db import get_engine
from app.core.errors import ProblemError
from app.db import models as m
from tests.identity.test_single_use import row_locked, wait_for_lock_waiters
from tests.test_keys import rfc5869_sha256


def scope() -> str:
    # A fresh key per test, so tests never share a count.
    return f"test-{uuid.uuid4().hex[:8]}"


async def rows(engine: AsyncEngine, key: str) -> list[m.RateCounter]:
    async with AsyncSession(engine) as s:
        return list((await s.scalars(select(m.RateCounter).where(m.RateCounter.key == key))).all())


async def test_two_sessions_counting_the_same_key_at_once_both_count(
    admin_engine: AsyncEngine,
) -> None:
    name = scope()
    key = counters.counter_key(name, "x")
    assert await counters.increment("5/minute", name, "x") == 1

    # Both increments wait on the row lock held here, so they really run at the same time.
    async with row_locked(
        admin_engine, "SELECT 1 FROM rate_counters WHERE key = :key FOR UPDATE", key=key
    ):
        tasks = [asyncio.create_task(counters.increment("5/minute", name, "x")) for _ in range(2)]
        await wait_for_lock_waiters(admin_engine, 2)
    assert sorted(await asyncio.gather(*tasks)) == [2, 3]
    assert [r.count for r in await rows(admin_engine, key)] == [3]


async def test_parallel_hits_get_every_count_once(admin_engine: AsyncEngine) -> None:
    name = scope()
    counts = await asyncio.gather(*(counters.increment("10/minute", name) for _ in range(6)))
    assert sorted(counts) == [1, 2, 3, 4, 5, 6]


async def test_counts_survive_a_restart(admin_engine: AsyncEngine) -> None:
    name = scope()
    assert await counters.increment("5/minute", name) == 1
    assert await counters.increment("5/minute", name) == 2
    # A restarted worker: new connections and a freshly loaded module.
    await get_engine().dispose()
    importlib.reload(counters)
    assert await counters.increment("5/minute", name) == 3


async def test_a_new_window_starts_at_one(admin_engine: AsyncEngine) -> None:
    name = scope()
    key = counters.counter_key(name)
    for _ in range(3):
        await counters.increment("3/minute", name)
    # Move the window back by its length, as if a minute had passed.
    async with AsyncSession(admin_engine) as s, s.begin():
        await s.execute(
            update(m.RateCounter)
            .where(m.RateCounter.key == key)
            .values(
                window_start=m.RateCounter.window_start - text("interval '60 seconds'"),
                expires_at=m.RateCounter.expires_at - text("interval '60 seconds'"),
            )
        )
    assert await counters.increment("3/minute", name) == 1
    assert sorted(r.count for r in await rows(admin_engine, key)) == [1, 3]


async def test_window_is_aligned_and_expires_after_its_length(admin_engine: AsyncEngine) -> None:
    name = scope()
    await counters.increment("5/15 minutes", name)
    (row,) = await rows(admin_engine, counters.counter_key(name))
    assert row.window_start.timestamp() % 900 == 0
    assert (row.expires_at - row.window_start).total_seconds() == 900


async def test_clear_forgets_every_window(admin_engine: AsyncEngine) -> None:
    name = scope()
    await counters.increment("5/minute", name)
    await counters.clear(name)
    assert await rows(admin_engine, counters.counter_key(name)) == []
    assert await counters.increment("5/minute", name) == 1


async def test_prune_deletes_ended_windows_only(admin_engine: AsyncEngine) -> None:
    old, current = scope(), scope()
    await counters.increment("5/minute", old)
    await counters.increment("5/minute", current)
    async with AsyncSession(admin_engine) as s, s.begin():
        await s.execute(
            update(m.RateCounter)
            .where(m.RateCounter.key == counters.counter_key(old))
            .values(expires_at=m.RateCounter.expires_at - text("interval '2 minutes'"))
        )
    assert await counters.prune() >= 1
    assert await rows(admin_engine, counters.counter_key(old)) == []
    assert len(await rows(admin_engine, counters.counter_key(current))) == 1


async def test_key_uses_a_derived_secret_and_stores_no_identifier(
    admin_engine: AsyncEngine,
) -> None:
    name = scope()
    email = f"someone.{uuid.uuid4().hex[:6]}@counters.test"
    await counters.increment("5/minute", name, email)
    otp_key = get_settings().otp_hmac_key.get_secret_value().encode()
    message = f"{name}\x1f{email}".encode()
    # The derived key "healthsaathi/rate-counters/v2" (ADR 0009), computed independently.
    derived = rfc5869_sha256(otp_key, b"healthsaathi/keys", b"healthsaathi/rate-counters/v2", 32)
    expected = hmac.new(derived, message, hashlib.sha256).hexdigest()
    # Not the v1 key either.
    v1 = hmac.new(otp_key, b"rate-counters-v1", hashlib.sha256).digest()
    assert expected != hmac.new(v1, message, hashlib.sha256).hexdigest()
    assert counters.counter_key(name, email) == expected
    # Not the OTP key itself.
    assert expected != hmac.new(otp_key, message, hashlib.sha256).hexdigest()
    async with admin_engine.connect() as conn:
        stored = (await conn.execute(text("SELECT key FROM rate_counters"))).scalars().all()
    assert expected in stored
    assert not any(email in k for k in stored)


async def test_unreachable_database_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def broken(_ctx: object) -> AsyncIterator[None]:
        down = True
        if down:
            raise OperationalError("SELECT 1", {}, ConnectionRefusedError("database is down"))
        yield

    monkeypatch.setattr(counters, "tenant_session", broken)
    for call in (counters.increment("5/minute", "down"), counters.clear("down")):
        with pytest.raises(ProblemError) as refused:
            await call
        assert refused.value.status == 503
        assert refused.value.code == "rate-limit-unavailable"
