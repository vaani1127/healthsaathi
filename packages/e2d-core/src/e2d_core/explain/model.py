"""Records the explanation engine works on.

These mirror the product tables (SPEC 3.1) but only carry the columns the engine needs, so the
same engine runs on the database and on simulator output.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

RESOURCE_TYPES = (
    "demographics",
    "vitals",
    "allergies",
    "notes",
    "prescriptions",
    "lab",
    "billing",
    "documents",
    "consent",
)


@dataclass(frozen=True, slots=True)
class AccessEvent:
    """a = (u, p, t, r, op, s) from SPEC 5.1, plus the role used."""

    id: uuid.UUID
    clinic_id: uuid.UUID
    user_id: uuid.UUID
    role: str
    patient_id: uuid.UUID
    resource: str
    action: str
    at: datetime
    session_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class PatientEv:
    id: uuid.UUID
    user_id: uuid.UUID | None
    created_by: uuid.UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AppointmentEv:
    id: uuid.UUID
    patient_id: uuid.UUID
    doctor_user_id: uuid.UUID
    slot_start: datetime
    slot_end: datetime
    kind: str
    status: str
    source: str
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class QueueTokenEv:
    id: uuid.UUID
    appointment_id: uuid.UUID
    patient_id: uuid.UUID
    token_no: int
    day: date
    status: str
    assigned_nurse_user_id: uuid.UUID | None
    created_by: uuid.UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ShiftEv:
    id: uuid.UUID
    user_id: uuid.UUID
    role: str
    starts_at: datetime
    ends_at: datetime
    status: str


@dataclass(frozen=True, slots=True)
class InvoiceEv:
    id: uuid.UUID
    patient_id: uuid.UUID
    status: str
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class EvidenceQuery:
    clinic_id: uuid.UUID
    user_id: uuid.UUID
    patient_id: uuid.UUID
    at: datetime
    timezone: str = "Asia/Kolkata"


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    """Workflow evidence around one (user, patient, time). Built by a repository backend."""

    patient: PatientEv | None = None
    appointments: tuple[AppointmentEv, ...] = ()
    queue_tokens: tuple[QueueTokenEv, ...] = ()
    shifts: tuple[ShiftEv, ...] = ()
    invoices: tuple[InvoiceEv, ...] = ()


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    kind: str
    id: uuid.UUID

    def to_json(self) -> dict[str, str]:
        return {"kind": self.kind, "id": str(self.id)}


@dataclass(frozen=True, slots=True)
class Candidate:
    template: str
    strength: float
    evidence: tuple[EvidenceRef, ...]
    anchor_time: datetime | None = None
    created_by: uuid.UUID | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Explanation:
    template_code: str | None
    strength: float
    evidence: tuple[EvidenceRef, ...] = ()
    alternatives: tuple[Candidate, ...] = ()
    forgery_flags: dict[str, Any] = field(default_factory=dict)

    @property
    def explained(self) -> bool:
        return self.template_code is not None

    def evidence_json(self) -> dict[str, Any]:
        return {
            "refs": [e.to_json() for e in self.evidence],
            "alternatives": [
                {
                    "template": c.template,
                    "strength": round(c.strength, 6),
                    "refs": [e.to_json() for e in c.evidence],
                }
                for c in self.alternatives
            ],
        }
