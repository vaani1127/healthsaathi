import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app import worker
from app.core.config import Settings, get_settings
from app.core.db import TenantContext, tenant_session
from app.core.dburl import asyncpg_url, engine_options
from app.db.models import AuditEvent
from app.ledger.checkpoint import build_checkpoint
from app.ledger.writer import append_audit_event
from tests.helpers import make_clinic

IST = "Asia/Kolkata"
# 11:00 IST on a weekday, inside the default watch hours (09:30 to 20:00).
MORNING = datetime(2026, 11, 11, 5, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
async def clean_state() -> AsyncIterator[None]:
    worker.WatchState.warned.clear()
    yield
    worker.WatchState.warned.clear()
    for task in list(worker.TimerState.abandoned):
        task.cancel()
    worker.TimerState.abandoned.clear()


async def hang_forever() -> None:
    """A call that never returns, and whose clean-up also never ends once it is cancelled (as a
    statement on a dead connection can)."""
    try:
        await asyncio.Event().wait()
    finally:
        await asyncio.sleep(3600)


async def quick_watch() -> list[uuid.UUID]:
    return []


async def test_a_hanging_run_is_abandoned_and_the_timer_carries_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(get_settings(), "anchor_timer_limit_seconds", 1)
    monkeypatch.setattr(worker, "anchor_watch_tick", quick_watch)
    calls: list[str] = []

    async def stuck() -> None:
        calls.append("stuck")
        await hang_forever()

    monkeypatch.setattr(worker, "anchor_fallback_tick", stuck)
    caplog.set_level(logging.INFO, logger="app.worker")
    started = time.monotonic()
    outcome = await worker.anchor_timer_run()
    assert time.monotonic() - started < 5, "the run did not wait for the stuck clean-up"
    assert outcome == "abandoned: still running after 1 s"
    assert len(worker.TimerState.abandoned) == 1

    # The next run is not blocked by the abandoned one.
    async def not_due() -> None:
        calls.append("next")

    monkeypatch.setattr(worker, "anchor_fallback_tick", not_due)
    assert await worker.anchor_timer_run() == "not due"
    assert calls == ["stuck", "next"]

    messages = [r.getMessage() for r in caplog.records]
    assert messages.count("anchor timer start") == 2
    assert messages.count("anchor timer finish") == 2
    assert "anchor timer run abandoned" in messages
    finish = next(r for r in caplog.records if r.getMessage() == "anchor timer finish")
    assert {"started_at", "finished_at", "seconds", "outcome"} <= set(vars(finish))
    assert datetime.fromisoformat(vars(finish)["finished_at"]) >= datetime.fromisoformat(
        vars(finish)["started_at"]
    )


async def test_a_hanging_staleness_check_does_not_freeze_the_timer(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(get_settings(), "anchor_timer_limit_seconds", 1)

    async def not_due() -> None:
        return None

    monkeypatch.setattr(worker, "anchor_fallback_tick", not_due)
    monkeypatch.setattr(worker, "anchor_watch_tick", hang_forever)
    caplog.set_level(logging.INFO, logger="app.worker")
    assert await worker.anchor_timer_run() == "not due"
    assert "anchor staleness check abandoned" in [r.getMessage() for r in caplog.records]


async def test_a_failing_run_is_logged_and_the_loop_keeps_going(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    runs = 0

    async def broken() -> None:
        nonlocal runs
        runs += 1
        if runs == 3:
            raise asyncio.CancelledError  # stop the loop for the test
        raise RuntimeError("rpc down")

    monkeypatch.setattr(worker, "anchor_fallback_tick", broken)
    monkeypatch.setattr(worker, "anchor_watch_tick", quick_watch)
    monkeypatch.setattr(worker, "CHECK_INTERVAL_SECONDS", 0)
    with pytest.raises(asyncio.CancelledError):
        await worker.anchor_fallback_loop()
    assert runs == 3
    assert [r.getMessage() for r in caplog.records].count("anchor fallback failed") == 2


async def test_run_limited_returns_results_and_errors_in_time() -> None:
    async def answer() -> int:
        return 42

    async def fails() -> None:
        raise ValueError("bad")

    assert await worker.run_limited(answer(), 1) == 42
    with pytest.raises(ValueError, match="bad"):
        await worker.run_limited(fails(), 1)


def state(
    *,
    anchor: timedelta | None,
    waiting: timedelta | None,
    now: datetime = MORNING,
) -> worker.ClinicAnchorState:
    """A clinic whose last anchor was `anchor` ago and whose oldest event not yet in a checkpoint
    was written `waiting` ago (None: nothing is waiting)."""
    return worker.ClinicAnchorState(
        uuid.uuid4(),
        IST,
        None if anchor is None else now - anchor,
        None if anchor is None else now - anchor,
        None if waiting is None else now - waiting,
    )


def test_stale_means_an_event_has_waited_30_minutes_in_opd_hours() -> None:
    minute = timedelta(minutes=1)
    stuck = state(anchor=50 * minute, waiting=35 * minute)
    fresh = state(anchor=10 * minute, waiting=2 * minute)
    quiet = state(anchor=90 * minute, waiting=None)
    never = state(anchor=None, waiting=40 * minute)
    found = dict(
        (s.clinic_id, waited)
        for s, waited in worker.stale_clinics(
            MORNING, [stuck, fresh, quiet, never], 30, "09:30-20:00"
        )
    )
    assert found == {stuck.clinic_id: 35.0, never.clinic_id: 40.0}

    # The same stuck clinic outside the watch hours (21:30 IST) is not reported.
    night = MORNING + timedelta(hours=10, minutes=30)
    late = state(anchor=50 * minute, waiting=35 * minute, now=night)
    assert worker.stale_clinics(night, [late], 30, "09:30-20:00") == []


def test_the_first_events_of_a_session_are_not_a_stall() -> None:
    """11 Nov 2026, 04:01 UTC: the old rule warned for both clinics, because the last anchor was
    711 minutes old (the night), although the first events had waited only a minute."""
    minute = timedelta(minutes=1)
    opening = datetime(2026, 11, 11, 4, 1, 17, tzinfo=UTC)
    session_start = state(anchor=711 * minute, waiting=minute, now=opening)
    assert worker.stale_clinics(opening, [session_start], 30, "09:30-20:00") == []
    # If the timer then never anchors them, the warning comes once they have waited 30 minutes.
    later = opening + 29 * minute
    assert worker.stale_clinics(later, [session_start], 30, "09:30-20:00") == [
        (session_start, 30.0)
    ]


def test_watch_hours_are_in_the_clinic_time() -> None:
    hours = "09:30-17:00"
    assert worker.in_watch_hours(datetime(2026, 11, 11, 4, 0, tzinfo=UTC), IST, hours)  # 09:30
    assert not worker.in_watch_hours(datetime(2026, 11, 11, 3, 59, tzinfo=UTC), IST, hours)
    assert not worker.in_watch_hours(datetime(2026, 11, 11, 11, 30, tzinfo=UTC), IST, hours)


async def test_a_stale_clinic_is_warned_about_every_30_minutes_not_every_run(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    minute = timedelta(minutes=1)
    clock = {"now": MORNING}
    stuck = state(anchor=50 * minute, waiting=35 * minute)

    async def states() -> tuple[datetime, list[worker.ClinicAnchorState]]:
        return clock["now"], [stuck]

    monkeypatch.setattr(worker, "clinic_anchor_states", states)
    monkeypatch.setattr(get_settings(), "anchor_stale_minutes", 30)
    monkeypatch.setattr(get_settings(), "anchor_watch_hours", "09:30-20:00")
    caplog.set_level(logging.WARNING, logger="app.worker")
    assert await worker.anchor_watch_tick() == [stuck.clinic_id]
    clock["now"] = MORNING + 10 * minute
    assert await worker.anchor_watch_tick() == []
    clock["now"] = MORNING + 30 * minute
    assert await worker.anchor_watch_tick() == [stuck.clinic_id]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert vars(warnings[0])["clinic_id"] == str(stuck.clinic_id)
    assert vars(warnings[0])["minutes_waiting"] == 35.0

    monkeypatch.setattr(get_settings(), "anchor_stale_minutes", 0)
    clock["now"] = MORNING + 90 * minute
    assert await worker.anchor_watch_tick() == [], "0 turns the warning off"


async def test_states_come_from_the_database(admin_engine: AsyncEngine) -> None:
    clinic = await make_clinic(admin_engine)

    async def add_event() -> None:
        async with tenant_session(TenantContext(clinic_id=clinic, role="system")) as db:
            await append_audit_event(db, clinic, "test", {"i": 0})

    async def mine() -> worker.ClinicAnchorState:
        _, states = await worker.clinic_anchor_states()
        return next(s for s in states if s.clinic_id == clinic)

    await add_event()
    first = await mine()
    assert first.timezone == IST
    assert first.last_anchor is None and first.last_checkpoint is None
    assert first.oldest_waiting is not None, "never checkpointed: the first event is waiting"

    await build_checkpoint(clinic)
    covered = await mine()
    assert covered.last_checkpoint is not None and covered.oldest_waiting is None

    # The next event is waiting at once, whatever the clocks say (positions, not times).
    await add_event()
    await add_event()
    later = await mine()
    async with tenant_session(TenantContext(clinic_id=clinic, role="system")) as db:
        times = (
            await db.scalars(
                select(AuditEvent.at).where(AuditEvent.clinic_id == clinic).order_by(AuditEvent.seq)
            )
        ).all()
    assert later.oldest_waiting == times[1], "the first event after the checkpoint"


def test_every_statement_gets_the_command_timeout() -> None:
    direct = asyncpg_url("postgresql://u:p@db.example.org:5432/hs?sslmode=require")
    assert engine_options(direct, command_timeout=30)["connect_args"] == {"command_timeout": 30}
    assert "connect_args" not in engine_options(direct)
    pooled = asyncpg_url("postgresql://u:p@ep-x-pooler.neon.tech/hs?sslmode=require")
    assert engine_options(pooled, command_timeout=30)["connect_args"]["command_timeout"] == 30


def test_watch_hours_setting_is_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANCHOR_WATCH_HOURS", "morning")
    with pytest.raises(ValidationError):
        Settings()


def test_blank_command_timeout_means_no_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_COMMAND_TIMEOUT_SECONDS", "")
    assert Settings().db_command_timeout_seconds is None
    monkeypatch.setenv("DB_COMMAND_TIMEOUT_SECONDS", "120")
    assert Settings().db_command_timeout_seconds == 120


def test_default_watch_hours_cover_both_opd_sessions() -> None:
    hours = Settings.model_fields["anchor_watch_hours"].default
    assert hours == "09:30-20:00"
    minute = timedelta(minutes=1)
    # 19:00 IST, in the evening session: a stall there is now reported.
    evening = datetime(2026, 11, 11, 13, 30, tzinfo=UTC)
    stuck = state(anchor=50 * minute, waiting=35 * minute, now=evening)
    assert [s.clinic_id for s, _ in worker.stale_clinics(evening, [stuck], 30, hours)] == [
        stuck.clinic_id
    ]
    # 15:00 IST, in the break between sessions: the morning's events are anchored and nothing new
    # is waiting, so a long time since the last anchor is quiet, not stuck.
    lunch = datetime(2026, 11, 11, 9, 30, tzinfo=UTC)
    quiet = state(anchor=80 * minute, waiting=None, now=lunch)
    assert worker.stale_clinics(lunch, [quiet], 30, hours) == []
    # 17:01 IST, the evening session's first event after the break: not a stall.
    reopening = datetime(2026, 11, 11, 11, 31, tzinfo=UTC)
    resumed = state(anchor=200 * minute, waiting=minute, now=reopening)
    assert worker.stale_clinics(reopening, [resumed], 30, hours) == []
    # 20:00 IST is the end of the watch hours.
    closing = datetime(2026, 11, 11, 14, 30, tzinfo=UTC)
    late = state(anchor=50 * minute, waiting=35 * minute, now=closing)
    assert worker.stale_clinics(closing, [late], 30, hours) == []
