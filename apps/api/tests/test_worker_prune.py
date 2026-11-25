"""The worker deletes rate limit windows that have ended."""

import uuid

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app import worker
from app.core import counters
from app.db import models as m


async def test_worker_prunes_ended_windows(admin_engine: AsyncEngine) -> None:
    ended, current = f"ended-{uuid.uuid4().hex[:6]}", f"now-{uuid.uuid4().hex[:6]}"
    await counters.increment("5/minute", ended)
    await counters.increment("5/15 minutes", current)
    async with AsyncSession(admin_engine) as s, s.begin():
        await s.execute(
            update(m.RateCounter)
            .where(m.RateCounter.key == counters.counter_key(ended))
            .values(expires_at=m.RateCounter.expires_at - text("interval '5 minutes'"))
        )

    assert await worker.prune_counters_tick() == 1

    async with AsyncSession(admin_engine) as s:
        keys = set((await s.scalars(select(m.RateCounter.key))).all())
    assert counters.counter_key(ended) not in keys
    assert counters.counter_key(current) in keys
    # Nothing left to prune.
    assert await worker.prune_counters_tick() == 0
