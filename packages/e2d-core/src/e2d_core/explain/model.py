"""Records the explanation engine works on.

These mirror the product tables (SPEC 3.1) but only carry the columns the engine needs, so the
same engine runs on the database and on simulator output.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time
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
class AccessReason:
    """A reason typed by a doctor when opening a chart without other evidence (T_REASON)."""

    code: str
    text: str


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
    reason: AccessReason | None = None
    break_glass_id: uuid.UUID | None = None


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
class EncounterEv:
    id: uuid.UUID
    patient_id: uuid.UUID
    doctor_user_id: uuid.UUID
    started_at: datetime
    ended_at: datetime | None
    created_by: uuid.UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class LabOrderEv:
    id: uuid.UUID
    patient_id: uuid.UUID
    ordered_by: uuid.UUID
    status: str
    created_at: datetime
    updated_at: datetime
    released_at: datetime | None


@dataclass(frozen=True, slots=True)
class ReferralEv:
    id: uuid.UUID
    patient_id: uuid.UUID
    from_user_id: uuid.UUID
    to_user_id: uuid.UUID
    valid_until: datetime
    status: str
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CareTeamEv:
    id: uuid.UUID
    patient_id: uuid.UUID
    user_id: uuid.UUID
    role: str
    starts_at: datetime
    ends_at: datetime | None
    status: str
    created_by: uuid.UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class BreakGlassEv:
    id: uuid.UUID
    user_id: uuid.UUID
    patient_id: uuid.UUID
    reason_code: str
    at: datetime


@dataclass(frozen=True, slots=True)
class StatusEv:
    """One status an appointment, lab order, referral or invoice took, and from when
    (status_events)."""

    entity_type: str  # appointment, lab_order, referral or invoice
    entity_id: uuid.UUID
    status: str
    at: datetime


def status_at(history: Iterable[StatusEv], at: datetime) -> str | None:
    """The status a record had at `at`: the last one it took at or before `at`, or None if it
    had none yet. `history` is one record's statuses in time order."""
    current = None
    for event in history:
        if event.at > at:
            break
        current = event.status
    return current


def status_order(event: StatusEv) -> tuple[str, uuid.UUID, datetime, str]:
    """The order repositories return a bundle's statuses in."""
    return (event.entity_type, event.entity_id, event.at, event.status)


@dataclass(frozen=True, slots=True)
class ProgressEv:
    """Something that moved a patient's care forward (for the no_progress flag)."""

    kind: str  # vitals, note, prescription, lab_result, invoice
    at: datetime


@dataclass(frozen=True, slots=True)
class OpenHours:
    weekday: int  # Monday = 0
    start: time
    end: time


@dataclass(frozen=True, slots=True)
class ClinicContext:
    """Clinic-level history used by forgery flags."""

    hours: tuple[OpenHours, ...] = ()


@dataclass(frozen=True, slots=True)
class UserContext:
    """The accessing user's recent evidence creation, against others with the same role."""

    created_7d: int = 0
    role_median_7d: float = 0.0


@dataclass(frozen=True, slots=True)
class EvidenceQuery:
    clinic_id: uuid.UUID
    user_id: uuid.UUID
    patient_id: uuid.UUID
    at: datetime
    timezone: str = "Asia/Kolkata"
    role: str | None = None
    as_of: datetime | None = None  # how far ahead to look for progress and status changes


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    """Workflow evidence around one (user, patient, time). Built by a repository backend."""

    patient: PatientEv | None = None
    appointments: tuple[AppointmentEv, ...] = ()
    queue_tokens: tuple[QueueTokenEv, ...] = ()
    shifts: tuple[ShiftEv, ...] = ()
    invoices: tuple[InvoiceEv, ...] = ()
    encounters: tuple[EncounterEv, ...] = ()
    lab_orders: tuple[LabOrderEv, ...] = ()
    referrals: tuple[ReferralEv, ...] = ()
    care_team: tuple[CareTeamEv, ...] = ()
    break_glass: tuple[BreakGlassEv, ...] = ()
    progress: tuple[ProgressEv, ...] = ()
    # Statuses of the bundle's appointments, lab orders, referrals and invoices up to as_of.
    statuses: tuple[StatusEv, ...] = ()
    clinic: ClinicContext = field(default_factory=ClinicContext)
    user: UserContext = field(default_factory=UserContext)

    def status_at(self, kind: str, record: Any, at: datetime) -> str | None:
        """The status `record` (of type `kind`) had at `at`, from the status history. A record
        with no history at all (written before histories were kept) keeps its stored status."""
        history = [s for s in self.statuses if s.entity_type == kind and s.entity_id == record.id]
        if not history:
            return str(record.status)
        return status_at(history, at)

    def status_changes(self, kind: str, record: Any) -> tuple[StatusEv, ...]:
        return tuple(s for s in self.statuses if s.entity_type == kind and s.entity_id == record.id)


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
    reason: AccessReason | None = None

    @property
    def explained(self) -> bool:
        return self.template_code is not None

    def evidence_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
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
        if self.reason is not None:
            data["reason"] = {"code": self.reason.code, "text": self.reason.text}
        return data
