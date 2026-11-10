"""Background worker process (SPEC 3).

- Detection: every DETECT_INTERVAL_SECONDS it scores the day's accesses of each clinic and
  raises alerts; once a day it fits the models on the last 30 days.
- Web push: sends the queued access notifications every few seconds.
- Anchor timer: the GitHub Actions schedule is the main trigger for the anchor job; if it has
  not run for ANCHOR_FALLBACK_MINUTES the worker runs the job itself. The job's advisory lock
  keeps the two from overlapping. Each timer run is logged at its start and finish and is
  abandoned after ANCHOR_TIMER_LIMIT_SECONDS, so one call that never returns cannot freeze the
  timer (from 5 to 10 November 2026 it did). Each run also warns when a clinic has events waiting
  and no anchor for ANCHOR_STALE_MINUTES inside ANCHOR_WATCH_HOURS, in the clinic's own time.

    uv run python -m app.worker
"""

import asyncio
import logging
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text

from app.anchor import job
from app.core.config import get_settings
from app.core.db import TenantContext, get_engine, tenant_session, wait_for_database
from app.core.logging import configure_logging
from app.db.models import AnchorReceipt, AuditEvent, Clinic, MerkleCheckpoint
from app.detect import service as detect
from app.notify import push
from app.startup import maintenance_loop, run_startup_tasks

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 60
PUSH_INTERVAL_SECONDS = 10
FIT_EVERY = timedelta(hours=24)
FIT_MARKER = "last_fit.txt"
# A clinic that stays stale is warned about again after this long.
WARN_AGAIN_AFTER = timedelta(minutes=30)


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


class FallbackState:
    """When this worker last ran the job itself. A quiet clinic makes no new checkpoint, so without
    this the job would run again every minute once the schedule was late."""

    last_run: datetime | None = None


async def anchor_fallback_tick(now: datetime | None = None) -> list[job.ClinicRun] | None:
    minutes = get_settings().anchor_fallback_minutes
    if minutes == 0:
        return None
    after = timedelta(minutes=minutes)
    now = now or datetime.now(UTC)
    if FallbackState.last_run is not None and now - FallbackState.last_run < after:
        return None
    if not await anchor_is_overdue(after):
        return None
    FallbackState.last_run = now
    logger.warning("anchor schedule is late; running the anchor job from the worker")
    backends = job.backends_from_settings()
    try:
        return await job.run(backends)
    finally:
        if backends.evm is not None:
            await backends.evm.close()


class TimerRunAbandonedError(Exception):
    pass


class TimerState:
    """Runs that passed their time limit. They are cancelled and not waited for (a cancelled call
    on a dead connection may not end soon); keeping a reference stops them being collected."""

    abandoned: ClassVar[set[asyncio.Task[Any]]] = set()


async def run_limited[T](work: Coroutine[Any, Any, T], seconds: float) -> T:
    """The result of `work`, or TimerRunAbandonedError once it has run for `seconds`. Unlike
    asyncio.wait_for, this does not wait for the cancelled work to finish."""
    task = asyncio.ensure_future(work)
    done, _ = await asyncio.wait({task}, timeout=seconds)
    if task not in done:
        task.cancel()
        TimerState.abandoned.add(task)
        task.add_done_callback(TimerState.abandoned.discard)
        raise TimerRunAbandonedError(f"still running after {seconds:g} s")
    return task.result()


@dataclass(frozen=True)
class ClinicAnchorState:
    clinic_id: uuid.UUID
    timezone: str
    last_anchor: datetime | None
    last_checkpoint: datetime | None
    last_event: datetime | None


async def clinic_anchor_states() -> tuple[datetime, list[ClinicAnchorState]]:
    """The database clock, and per clinic its last anchor attempt, checkpoint and audit event."""
    last_anchor = (
        select(func.max(AnchorReceipt.submitted_at))
        .where(AnchorReceipt.clinic_id == Clinic.id)
        .scalar_subquery()
    )
    last_checkpoint = (
        select(func.max(MerkleCheckpoint.signed_at))
        .where(MerkleCheckpoint.clinic_id == Clinic.id)
        .scalar_subquery()
    )
    last_event = (
        select(AuditEvent.at)
        .where(AuditEvent.clinic_id == Clinic.id)
        .order_by(AuditEvent.seq.desc())
        .limit(1)
        .scalar_subquery()
    )
    async with tenant_session(TenantContext(role="system", db_role=job.JOB_ROLE)) as db:
        now = await db.scalar(text("SELECT clock_timestamp()"))
        rows = (
            await db.execute(
                select(Clinic.id, Clinic.timezone, last_anchor, last_checkpoint, last_event)
            )
        ).all()
    return now, [ClinicAnchorState(*row) for row in rows]


