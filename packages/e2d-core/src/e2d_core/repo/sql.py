"""Repository backend over the product database (async SQLAlchemy, plain SQL).

Runs inside the caller's session, so it sees the same snapshot as the request and is limited by
the same row level security policies.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from e2d_core.explain.model import (
    AppointmentEv,
    BreakGlassEv,
    CareTeamEv,
    ClinicContext,
    EncounterEv,
    EvidenceBundle,
    EvidenceQuery,
    InvoiceEv,
    LabOrderEv,
    OpenHours,
    PatientEv,
    ProgressEv,
    QueueTokenEv,
    ReferralEv,
    ShiftEv,
    StatusEv,
    UserContext,
    status_order,
)
from e2d_core.repo import (
    APPOINTMENT_LOOKAHEAD,
    APPOINTMENT_LOOKBACK,
    ASSIGNMENT_GRACE,
    CREATION_WINDOW,
    ENCOUNTER_LOOKBACK,
    INVOICE_LOOKBACK,
    LAB_LOOKBACK,
    PROGRESS_LOOKBACK,
    WINDOW_MARGIN,
    median,
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
ENCOUNTERS_SQL = text(
    "SELECT id, patient_id, doctor_user_id, started_at, ended_at, created_by, created_at"
    " FROM encounters"
    " WHERE clinic_id = :clinic AND patient_id = :patient"
    " AND started_at BETWEEN :start AND :end ORDER BY started_at, id"
)
LAB_ORDERS_SQL = text(
    "SELECT o.id, o.patient_id, o.ordered_by, o.status, o.created_at, o.updated_at,"
    " r.released_at FROM lab_orders o LEFT JOIN lab_results r"
    " ON r.clinic_id = o.clinic_id AND r.lab_order_id = o.id"
    " WHERE o.clinic_id = :clinic AND o.patient_id = :patient"
    " AND o.created_at BETWEEN :start AND :end ORDER BY o.created_at, o.id"
)
REFERRALS_SQL = text(
    "SELECT id, patient_id, from_user_id, to_user_id, valid_until, status, created_by,"
    " created_at, updated_at FROM referrals"
    " WHERE clinic_id = :clinic AND patient_id = :patient AND to_user_id = :user"
    " AND created_at <= :end AND valid_until >= :start ORDER BY created_at, id"
)
CARE_TEAM_SQL = text(
    "SELECT id, patient_id, user_id, role, starts_at, ends_at, status, created_by, created_at"
    " FROM care_team_assignments"
    " WHERE clinic_id = :clinic AND patient_id = :patient AND user_id = :user"
    " AND starts_at <= :end AND (ends_at IS NULL OR ends_at >= :start)"
    " ORDER BY starts_at, id"
)
BREAK_GLASS_SQL = text(
    "SELECT id, user_id, patient_id, reason_code, at FROM break_glass_events"
    " WHERE clinic_id = :clinic AND patient_id = :patient AND user_id = :user"
    " AND at BETWEEN :start AND :end ORDER BY at, id"
)
PROGRESS_SQL = text(
    "SELECT kind, at FROM ("
    " SELECT 'vitals' AS kind, recorded_at AS at FROM vitals"
    "  WHERE clinic_id = :clinic AND patient_id = :patient"
    " UNION ALL SELECT 'note', created_at FROM clinical_notes"
    "  WHERE clinic_id = :clinic AND patient_id = :patient"
    " UNION ALL SELECT 'prescription', created_at FROM prescriptions"
    "  WHERE clinic_id = :clinic AND patient_id = :patient"
    " UNION ALL SELECT 'lab_result', resulted_at FROM lab_results"
    "  WHERE clinic_id = :clinic AND patient_id = :patient"
    " UNION ALL SELECT 'invoice', created_at FROM invoices"
    "  WHERE clinic_id = :clinic AND patient_id = :patient"
    ") p WHERE at BETWEEN :start AND :end ORDER BY at, kind"
)
HOURS_SQL = text(
    "SELECT DISTINCT weekday, start_time, end_time FROM schedules"
    " WHERE clinic_id = :clinic AND status = 'active' ORDER BY weekday, start_time, end_time"
)
STATUSES_SQL = text(
    "SELECT entity_type, entity_id, status, at FROM status_events"
    " WHERE clinic_id = :clinic AND entity_id = ANY(:ids) AND at <= :end"
)
STATUS_AT_SQL = text(
    "SELECT status FROM status_events"
    " WHERE clinic_id = :clinic AND entity_type = :entity_type AND entity_id = :entity_id"
    ' AND at <= :at ORDER BY at DESC, status COLLATE "C" DESC LIMIT 1'
)
CREATION_COUNTS_SQL = text(
    "SELECT m.user_id, coalesce(sum(c.n), 0) AS n FROM memberships m"
    " LEFT JOIN ("
    "  SELECT created_by AS user_id, count(*) AS n FROM appointments"
    "   WHERE clinic_id = :clinic AND created_at BETWEEN :start AND :end GROUP BY created_by"
    "  UNION ALL SELECT ordered_by, count(*) FROM lab_orders"
    "   WHERE clinic_id = :clinic AND created_at BETWEEN :start AND :end GROUP BY ordered_by"
    "  UNION ALL SELECT created_by, count(*) FROM referrals"
    "   WHERE clinic_id = :clinic AND created_at BETWEEN :start AND :end GROUP BY created_by"
    "  UNION ALL SELECT created_by, count(*) FROM care_team_assignments"
    "   WHERE clinic_id = :clinic AND created_at BETWEEN :start AND :end GROUP BY created_by"
    " ) c ON c.user_id = m.user_id"
    " WHERE m.clinic_id = :clinic AND m.role = :role AND m.is_active"
    " GROUP BY m.user_id"
)


class SqlRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _all(self, sql: Any, params: dict[str, Any]) -> list[Any]:
        return list((await self.session.execute(sql, params)).all())

    async def evidence_for(self, query: EvidenceQuery) -> EvidenceBundle:
        at = query.at
        as_of = max(query.as_of or at, at)
        base = {"clinic": query.clinic_id, "patient": query.patient_id}
        user = {**base, "user": query.user_id}
        local_day = at.astimezone(ZoneInfo(query.timezone)).date()

        patient_row = (await self.session.execute(PATIENT_SQL, base)).first()
        appointments = await self._all(
            APPOINTMENTS_SQL,
            {**base, "start": at - APPOINTMENT_LOOKBACK, "end": at + APPOINTMENT_LOOKAHEAD},
        )
        tokens = await self._all(
            QUEUE_SQL,
            {
                **base,
                "day_from": local_day - timedelta(days=1),
                "day_to": local_day + timedelta(days=1),
            },
        )
        shifts = await self._all(
            SHIFTS_SQL,
            {
                "clinic": query.clinic_id,
                "user": query.user_id,
                "start": at - WINDOW_MARGIN,
                "end": at + WINDOW_MARGIN,
            },
        )
        invoices = await self._all(
            INVOICES_SQL, {**base, "start": at - INVOICE_LOOKBACK, "end": at + WINDOW_MARGIN}
        )
        encounters = await self._all(
            ENCOUNTERS_SQL, {**base, "start": at - ENCOUNTER_LOOKBACK, "end": at + WINDOW_MARGIN}
        )
        lab_orders = await self._all(
            LAB_ORDERS_SQL, {**base, "start": at - LAB_LOOKBACK, "end": at + WINDOW_MARGIN}
        )
        window = {"start": at - ASSIGNMENT_GRACE, "end": at + WINDOW_MARGIN}
        referrals = await self._all(REFERRALS_SQL, {**user, **window})
        care_team = await self._all(CARE_TEAM_SQL, {**user, **window})
        break_glass = await self._all(
            BREAK_GLASS_SQL, {**user, "start": at - WINDOW_MARGIN, "end": at + WINDOW_MARGIN}
        )
        progress = await self._all(
            PROGRESS_SQL, {**base, "start": at - PROGRESS_LOOKBACK, "end": as_of}
        )
        tracked = (
            ("appointment", appointments),
            ("lab_order", lab_orders),
            ("referral", referrals),
            ("invoice", invoices),
        )
        kinds = {r.id: kind for kind, rows in tracked for r in rows}
        statuses = await self._all(
            STATUSES_SQL, {"clinic": query.clinic_id, "ids": list(kinds), "end": as_of}
        )
        return EvidenceBundle(
            patient=PatientEv(*patient_row) if patient_row else None,
            appointments=tuple(AppointmentEv(*r) for r in appointments),
            queue_tokens=tuple(QueueTokenEv(*r) for r in tokens),
            shifts=tuple(ShiftEv(*r) for r in shifts),
            invoices=tuple(InvoiceEv(*r) for r in invoices),
            encounters=tuple(EncounterEv(*r) for r in encounters),
            lab_orders=tuple(LabOrderEv(*r) for r in lab_orders),
            referrals=tuple(ReferralEv(*r) for r in referrals),
            care_team=tuple(CareTeamEv(*r) for r in care_team),
            break_glass=tuple(BreakGlassEv(*r) for r in break_glass),
            progress=tuple(ProgressEv(*r) for r in progress),
            statuses=tuple(
                sorted(
                    (StatusEv(*r) for r in statuses if kinds.get(r.entity_id) == r.entity_type),
                    key=status_order,
                )
            ),
            clinic=await self._clinic_context(query),
            user=await self._user_context(query),
        )

    async def _clinic_context(self, query: EvidenceQuery) -> ClinicContext:
        hours = await self._all(HOURS_SQL, {"clinic": query.clinic_id})
        return ClinicContext(hours=tuple(OpenHours(*h) for h in hours))

    async def status_at(
        self, clinic_id: uuid.UUID, entity_type: str, entity_id: uuid.UUID, at: datetime
    ) -> str | None:
        row = (
            await self.session.execute(
                STATUS_AT_SQL,
                {"clinic": clinic_id, "entity_type": entity_type, "entity_id": entity_id, "at": at},
            )
        ).first()
        return None if row is None else str(row.status)

    async def _user_context(self, query: EvidenceQuery) -> UserContext:
        if query.role is None:
            return UserContext()
        rows = await self._all(
            CREATION_COUNTS_SQL,
            {
                "clinic": query.clinic_id,
                "role": query.role,
                "start": query.at - CREATION_WINDOW,
                "end": query.at,
            },
        )
        counts = {r.user_id: int(r.n) for r in rows}
        return UserContext(
            created_7d=counts.get(query.user_id, 0), role_median_7d=median(list(counts.values()))
        )
