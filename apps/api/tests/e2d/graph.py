"""Small hand-built workflow graphs, written to Postgres and to polars frames alike."""

import uuid
from datetime import date, datetime, time, timedelta
from typing import Any

import polars as pl
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db import TenantContext, tenant_session
from app.db import models as m
from e2d_core.explain import EvidenceBundle, EvidenceQuery
from e2d_core.repo.memory import COLUMNS, MemoryRepository, frames_from_rows
from e2d_core.repo.sql import SqlRepository

MODELS: dict[str, Any] = {
    "patients": m.Patient,
    "appointments": m.Appointment,
    "queue_tokens": m.QueueToken,
    "shifts": m.Shift,
    "invoices": m.Invoice,
    "encounters": m.Encounter,
    "lab_orders": m.LabOrder,
    "lab_results": m.LabResult,
    "referrals": m.Referral,
    "care_team_assignments": m.CareTeamAssignment,
    "break_glass_events": m.BreakGlassEvent,
    "vitals": m.Vital,
    "clinical_notes": m.ClinicalNote,
    "prescriptions": m.Prescription,
    "schedules": m.Schedule,
    "memberships": m.Membership,
    "status_events": m.StatusEvent,
}
INSERT_ORDER = list(MODELS)


class Graph:
    """One clinic with a patient and staff. Methods add rows with sensible defaults."""

    def __init__(self, at: datetime) -> None:
        self.at = at
        self.clinic = uuid.uuid4()
        self.users: dict[str, uuid.UUID] = {}
        self.rows: dict[str, list[dict[str, Any]]] = {t: [] for t in COLUMNS}
        for name, role in (
            ("doctor", "doctor"),
            ("doctor2", "doctor"),
            ("doctor3", "doctor"),
            ("doctor4", "doctor"),
            ("nurse", "nurse"),
            ("reception", "reception"),
            ("lab", "lab_tech"),
            ("admin", "clinic_admin"),
            ("patient_user", "patient"),
        ):
            self.users[name] = uuid.uuid4()
            self.rows["memberships"].append(
                {
                    "id": uuid.uuid4(),
                    "clinic_id": self.clinic,
                    "user_id": self.users[name],
                    "role": role,
                    "is_active": True,
                }
            )
        self.patient = self.add_patient(self.users["patient_user"])

    def _add(self, table: str, **row: Any) -> uuid.UUID:
        row.setdefault("id", uuid.uuid4())
        row.setdefault("clinic_id", self.clinic)
        self.rows[table].append(row)
        return row["id"]  # type: ignore[no-any-return]

    def add_status(self, kind: str, entity: uuid.UUID, status: str, at: datetime) -> None:
        self._add("status_events", entity_type=kind, entity_id=entity, status=status, at=at)

    def _history(
        self,
        kind: str,
        entity: uuid.UUID,
        first: str,
        created: datetime,
        status: str,
        changed: datetime,
        history: bool,
    ) -> None:
        """The usual history: the first status at creation, then the stored one at its last
        update. `history=False` writes none, as for rows written before histories were kept."""
        if not history:
            return
        self.add_status(kind, entity, first, created)
        if status != first:
            self.add_status(kind, entity, status, changed)

    def add_patient(
        self, user_id: uuid.UUID | None = None, created_at: datetime | None = None
    ) -> uuid.UUID:
        pid = uuid.uuid4()
        return self._add(
            "patients",
            id=pid,
            user_id=user_id,
            created_by=self.users["reception"],
            created_at=created_at or self.at - timedelta(days=30),
            mrn=pid.hex[:10],
            name="Graph Patient",
            sex="female",
        )

    def add_appointment(
        self,
        start: datetime,
        doctor: str = "doctor",
        status: str = "booked",
        source: str = "reception",
        created_by: str = "reception",
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
        patient: uuid.UUID | None = None,
        history: bool = True,
    ) -> uuid.UUID:
        created = created_at or start - timedelta(days=2)
        appt = self._add(
            "appointments",
            patient_id=patient or self.patient,
            doctor_user_id=self.users[doctor],
            slot_start=start,
            slot_end=start + timedelta(minutes=15),
            kind="new",
            status=status,
            source=source,
            created_by=self.users[created_by],
            created_at=created,
            updated_at=updated_at or created,
        )
        self._history(
            "appointment", appt, "booked", created, status, updated_at or created, history
        )
        return appt

    def add_token(
        self, appointment: uuid.UUID, day: date, nurse: str | None = "nurse"
    ) -> uuid.UUID:
        return self._add(
            "queue_tokens",
            appointment_id=appointment,
            token_no=len(self.rows["queue_tokens"]) + 1,
            day=day,
            status="waiting",
            assigned_nurse_user_id=self.users[nurse] if nurse else None,
            created_by=self.users["reception"],
            created_at=self.at - timedelta(hours=2),
            updated_at=self.at - timedelta(hours=2),
        )

    def add_shift(
        self, user: str, start: datetime, end: datetime, role: str = "nurse"
    ) -> uuid.UUID:
        return self._add(
            "shifts",
            user_id=self.users[user],
            role=role,
            starts_at=start,
            ends_at=end,
            status="scheduled",
            created_by=self.users["admin"],
            created_at=start - timedelta(days=1),
            updated_at=start - timedelta(days=1),
        )

    def add_invoice(
        self, created_at: datetime, status: str = "issued", updated_at: datetime | None = None
    ) -> uuid.UUID:
        invoice = self._add(
            "invoices",
            patient_id=self.patient,
            status=status,
            total_paise=1000,
            created_by=self.users["reception"],
            created_at=created_at,
            updated_at=updated_at or created_at,
        )
        self._history(
            "invoice", invoice, "issued", created_at, status, updated_at or created_at, True
        )
        return invoice

    def add_encounter(self, started_at: datetime, doctor: str = "doctor") -> uuid.UUID:
        return self._add(
            "encounters",
            appointment_id=None,
            patient_id=self.patient,
            doctor_user_id=self.users[doctor],
            started_at=started_at,
            ended_at=started_at + timedelta(minutes=15),
            status="closed",
            created_by=self.users[doctor],
            created_at=started_at,
            updated_at=started_at,
        )

    def add_lab_order(
        self,
        created_at: datetime,
        status: str = "ordered",
        released_at: datetime | None = None,
        ordered_by: str = "doctor",
        updated_at: datetime | None = None,
    ) -> uuid.UUID:
        order = self._add(
            "lab_orders",
            patient_id=self.patient,
            encounter_id=None,
            ordered_by=self.users[ordered_by],
            tests=[],
            status=status,
            created_at=created_at,
            updated_at=updated_at or released_at or created_at,
        )
        self._history(
            "lab_order",
            order,
            "ordered",
            created_at,
            status,
            updated_at or released_at or created_at,
            True,
        )
        if released_at is not None:
            self._add(
                "lab_results",
                lab_order_id=order,
                patient_id=self.patient,
                values={},
                resulted_by=self.users["lab"],
                resulted_at=released_at,
                released_at=released_at,
            )
        return order

    def add_referral(
        self,
        created_at: datetime,
        valid_until: datetime,
        to: str = "doctor2",
        status: str = "active",
        updated_at: datetime | None = None,
    ) -> uuid.UUID:
        referral = self._add(
            "referrals",
            patient_id=self.patient,
            from_user_id=self.users["doctor"],
            to_user_id=self.users[to],
            reason="second opinion",
            valid_until=valid_until,
            status=status,
            created_by=self.users["doctor"],
            created_at=created_at,
            updated_at=updated_at or created_at,
        )
        self._history(
            "referral", referral, "active", created_at, status, updated_at or created_at, True
        )
        return referral

    def add_care_team(
        self, user: str, role: str, starts_at: datetime, ends_at: datetime | None = None
    ) -> uuid.UUID:
        return self._add(
            "care_team_assignments",
            patient_id=self.patient,
            user_id=self.users[user],
            role=role,
            starts_at=starts_at,
            ends_at=ends_at,
            status="active",
            created_by=self.users["admin"],
            created_at=starts_at,
            updated_at=starts_at,
        )

    def add_break_glass(self, user: str, at: datetime) -> uuid.UUID:
        return self._add(
            "break_glass_events",
            user_id=self.users[user],
            patient_id=self.patient,
            reason_code="emergency",
            reason_text="collapsed in waiting area",
            at=at,
        )

    def add_vitals(self, at: datetime) -> uuid.UUID:
        return self._add(
            "vitals",
            patient_id=self.patient,
            recorded_by=self.users["nurse"],
            recorded_at=at,
            created_at=at,
        )

    def add_note(self, at: datetime) -> uuid.UUID:
        encounter = self.add_encounter(at)
        return self._add(
            "clinical_notes",
            patient_id=self.patient,
            encounter_id=encounter,
            author_user_id=self.users["doctor"],
            body_enc=b"x",
            created_at=at,
        )

    def add_schedule(
        self, weekday: int, start: time, end: time, doctor: str = "doctor"
    ) -> uuid.UUID:
        return self._add(
            "schedules",
            doctor_user_id=self.users[doctor],
            weekday=weekday,
            start_time=start,
            end_time=end,
            slot_minutes=15,
            status="active",
            created_by=self.users["admin"],
            created_at=self.at - timedelta(days=90),
            updated_at=self.at - timedelta(days=90),
        )

    def frames(self) -> dict[str, pl.DataFrame]:
        return frames_from_rows(self.rows)

    async def write(self, engine: AsyncEngine) -> None:
        async with AsyncSession(engine) as s, s.begin():
            await s.execute(
                insert(m.Clinic).values(id=self.clinic, name="Graph clinic", city="X", state="Y")
            )
            for name, uid in self.users.items():
                await s.execute(
                    insert(m.User).values(id=uid, email=f"{name}.{uid.hex}@graph.test", name=name)
                )
            for table in INSERT_ORDER:
                for row in self.rows[table]:
                    await s.execute(insert(MODELS[table]).values(**row))

    def query(
        self, user: str, role: str | None, at: datetime | None = None, as_of: datetime | None = None
    ) -> EvidenceQuery:
        return EvidenceQuery(
            self.clinic, self.users[user], self.patient, at or self.at, role=role, as_of=as_of
        )


async def fetch_sql(query: EvidenceQuery) -> EvidenceBundle:
    async with tenant_session(TenantContext(clinic_id=query.clinic_id, role="system")) as db:
        return await SqlRepository(db).evidence_for(query)


async def fetch_memory(graph: Graph, query: EvidenceQuery) -> EvidenceBundle:
    return await MemoryRepository(graph.frames()).evidence_for(query)
