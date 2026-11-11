"""Background worker process (SPEC 3).

- Detection: every DETECT_INTERVAL_SECONDS it scores the day's accesses of each clinic and
  raises alerts; once a day it fits the models on the last 30 days.
- Web push: sends the queued access notifications every few seconds.
- Anchor timer: the GitHub Actions schedule is the main trigger for the anchor job; if it has
  not run for ANCHOR_FALLBACK_MINUTES the worker runs the job itself. The job's advisory lock
  keeps the two from overlapping. Each timer run is logged at its start and finish and is
  abandoned after ANCHOR_TIMER_LIMIT_SECONDS, so one call that never returns cannot freeze the
  timer (from 5 to 10 November 2026 it did). Each run also warns when a clinic's oldest event
  not yet in a checkpoint has waited ANCHOR_STALE_MINUTES inside ANCHOR_WATCH_HOURS, in the
  clinic's own time.

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
    # The oldest audit event not yet in a checkpoint (None: nothing is waiting).
    oldest_waiting: datetime | None


async def clinic_anchor_states() -> tuple[datetime, list[ClinicAnchorState]]:
    """The database clock, and per clinic its last anchor attempt, its last checkpoint and its
    oldest audit event not in that checkpoint.

    A checkpoint of size n covers the clinic's first n events in seq order, so the oldest waiting
    event is the one at position n. Positions are used, not times: signed_at comes from the app's
    clock and an event's time from the database's, and the two can differ by seconds."""
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
    covered = (
        select(func.max(MerkleCheckpoint.tree_size))
        .where(MerkleCheckpoint.clinic_id == Clinic.id)
        .scalar_subquery()
    )
    async with tenant_session(TenantContext(role="system", db_role=job.JOB_ROLE)) as db:
        now = await db.scalar(text("SELECT clock_timestamp()"))
        rows = (
            await db.execute(
                select(Clinic.id, Clinic.timezone, last_anchor, last_checkpoint, covered)
            )
        ).all()
        states = []
        for clinic_id, timezone, anchored, signed, size in rows:
            oldest_waiting = await db.scalar(
                select(AuditEvent.at)
                .where(AuditEvent.clinic_id == clinic_id)
                .order_by(AuditEvent.seq)
                .offset(size or 0)
                .limit(1)
            )
            states.append(ClinicAnchorState(clinic_id, timezone, anchored, signed, oldest_waiting))
    return now, states


def in_watch_hours(at: datetime, timezone: str, hours: str) -> bool:
    start, end = (time.fromisoformat(part) for part in hours.split("-"))
    return start <= at.astimezone(ZoneInfo(timezone)).time() < end


def stale_clinics(
    now: datetime, states: list[ClinicAnchorState], minutes: int, hours: str
) -> list[tuple[ClinicAnchorState, float]]:
    """Clinics inside their watch hours whose oldest event not yet in a checkpoint has waited at
    least `minutes`, with how long it has waited. The wait is measured from that event, not from
    the last anchor: after a quiet night the last anchor is hours old, but the first events of
    the session have only just arrived. A clinic with nothing waiting is quiet, not stuck."""
    out: list[tuple[ClinicAnchorState, float]] = []
    for s in states:
        if s.oldest_waiting is None or not in_watch_hours(now, s.timezone, hours):
            continue
        waited = (now - s.oldest_waiting).total_seconds() / 60
        if waited >= minutes:
            out.append((s, waited))
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
    for state, waited in stale_clinics(now, states, s.anchor_stale_minutes, s.anchor_watch_hours):
        last = WatchState.warned.get(state.clinic_id)
        if last is not None and now - last < WARN_AGAIN_AFTER:
            continue
        WatchState.warned[state.clinic_id] = now
        warned.append(state.clinic_id)
        logger.warning(
            "no anchor inside OPD hours while events wait",
            extra={
                "clinic_id": str(state.clinic_id),
                "minutes_waiting": round(waited, 1),
                "oldest_waiting_event": state.oldest_waiting,
                "last_anchor": state.last_anchor,
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
