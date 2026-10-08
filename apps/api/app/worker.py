"""Background worker process (SPEC 3).

For now it runs the anchor fallback timer: the GitHub Actions schedule is the main trigger for the
anchor job, and if it has not run for ANCHOR_FALLBACK_MINUTES the worker runs the job itself. The
job's advisory lock keeps the two from overlapping.

    uv run python -m app.worker
"""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import func, select, text

from app.anchor import job
from app.core.config import get_settings
from app.core.db import TenantContext, get_engine, tenant_session
from app.core.logging import configure_logging
from app.db.models import AnchorReceipt, MerkleCheckpoint
from app.startup import maintenance_loop, run_startup_tasks

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 60


async def anchor_is_overdue(after: timedelta) -> bool:
    """True when neither a checkpoint nor an anchor attempt happened in the last `after`."""
    ctx = TenantContext(role="system", db_role=job.JOB_ROLE)
    async with tenant_session(ctx) as db:
        now = await db.scalar(text("SELECT clock_timestamp()"))
        last_receipt = await db.scalar(select(func.max(AnchorReceipt.submitted_at)))
        last_checkpoint = await db.scalar(select(func.max(MerkleCheckpoint.signed_at)))
    last = max((t for t in (last_receipt, last_checkpoint) if t is not None), default=None)
    return last is None or now - last >= after


async def anchor_fallback_tick() -> list[job.ClinicRun] | None:
    minutes = get_settings().anchor_fallback_minutes
    if minutes == 0 or not await anchor_is_overdue(timedelta(minutes=minutes)):
        return None
    logger.warning("anchor schedule is late; running the anchor job from the worker")
    backends = job.backends_from_settings()
    try:
        return await job.run(backends)
    finally:
        if backends.evm is not None:
            await backends.evm.close()


async def anchor_fallback_loop() -> None:
    while True:
        try:
            await anchor_fallback_tick()
        except Exception:
            logger.exception("anchor fallback failed")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def main() -> None:
    configure_logging()
    await run_startup_tasks()
    try:
        await asyncio.gather(maintenance_loop(), anchor_fallback_loop())
    finally:
        await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(main())
