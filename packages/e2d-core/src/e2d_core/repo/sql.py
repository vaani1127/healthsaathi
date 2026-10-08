"""Repository backend over the product database (async SQLAlchemy, plain SQL).

Runs inside the caller's session, so it sees the same snapshot as the request and is limited by
the same row level security policies.
"""

from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from e2d_core.explain.model import (
    AppointmentEv,
    EvidenceBundle,
    EvidenceQuery,
    InvoiceEv,
    PatientEv,
    QueueTokenEv,
    ShiftEv,
)
from e2d_core.repo import (
    APPOINTMENT_LOOKAHEAD,
    APPOINTMENT_LOOKBACK,
    INVOICE_LOOKBACK,
    SHIFT_MARGIN,
)

PATIENT_SQL = text(
    "SELECT id, user_id, created_by, created_at FROM patients"
    " WHERE clinic_id = :clinic AND id = :patient"
)
APPOINTMENTS_SQL = text(
    "SELECT id, patient_id, doctor_user_id, slot_start, slot_end, kind, status, source,"
    " created_by, created_at, updated_at FROM appointments"
    " WHERE clinic_id = :clinic AND patient_id = :patient"
    " AND slot_start BETWEEN :start AND :end ORDER BY slot_start, id"
)
QUEUE_SQL = text(
    "SELECT q.id, q.appointment_id, a.patient_id, q.token_no, q.day, q.status,"
    " q.assigned_nurse_user_id, q.created_by, q.created_at"
    " FROM queue_tokens q JOIN appointments a"
    " ON a.clinic_id = q.clinic_id AND a.id = q.appointment_id"
    " WHERE q.clinic_id = :clinic AND a.patient_id = :patient"
    " AND q.day BETWEEN :day_from AND :day_to ORDER BY q.day, q.token_no"
)
SHIFTS_SQL = text(
    "SELECT id, user_id, role, starts_at, ends_at, status FROM shifts"
    " WHERE clinic_id = :clinic AND user_id = :user"
    " AND starts_at <= :end AND ends_at >= :start ORDER BY starts_at, id"
)
INVOICES_SQL = text(
    "SELECT id, patient_id, status, created_by, created_at, updated_at FROM invoices"
    " WHERE clinic_id = :clinic AND patient_id = :patient"
    " AND created_at BETWEEN :start AND :end ORDER BY created_at, id"
)


class SqlRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def evidence_for(self, query: EvidenceQuery) -> EvidenceBundle:
        at = query.at
        base = {"clinic": query.clinic_id, "patient": query.patient_id}
        local_day = at.astimezone(ZoneInfo(query.timezone)).date()

        patient_row = (await self.session.execute(PATIENT_SQL, base)).first()
        appointments = (
            await self.session.execute(
                APPOINTMENTS_SQL,
                {**base, "start": at - APPOINTMENT_LOOKBACK, "end": at + APPOINTMENT_LOOKAHEAD},
            )
        ).all()
        tokens = (
            await self.session.execute(
                QUEUE_SQL,
                {
                    **base,
                    "day_from": local_day - timedelta(days=1),
                    "day_to": local_day + timedelta(days=1),
                },
            )
        ).all()
        shifts = (
            await self.session.execute(
                SHIFTS_SQL,
                {
                    "clinic": query.clinic_id,
                    "user": query.user_id,
                    "start": at - SHIFT_MARGIN,
                    "end": at + SHIFT_MARGIN,
                },
            )
        ).all()
        invoices = (
            await self.session.execute(
                INVOICES_SQL,
                {**base, "start": at - INVOICE_LOOKBACK, "end": at + timedelta(days=1)},
            )
        ).all()

        return EvidenceBundle(
            patient=PatientEv(*patient_row) if patient_row else None,
            appointments=tuple(AppointmentEv(*r) for r in appointments),
            queue_tokens=tuple(QueueTokenEv(*r) for r in tokens),
            shifts=tuple(ShiftEv(*r) for r in shifts),
            invoices=tuple(InvoiceEv(*r) for r in invoices),
        )
