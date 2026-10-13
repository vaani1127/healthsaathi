"""Workflow evidence for many patients at once, for list screens (SPEC 5.1).

`SqlRepository.evidence_for` sends about fifteen queries per patient. A list screen that shows
twenty patients would wait on three hundred round trips, which is slow when the database is far
away. Here the same tables are read once for all the patients with set-based queries, each a
superset of what `SqlRepository` reads for one patient, and the rows go into the in-memory
backend (`MemoryRepository`), which applies the exact windows and filters. The two backends are
kept identical by the shared backend tests, and tests/access/test_batch.py checks that the
bundles from here equal SqlRepository's for every template case.

All queries run in the caller's session, so they see the same snapshot and the same row level
security as the read being explained.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from e2d_core.explain import EvidenceBundle, EvidenceQuery
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
)
from e2d_core.repo.memory import COLUMNS, MemoryRepository, frames_from_rows

# Rows created in the last CREATION_WINDOW anywhere in the clinic are loaded too: the user
# context compares the user's own creation count with the median of their role.
PATIENTS_SQL = text(
    "SELECT id, clinic_id, user_id, created_by, created_at FROM patients"
    " WHERE clinic_id = :clinic AND id = ANY(:patients)"
)
APPOINTMENTS_SQL = text(
    "SELECT id, clinic_id, patient_id, doctor_user_id, slot_start, slot_end, kind, status,"
    " source, created_by, created_at, updated_at FROM appointments"
    " WHERE clinic_id = :clinic AND ("
    "  (patient_id = ANY(:patients) AND (slot_start BETWEEN :appt_start AND :appt_end"
    "   OR id IN (SELECT appointment_id FROM queue_tokens"
    "    WHERE clinic_id = :clinic AND day BETWEEN :day_from AND :day_to)))"
    "  OR created_at BETWEEN :made_start AND :at)"
)
QUEUE_SQL = text(
    "SELECT q.id, q.clinic_id, q.appointment_id, q.token_no, q.day, q.status,"
    " q.assigned_nurse_user_id, q.created_by, q.created_at"
    " FROM queue_tokens q JOIN appointments a"
    " ON a.clinic_id = q.clinic_id AND a.id = q.appointment_id"
    " WHERE q.clinic_id = :clinic AND a.patient_id = ANY(:patients)"
    " AND q.day BETWEEN :day_from AND :day_to"
)
SHIFTS_SQL = text(
    "SELECT id, clinic_id, user_id, role, starts_at, ends_at, status FROM shifts"
    " WHERE clinic_id = :clinic AND user_id = :user"
    " AND starts_at <= :margin_end AND ends_at >= :margin_start"
)
INVOICES_SQL = text(
    "SELECT id, clinic_id, patient_id, status, created_by, created_at, updated_at FROM invoices"
    " WHERE clinic_id = :clinic AND patient_id = ANY(:patients)"
    " AND created_at BETWEEN :invoice_start AND :margin_end"
)
ENCOUNTERS_SQL = text(
    "SELECT id, clinic_id, patient_id, doctor_user_id, started_at, appointment_id, created_by,"
    " created_at FROM encounters"
    " WHERE clinic_id = :clinic AND patient_id = ANY(:patients)"
    " AND started_at BETWEEN :encounter_start AND :margin_end"
)
LAB_ORDERS_SQL = text(
    "SELECT id, clinic_id, patient_id, ordered_by, status, created_at, updated_at FROM lab_orders"
    " WHERE clinic_id = :clinic AND ("
    "  (patient_id = ANY(:patients) AND created_at BETWEEN :lab_start AND :margin_end)"
    "  OR created_at BETWEEN :made_start AND :at)"
)
LAB_RESULTS_SQL = text(
    "SELECT id, clinic_id, lab_order_id, patient_id, resulted_at, released_at FROM lab_results"
    " WHERE clinic_id = :clinic AND patient_id = ANY(:patients) AND ("
    "  resulted_at BETWEEN :progress_start AND :at"
    "  OR lab_order_id IN (SELECT id FROM lab_orders WHERE clinic_id = :clinic"
    "   AND patient_id = ANY(:patients) AND created_at BETWEEN :lab_start AND :margin_end))"
)
REFERRALS_SQL = text(
    "SELECT id, clinic_id, patient_id, from_user_id, to_user_id, valid_until, status, created_by,"
    " created_at, updated_at FROM referrals"
    " WHERE clinic_id = :clinic AND ("
    "  (patient_id = ANY(:patients) AND to_user_id = :user"
    "   AND created_at <= :margin_end AND valid_until >= :grace_start)"
    "  OR created_at BETWEEN :made_start AND :at)"
)
CARE_TEAM_SQL = text(
    "SELECT id, clinic_id, patient_id, user_id, role, starts_at, ends_at, status, created_by,"
    " created_at FROM care_team_assignments"
    " WHERE clinic_id = :clinic AND ("
    "  (patient_id = ANY(:patients) AND user_id = :user"
    "   AND starts_at <= :margin_end AND (ends_at IS NULL OR ends_at >= :grace_start))"
    "  OR created_at BETWEEN :made_start AND :at)"
)
BREAK_GLASS_SQL = text(
    "SELECT id, clinic_id, user_id, patient_id, reason_code, at FROM break_glass_events"
    " WHERE clinic_id = :clinic AND patient_id = ANY(:patients) AND user_id = :user"
    " AND at BETWEEN :margin_start AND :margin_end"
)
# Vitals, notes and prescriptions only count as progress (the lab results and invoices that also
# count are loaded above).
PROGRESS_SQL = text(
    "SELECT 'vitals' AS tbl, id, clinic_id, patient_id, recorded_at AS t FROM vitals"
    " WHERE clinic_id = :clinic AND patient_id = ANY(:patients)"
    " AND recorded_at BETWEEN :progress_start AND :at"
    " UNION ALL SELECT 'clinical_notes', id, clinic_id, patient_id, created_at FROM clinical_notes"
    " WHERE clinic_id = :clinic AND patient_id = ANY(:patients)"
    " AND created_at BETWEEN :progress_start AND :at"
    " UNION ALL SELECT 'prescriptions', id, clinic_id, patient_id, created_at FROM prescriptions"
    " WHERE clinic_id = :clinic AND patient_id = ANY(:patients)"
    " AND created_at BETWEEN :progress_start AND :at"
)
SCHEDULES_SQL = text(
    "SELECT id, clinic_id, weekday, start_time, end_time, status FROM schedules"
    " WHERE clinic_id = :clinic AND status = 'active'"
)
MEMBERSHIPS_SQL = text(
    "SELECT id, clinic_id, user_id, role, is_active FROM memberships"
    " WHERE clinic_id = :clinic AND role = :role AND is_active"
)
STATUSES_SQL = text(
    "SELECT clinic_id, entity_type, entity_id, status, at FROM status_events"
    " WHERE clinic_id = :clinic AND entity_id = ANY(:ids) AND at <= :at"
)
PROGRESS_COLUMNS = {
    "vitals": "recorded_at",
    "clinical_notes": "created_at",
    "prescriptions": "created_at",
}


def _dicts(rows: Sequence[Any]) -> list[dict[str, Any]]:
    return [dict(r._mapping) for r in rows]


async def load_evidence(
    db: AsyncSession,
    clinic_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str,
    at: datetime,
    timezone: str,
    patient_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, EvidenceBundle]:
    """The evidence bundle of each patient for one access by `user_id` at `at`, the same bundle
    `SqlRepository.evidence_for` returns for that patient (with `as_of` left unset)."""
    local_day = at.astimezone(ZoneInfo(timezone)).date()
    params: dict[str, Any] = {
        "clinic": clinic_id,
        "patients": list(patient_ids),
        "user": user_id,
        "role": role,
        "at": at,
        "appt_start": at - APPOINTMENT_LOOKBACK,
        "appt_end": at + APPOINTMENT_LOOKAHEAD,
        "day_from": local_day - timedelta(days=1),
        "day_to": local_day + timedelta(days=1),
        "margin_start": at - WINDOW_MARGIN,
        "margin_end": at + WINDOW_MARGIN,
        "invoice_start": at - INVOICE_LOOKBACK,
        "encounter_start": at - ENCOUNTER_LOOKBACK,
        "lab_start": at - LAB_LOOKBACK,
        "grace_start": at - ASSIGNMENT_GRACE,
        "progress_start": at - PROGRESS_LOOKBACK,
        "made_start": at - CREATION_WINDOW,
    }

    async def rows(sql: Any) -> list[dict[str, Any]]:
        names = sql.compile().params.keys()
        return _dicts((await db.execute(sql, {k: params[k] for k in names})).all())

    tables: dict[str, list[dict[str, Any]]] = {
        "patients": await rows(PATIENTS_SQL),
        "appointments": await rows(APPOINTMENTS_SQL),
        "queue_tokens": await rows(QUEUE_SQL),
        "shifts": await rows(SHIFTS_SQL),
        "invoices": await rows(INVOICES_SQL),
        "encounters": await rows(ENCOUNTERS_SQL),
        "lab_orders": await rows(LAB_ORDERS_SQL),
        "lab_results": await rows(LAB_RESULTS_SQL),
        "referrals": await rows(REFERRALS_SQL),
        "care_team_assignments": await rows(CARE_TEAM_SQL),
        "break_glass_events": await rows(BREAK_GLASS_SQL),
        "schedules": await rows(SCHEDULES_SQL),
        "memberships": await rows(MEMBERSHIPS_SQL),
        "vitals": [],
        "clinical_notes": [],
        "prescriptions": [],
    }
    for row in await rows(PROGRESS_SQL):
        table = row.pop("tbl")
        row[PROGRESS_COLUMNS[table]] = row.pop("t")
        tables[table].append(row)
    tracked = ("appointments", "lab_orders", "referrals", "invoices")
    params["ids"] = [r["id"] for name in tracked for r in tables[name]]
    tables["status_events"] = await rows(STATUSES_SQL)

    assert set(tables) == set(COLUMNS)
    repo = MemoryRepository(frames_from_rows(tables))
    return {
        pid: repo.evidence_for_sync(EvidenceQuery(clinic_id, user_id, pid, at, timezone, role=role))
        for pid in patient_ids
    }
