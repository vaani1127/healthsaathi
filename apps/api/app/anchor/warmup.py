"""Warm the proof and verification caches when an API process starts (ADR 0011).

Both caches are per process and empty after a start, so the first integrity check paid for
building the proof tree and the first admin audit page paid for the full check of the whole log:
on v0.1.9 those first calls took 5.88 s and 14.39 s on the VM. A background task started by the
lifespan does that work instead, so it never blocks startup or /ready: for each clinic, one
after another (never two at once, to keep memory low), it builds the proof tree up to the latest
checkpoint (tree_cache.tree_for) and runs the full audit verification once (verified_tree, whose
hashing runs in a worker thread). A failure is logged and ignored: the caches then fill on the
first request, as before. Each API worker process warms its own caches.

Skipped when APP_ENV=test unless `enabled_in_tests` is set.
"""

import asyncio
import logging
import time
import uuid

from sqlalchemy import select

from app.anchor.receipts import verified_tree
from app.core.config import get_settings
from app.core.db import ANONYMOUS, TenantContext, tenant_session
from app.db.models import Clinic
from app.ledger import tree_cache
from app.ledger.checkpoint import SYSTEM_ROLE, latest_checkpoint

logger = logging.getLogger(__name__)

enabled_in_tests = False
# The running or finished warm-up of this process, if any (tests await it).
task: asyncio.Task[dict[str, float | None]] | None = None


async def warm_clinic(clinic_id: uuid.UUID) -> None:
    async with tenant_session(TenantContext(clinic_id=clinic_id, role=SYSTEM_ROLE)) as db:
        latest = await latest_checkpoint(db, clinic_id)
        if latest is not None:
            await tree_cache.tree_for(db, clinic_id, latest.tree_size)
        await verified_tree(db, clinic_id)


async def warm_caches(clinic_ids: list[uuid.UUID] | None = None) -> dict[str, float | None]:
    """Warm every clinic (or the given ones) in turn. Returns seconds per clinic, None for a
    clinic whose warm-up failed. Never raises."""
    start = time.perf_counter()
    seconds: dict[str, float | None] = {}
    try:
        if clinic_ids is None:
            async with tenant_session(ANONYMOUS) as db:
                clinic_ids = list(await db.scalars(select(Clinic.id).order_by(Clinic.created_at)))
        logger.info("warmup start", extra={"clinics": len(clinic_ids)})
        for clinic_id in clinic_ids:
            t = time.perf_counter()
            try:
                await warm_clinic(clinic_id)
                seconds[str(clinic_id)] = round(time.perf_counter() - t, 3)
            except Exception:
                seconds[str(clinic_id)] = None
                logger.exception("warmup failed for a clinic", extra={"clinic_id": str(clinic_id)})
    except Exception:
        logger.exception("warmup failed")
    logger.info(
        "warmup done",
        extra={"seconds": round(time.perf_counter() - start, 3), "clinic_seconds": seconds},
    )
    return seconds


def start_warmup() -> asyncio.Task[dict[str, float | None]] | None:
    """Start the warm-up in the background (called by the lifespan), or None when skipped."""
    global task
    if get_settings().env == "test" and not enabled_in_tests:
        return None
    task = asyncio.create_task(warm_caches())
    return task