def in_watch_hours(at: datetime, timezone: str, hours: str) -> bool:
    start, end = (time.fromisoformat(part) for part in hours.split("-"))
    return start <= at.astimezone(ZoneInfo(timezone)).time() < end


def stale_clinics(
    now: datetime, states: list[ClinicAnchorState], minutes: int, hours: str
) -> list[tuple[ClinicAnchorState, float | None]]:
    """Clinics inside their watch hours with events newer than their last checkpoint (work is
    waiting) and no anchor attempt for `minutes`, with the minutes since their last anchor (None:
    never anchored). A clinic with nothing new to anchor is quiet, not stuck, and is left out."""
    out: list[tuple[ClinicAnchorState, float | None]] = []
    for s in states:
        waiting = s.last_event is not None and (
            s.last_checkpoint is None or s.last_event > s.last_checkpoint
        )
        if not waiting or not in_watch_hours(now, s.timezone, hours):
            continue
        if s.last_anchor is None:
            out.append((s, None))
            continue
        idle = (now - s.last_anchor).total_seconds() / 60
        if idle >= minutes:
            out.append((s, idle))
    return out


class WatchState:
    """When each clinic was last warned about, so a long stall warns every 30 minutes, not every
    minute."""

    warned: ClassVar[dict[uuid.UUID, datetime]] = {}


async def anchor_watch_tick() -> list[uuid.UUID]:
    """Logs a warning for each stale clinic (see stale_clinics); returns the clinics warned now."""
    s = get_settings()
    if s.anchor_stale_minutes == 0:
        return []
    now, states = await clinic_anchor_states()
    warned = []
    for state, idle in stale_clinics(now, states, s.anchor_stale_minutes, s.anchor_watch_hours):
        last = WatchState.warned.get(state.clinic_id)
        if last is not None and now - last < WARN_AGAIN_AFTER:
            continue
        WatchState.warned[state.clinic_id] = now
        warned.append(state.clinic_id)
        logger.warning(
            "no anchor inside OPD hours while events wait",
            extra={
                "clinic_id": str(state.clinic_id),
                "minutes_since_anchor": None if idle is None else round(idle, 1),
                "last_anchor": state.last_anchor,
                "last_event": state.last_event,
            },
        )
    return warned


async def anchor_timer_run() -> str:
    """One run of the anchor timer: the fallback, then the staleness check, each abandoned after
    ANCHOR_TIMER_LIMIT_SECONDS. Logged at its start and finish; returns the outcome."""
    limit = get_settings().anchor_timer_limit_seconds
    started = datetime.now(UTC)
    logger.info("anchor timer start", extra={"started_at": started.isoformat()})
    try:
        runs = await run_limited(anchor_fallback_tick(), limit)
        outcome = "not due" if runs is None else f"anchor job ran for {len(runs)} clinics"
    except TimerRunAbandonedError as exc:
        outcome = f"abandoned: {exc}"
        logger.error("anchor timer run abandoned", extra={"started_at": started.isoformat()})
    except Exception:
        outcome = "failed"
        logger.exception("anchor fallback failed")
    try:
        await run_limited(anchor_watch_tick(), limit)
    except TimerRunAbandonedError:
        logger.error("anchor staleness check abandoned")
    except Exception:
        logger.exception("anchor staleness check failed")
    finished = datetime.now(UTC)
    logger.info(
        "anchor timer finish",
        extra={
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "seconds": round((finished - started).total_seconds(), 3),
            "outcome": outcome,
        },
    )
    return outcome


async def anchor_fallback_loop() -> None:
    while True:
        await anchor_timer_run()
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
        await detect.fit_models(store=store)  # fits up to the database clock
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


async def push_loop() -> None:
    """Sends queued web push messages (patients who opted in to access notifications)."""
    while True:
        try:
            await push.send_pending()
        except Exception:
            logger.exception("push sending failed")
        await asyncio.sleep(PUSH_INTERVAL_SECONDS)


async def main() -> None:
    configure_logging()
    await wait_for_database()
    await run_startup_tasks()
    try:
        await asyncio.gather(maintenance_loop(), anchor_fallback_loop(), detect_loop(), push_loop())
    finally:
        await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(main())
