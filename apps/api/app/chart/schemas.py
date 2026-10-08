"""Task views (SPEC 4): each role gets its own response schema, so fields outside a role's view
cannot be returned by mistake."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.clinic.schemas import AppointmentOut, PatientOut, PatientSummary
from app.db.enums import (
    AllergySeverity,
    ConditionStatus,
    InvoiceStatus,
    LabOrderStatus,
    ReviewOutcome,
    Role,
)


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class VitalOut(ORM):
    id: uuid.UUID
    recorded_at: datetime
    bp_sys: int | None
    bp_dia: int | None
    pulse: int | None
    temp_c: Decimal | None
    spo2: int | None
    rr: int | None
    weight_kg: Decimal | None
    height_cm: Decimal | None


class AllergyOut(ORM):
    id: uuid.UUID
    substance: str
    reaction: str | None
    severity: AllergySeverity
    is_active: bool


class ConditionOut(ORM):
    id: uuid.UUID
    code: str | None
    text: str
    onset: date | None
    status: ConditionStatus


class NoteOut(BaseModel):
    id: uuid.UUID
    version: int
    parent_id: uuid.UUID | None
    author_user_id: uuid.UUID
    created_at: datetime
    signed_at: datetime | None
    body: str


class PrescriptionOut(ORM):
    id: uuid.UUID
    version: int
    parent_id: uuid.UUID | None
    author_user_id: uuid.UUID
    items: list[dict[str, Any]]
    advice_en: str | None
    advice_hi: str | None
    created_at: datetime
    signed_at: datetime | None


class LabResultOut(ORM):
    id: uuid.UUID
    values: dict[str, Any]
    document_id: uuid.UUID | None
    resulted_at: datetime
    released_at: datetime | None


class LabOrderOut(BaseModel):
    id: uuid.UUID
    tests: list[dict[str, Any]]
    status: LabOrderStatus
    ordered_by: uuid.UUID
    created_at: datetime
    result: LabResultOut | None = None


class DocumentOut(ORM):
    id: uuid.UUID
    kind: str
    sha256: str
    created_at: datetime


class InvoiceSummary(ORM):
    id: uuid.UUID
    status: InvoiceStatus
    total_paise: int
    created_at: datetime


class ConsentStatus(BaseModel):
    notice_version: int
    granted_at: datetime
    withdrawn_at: datetime | None


class ReceptionChart(BaseModel):
    view: Literal["reception"] = "reception"
    patient: PatientOut
    appointments: list[AppointmentOut]
    invoices: list[InvoiceSummary]
    consent: ConsentStatus | None


class NurseChart(BaseModel):
    view: Literal["nurse"] = "nurse"
    patient: PatientSummary
    today_appointment: AppointmentOut | None
    vitals: list[VitalOut]
    allergies: list[AllergyOut]


class LabChart(BaseModel):
    view: Literal["lab"] = "lab"
    patient: PatientSummary
    worklist: list[LabOrderOut]


class DoctorChart(BaseModel):
    view: Literal["doctor"] = "doctor"
    patient: PatientOut
    appointments: list[AppointmentOut]
    vitals: list[VitalOut]
    allergies: list[AllergyOut]
    conditions: list[ConditionOut]
    notes: list[NoteOut]
    prescriptions: list[PrescriptionOut]
    lab_orders: list[LabOrderOut]
    documents: list[DocumentOut]
    explanation: str | None = None


class AdminChart(BaseModel):
    view: Literal["admin"] = "admin"
    patient: PatientOut


class PatientChart(BaseModel):
    """What a patient sees of their own record: signed notes and prescriptions, released results."""

    view: Literal["patient"] = "patient"
    patient: PatientOut
    appointments: list[AppointmentOut]
    vitals: list[VitalOut]
    allergies: list[AllergyOut]
    conditions: list[ConditionOut]
    notes: list[NoteOut]
    prescriptions: list[PrescriptionOut]
    lab_orders: list[LabOrderOut]
    documents: list[DocumentOut]
    invoices: list[InvoiceSummary]
    consent: ConsentStatus | None


class EmergencyChart(BaseModel):
    view: Literal["emergency"] = "emergency"
    break_glass_id: uuid.UUID
    expires_at: datetime
    patient: PatientSummary
    allergies: list[AllergyOut]
    conditions: list[ConditionOut]
    current_medicines: list[dict[str, Any]]
    last_vitals: VitalOut | None


Chart = Annotated[
    ReceptionChart | NurseChart | LabChart | DoctorChart | AdminChart | PatientChart,
    Field(discriminator="view"),
]


class AccessReasonIn(BaseModel):
    reason_code: Literal[
        "covering_doctor", "lab_review", "second_opinion", "administrative", "other"
    ]
    reason_text: str = Field(min_length=5, max_length=300)


class BreakGlassIn(BaseModel):
    reason_code: Literal["emergency", "unconscious", "severe_allergy", "system_outage", "other"]
    reason_text: str = Field(min_length=5, max_length=300)


class BreakGlassItem(BaseModel):
    id: uuid.UUID
    at: datetime
    user_id: uuid.UUID
    user_name: str
    user_role: Role | None
    patient: PatientSummary
    reason_code: str
    reason_text: str
    reviewed_at: datetime | None
    reviewed_by: uuid.UUID | None
    outcome: ReviewOutcome | None


class BreakGlassReviewIn(BaseModel):
    outcome: ReviewOutcome
    note: str | None = Field(default=None, max_length=500)
