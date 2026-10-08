"""Background worker process (SPEC 3).

- Detection: every DETECT_INTERVAL_SECONDS it scores the day's accesses of each clinic and
  raises alerts; once a day it fits the models on the last 30 days.
- Anchor fallback: the GitHub Actions schedule is the main trigger for the anchor job; if it has
  not run for ANCHOR_FALLBACK_MINUTES the worker runs the job itself. The job's advisory lock
  keeps the two from overlapping.

    uv run python -m app.worker
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text

from app.anchor import job
from app.core.config import get_settings
from app.core.db import TenantContext, get_engine, tenant_session
from app.core.logging import configure_logging
from app.db.models import AnchorReceipt, MerkleCheckpoint
from app.detect import service as detect
from app.startup import maintenance_loop, run_startup_tasks

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 60
FIT_EVERY = timedelta(hours=24)
FIT_MARKER = "last_fit.txt"


async def anchor_is_overdue(after: timedelta, now: datetime | None = None) -> bool:
    """True when neither a checkpoint nor an anchor attempt happened in the `after` before `now`
    (default: the database clock)."""
    ctx = TenantContext(role="system", db_role=job.JOB_ROLE)
    async with tenant_session(ctx) as db:
        now = now or await db.scalar(text("SELECT clock_timestamp()"))
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


def fit_is_due(store: detect.ModelStore, now: datetime) -> bool:
    marker = store.root / FIT_MARKER
    if not marker.exists():
        return True
    last = datetime.fromisoformat(marker.read_text(encoding="utf-8").strip())
    return now - last >= FIT_EVERY


async def detect_tick(now: datetime | None = None) -> dict[str, int]:
    store = detect.model_store()
    now = now or datetime.now(UTC)
    if fit_is_due(store, now):
        await detect.fit_models(now, store=store)
        store.root.mkdir(parents=True, exist_ok=True)
        (store.root / FIT_MARKER).write_text(now.isoformat(), encoding="utf-8")
    return await detect.score_all()


async def detect_loop() -> None:
    interval = get_settings().detect_interval_seconds
    if interval == 0:
        return
    while True:
        try:
            await detect_tick()
        except Exception:
            logger.exception("detection failed")
        await asyncio.sleep(interval)


async def main() -> None:
    configure_logging()
    await run_startup_tasks()
    try:
        await asyncio.gather(maintenance_loop(), anchor_fallback_loop(), detect_loop())
    finally:
        await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(main())
