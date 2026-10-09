"""Status changes made through the ORM are written to status_events, and SqlRepository.status_at
reads the status a record had at a past time."""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.db import TenantContext, tenant_session
from app.db import models as m
from app.db.enums import AppointmentStatus, Role
from e2d_core.repo.sql import SqlRepository
from tests.factories import build_clinic_graph


async def _history(ctx: TenantContext, entity_id: uuid.UUID) -> list[tuple[str, datetime]]:
    async with tenant_session(ctx) as s:
        rows = await s.execute(
            select(m.StatusEvent.status, m.StatusEvent.at)
            .where(m.StatusEvent.entity_id == entity_id)
            .order_by(m.StatusEvent.at)
        )
        return [(r.status, r.at) for r in rows]


async def test_status_changes_are_recorded_in_order(admin_engine: AsyncEngine) -> None:
    graph = await build_clinic_graph(admin_engine, "status-history")
    ctx = graph.ctx(Role.DOCTOR)
    async with tenant_session(ctx) as s:
        appt = (
            await s.execute(select(m.Appointment).where(m.Appointment.clinic_id == graph.clinic_id))
        ).scalar_one()
        appt_id = appt.id
    for status in (AppointmentStatus.CHECKED_IN, AppointmentStatus.COMPLETED):
        async with tenant_session(ctx) as s:
            row = await s.get(m.Appointment, appt_id)
            assert row is not None
            row.status = status
    # Setting the same status again is not a change.
    async with tenant_session(ctx) as s:
        row = await s.get(m.Appointment, appt_id)
        assert row is not None
        row.status = AppointmentStatus.COMPLETED

    history = await _history(ctx, appt_id)
    assert [status for status, _ in history] == ["booked", "checked_in", "completed"]
    (_, booked), (_, checked_in), (_, completed) = history
    assert booked < checked_in < completed

    async with tenant_session(ctx) as s:
        repo = SqlRepository(s)
        cid = graph.clinic_id
        before = booked - timedelta(seconds=1)
        assert await repo.status_at(cid, "appointment", appt_id, before) is None
        assert await repo.status_at(cid, "appointment", appt_id, booked) == "booked"
        assert await repo.status_at(cid, "appointment", appt_id, checked_in) == "checked_in"
        assert await repo.status_at(cid, "appointment", appt_id, completed) == "completed"


async def test_new_rows_record_their_default_status(admin_engine: AsyncEngine) -> None:
    graph = await build_clinic_graph(admin_engine, "status-default")
    ctx = graph.ctx(Role.DOCTOR)
    async with tenant_session(ctx) as s:
        kinds = (
            await s.execute(
                select(m.StatusEvent.entity_type, m.StatusEvent.status).where(
                    m.StatusEvent.clinic_id == graph.clinic_id
                )
            )
        ).all()
    # The factory sets no invoice status, so the invoice starts as a draft (the column default).
    assert {(str(kind), status) for kind, status in kinds} >= {
        ("appointment", "booked"),
        ("invoice", "draft"),
        ("lab_order", "ordered"),
        ("referral", "active"),
    }
