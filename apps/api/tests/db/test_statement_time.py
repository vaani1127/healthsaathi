"""Statement-time stamps (migration 0005, ADR 0008): evidence rows and status changes made after
an access in the same request are dated after it, so a later explanation of that access cannot
see them as earlier evidence."""

import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db import TenantContext, tenant_session
from app.db import models as m
from app.db.enums import Role
from app.detect import daily
from tests.helpers import Actor, make_actor, make_clinic, make_patient

IST = ZoneInfo("Asia/Kolkata")

# Kept in step with migrations/versions/20261116_0005_statement_time_defaults.py.
STATEMENT_TIME = {
    *(
        (table, "created_at")
        for table in (
            "patients",
            "appointments",
            "queue_tokens",
            "encounters",
            "referrals",
            "care_team_assignments",
            "invoices",
            "lab_orders",
            "shifts",
            "schedules",
            "clinical_notes",
            "prescriptions",
            "vitals",
        )
    ),
    ("status_events", "at"),
    ("vitals", "recorded_at"),
    ("lab_results", "resulted_at"),
    ("break_glass_events", "at"),
}
# Audit and anchoring stamps keep the transaction time (unchanged by 0005).
TRANSACTION_TIME = {
    ("audit_events", "at"),
    ("access_events", "at"),
    ("merkle_checkpoints", "signed_at"),
    ("anchor_receipts", "submitted_at"),
}


async def test_defaults_are_statement_time_for_evidence_and_unchanged_for_audit(
    admin_engine: AsyncEngine,
) -> None:
    async with admin_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT table_name, column_name, column_default FROM information_schema.columns"
                    " WHERE table_schema = 'public'"
                )
            )
        ).all()
    defaults = {(r.table_name, r.column_name): r.column_default for r in rows}
    assert {c: defaults[c] for c in STATEMENT_TIME} == dict.fromkeys(
        STATEMENT_TIME, "clock_timestamp()"
    )
    assert {c: defaults[c] for c in TRANSACTION_TIME} == dict.fromkeys(TRANSACTION_TIME, "now()")


@pytest.fixture
async def clinic(admin_engine: AsyncEngine) -> uuid.UUID:
    return await make_clinic(admin_engine)


@pytest.fixture
async def staff(admin_engine: AsyncEngine, clinic: uuid.UUID) -> dict[Role, Actor]:
    return {
        role: await make_actor(admin_engine, clinic, role)
        for role in (Role.RECEPTION, Role.NURSE, Role.DOCTOR)
    }


async def last_access(engine: AsyncEngine, user: uuid.UUID, patient: uuid.UUID) -> m.AccessEvent:
    async with AsyncSession(engine) as s:
        event = await s.scalar(
            select(m.AccessEvent)
            .where(m.AccessEvent.user_id == user, m.AccessEvent.patient_id == patient)
            .order_by(m.AccessEvent.at.desc())
            .limit(1)
        )
    assert event is not None
    return event


async def status_times(engine: AsyncEngine, entity: uuid.UUID) -> dict[str, datetime]:
    async with AsyncSession(engine) as s:
        rows = (
            await s.execute(
                select(m.StatusEvent.status, m.StatusEvent.at).where(
                    m.StatusEvent.entity_id == entity
                )
            )
        ).all()
    return {status: at for status, at in rows}


async def test_rows_made_after_an_access_in_the_same_request_are_stamped_after_it(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
) -> None:
    reception, nurse = staff[Role.RECEPTION], staff[Role.NURSE]
    patient = await make_patient(admin_engine, clinic, reception.user_id)

    # Walk-in: one request records the access, then creates the appointment and its token.
    r = await client.post(
        "/api/v1/walkins",
        json={"patient_id": str(patient), "doctor_user_id": str(staff[Role.DOCTOR].user_id)},
        headers=reception.headers,
    )
    assert r.status_code == 201, r.text
    appointment_id = uuid.UUID(r.json()["appointment"]["id"])
    token_id = uuid.UUID(r.json()["token"]["id"])
    access = await last_access(admin_engine, reception.user_id, patient)
    async with AsyncSession(admin_engine) as s:
        appointment = await s.get(m.Appointment, appointment_id)
        token = await s.get(m.QueueToken, token_id)
    assert appointment is not None and token is not None
    walkin_statuses = await status_times(admin_engine, appointment_id)
    assert access.at < appointment.created_at < token.created_at
    assert access.at < walkin_statuses["booked"] < walkin_statuses["checked_in"]
    assert access.at <= appointment.slot_start, "the slot comes from the database clock too"

    # Token change: the access is recorded, then the appointment goes to in consultation.
    r = await client.patch(
        f"/api/v1/queue/tokens/{token_id}", json={"status": "with_doctor"}, headers=nurse.headers
    )
    assert r.status_code == 200, r.text
    access = await last_access(admin_engine, nurse.user_id, patient)
    assert access.at < (await status_times(admin_engine, appointment_id))["in_consult"]

    # Cancel: a second, booked appointment is cancelled; the access comes first.
    r = await client.post(
        "/api/v1/appointments",
        json={
            "patient_id": str(patient),
            "doctor_user_id": str(staff[Role.DOCTOR].user_id),
            "slot_start": (datetime.now(IST) + timedelta(hours=2)).isoformat(),
        },
        headers=reception.headers,
    )
    assert r.status_code == 201, r.text
    booked = uuid.UUID(r.json()["id"])
    r = await client.patch(
        f"/api/v1/appointments/{booked}", json={"status": "cancelled"}, headers=reception.headers
    )
    assert r.status_code == 200, r.text
    access = await last_access(admin_engine, reception.user_id, patient)
    assert access.at < (await status_times(admin_engine, booked))["cancelled"]


async def test_an_access_in_the_request_that_cancels_its_own_appointment_is_flagged(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
) -> None:
    """The cancelling request records an edit of the patient explained by the appointment it
    is about to cancel. With the cancellation stamped after that access, the access's evidence
    was booked at the access and cancelled 24 hours later."""
    reception = staff[Role.RECEPTION]
    patient = await make_patient(admin_engine, clinic, reception.user_id)
    # Registered three days ago, so the appointment (not the registration) explains the
    # reception's access in the cancelling request.
    async with AsyncSession(admin_engine) as s, s.begin():
        await s.execute(
            text("UPDATE patients SET created_at = created_at - interval '3 days' WHERE id = :p"),
            {"p": patient},
        )
    r = await client.post(
        "/api/v1/appointments",
        json={
            "patient_id": str(patient),
            "doctor_user_id": str(staff[Role.DOCTOR].user_id),
            "slot_start": (datetime.now(IST) + timedelta(hours=2)).isoformat(),
        },
        headers=reception.headers,
    )
    assert r.status_code == 201, r.text
    appointment = r.json()["id"]
    r = await client.patch(
        f"/api/v1/appointments/{appointment}",
        json={"status": "cancelled"},
        headers=reception.headers,
    )
    assert r.status_code == 200, r.text
    access = await last_access(admin_engine, reception.user_id, patient)
    async with AsyncSession(admin_engine) as s:
        explanation = await s.get(m.AccessExplanation, access.id)
    assert explanation is not None and explanation.template_code == "T_FRONTDESK"
    assert explanation.evidence["refs"][0] == {"kind": "appointment", "id": appointment}

    day: date = access.at.astimezone(IST).date()
    async with tenant_session(TenantContext(clinic_id=clinic, role="system")) as db:
        late = (await daily.day_features(db, clinic, day, "Asia/Kolkata")).late
    assert late[str(access.id)].status == "decided"
    assert late[str(access.id)].flags["cancelled_after_access"] is True
