"""Clinic days for detection: when a day can be finalised, and which days already are.

A day is the clinic's local calendar day (clinics.timezone). Its final alert list can be made from
00:00 clinic time two days later, when the last access of the day has had its 24 hours
(ADR 0008). A day counts as finalised (or skipped) once its `detect_daily` audit event exists.
"""

import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditEvent

DAILY_KIND = "detect_daily"
READY_AFTER = timedelta(days=2)
# Default of DETECT_CATCH_UP_DAYS: how many due days a run may still finalise.
CATCH_UP_DAYS = 7


class Outcome:
    FINALISED = "finalised"
    SKIPPED = "skipped"


def day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def ready_after(day: date, tz: ZoneInfo) -> datetime:
    """When `day` can be finalised: 00:00 clinic time two days later."""
    return datetime.combine(day + READY_AFTER, time.min, tzinfo=tz).astimezone(UTC)


def due_window(today: date, catch_up: int = CATCH_UP_DAYS) -> list[date]:
    """The days a run may finalise, oldest first: the `catch_up` days that end two days before
    the clinic's today."""
    return [today - timedelta(days=k) for k in range(catch_up + 1, 1, -1)]


async def entries(
    db: AsyncSession, clinic_id: uuid.UUID, days: Iterable[date] | None = None
) -> dict[date, dict[str, Any]]:
    """The detect_daily audit payload of each day that has one (only `days`, if given)."""
    stmt = select(AuditEvent.payload).where(
        AuditEvent.clinic_id == clinic_id, AuditEvent.kind == DAILY_KIND
    )
    if days is not None:
        wanted = sorted({d.isoformat() for d in days})
        if not wanted:
            return {}
        stmt = stmt.where(AuditEvent.payload["day"].astext.in_(wanted))
    rows = (await db.scalars(stmt)).all()
    return {date.fromisoformat(p["day"]): p for p in rows}


async def entry_for(db: AsyncSession, clinic_id: uuid.UUID, day: date) -> dict[str, Any] | None:
    return (await entries(db, clinic_id, [day])).get(day)
