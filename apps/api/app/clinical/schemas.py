import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.chart.schemas import (
    AllergyOut,
    ConditionOut,
    LabOrderOut,
    LabResultOut,
    NoteOut,
    PrescriptionOut,
    VitalOut,
)
from app.db.enums import (
    AllergySeverity,
    ConditionStatus,
    EncounterStatus,
    LabOrderStatus,
    ReferralStatus,
)

__all__ = [
    "AllergyOut",
    "ConditionOut",
    "LabOrderOut",
    "LabResultOut",
    "NoteOut",
    "PrescriptionOut",
    "VitalOut",
]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class EncounterOut(ORM):
    id: uuid.UUID
    appointment_id: uuid.UUID | None
    patient_id: uuid.UUID
    doctor_user_id: uuid.UUID
    started_at: datetime
    ended_at: datetime | None
    status: EncounterStatus


class VitalsIn(BaseModel):
    encounter_id: uuid.UUID | None = None
    bp_sys: int | None = Field(default=None, ge=50, le=260)
    bp_dia: int | None = Field(default=None, ge=30, le=160)
    pulse: int | None = Field(default=None, ge=20, le=250)
    temp_c: Decimal | None = Field(default=None, ge=30, le=45, decimal_places=1)
    spo2: int | None = Field(default=None, ge=50, le=100)
    rr: int | None = Field(default=None, ge=4, le=60)
    weight_kg: Decimal | None = Field(default=None, gt=0, le=400, decimal_places=2)
    height_cm: Decimal | None = Field(default=None, gt=20, le=250, decimal_places=1)

    @model_validator(mode="after")
    def _something(self) -> "VitalsIn":
        values = self.model_dump(exclude={"encounter_id"})
        if all(v is None for v in values.values()):
            raise ValueError("record at least one measurement")
        if (self.bp_sys is None) != (self.bp_dia is None):
            raise ValueError("blood pressure needs both numbers")
        return self


class AllergyIn(BaseModel):
    substance: str = Field(min_length=2, max_length=120)
    reaction: str | None = Field(default=None, max_length=200)
    severity: AllergySeverity


class AllergyUpdate(BaseModel):
    is_active: bool


class ConditionIn(BaseModel):
    code: str | None = Field(default=None, max_length=32)
    text: str = Field(min_length=2, max_length=200)
    onset: date | None = None


class ConditionUpdate(BaseModel):
    status: ConditionStatus


class NoteIn(BaseModel):
    encounter_id: uuid.UUID
    body: str = Field(min_length=1, max_length=20000)


class NoteEdit(BaseModel):
    body: str = Field(min_length=1, max_length=20000)


class PrescriptionItem(BaseModel):
    drug: str = Field(min_length=2, max_length=120)
    strength: str | None = Field(default=None, max_length=40)
    dose: str = Field(min_length=1, max_length=40, description="For example 1-0-1")
    duration_days: int = Field(ge=1, le=365)
    instructions_en: str | None = Field(default=None, max_length=200)
    instructions_hi: str | None = Field(default=None, max_length=200)


class PrescriptionIn(BaseModel):
    encounter_id: uuid.UUID
    items: list[PrescriptionItem] = Field(min_length=1, max_length=30)
    advice_en: str | None = Field(default=None, max_length=1000)
    advice_hi: str | None = Field(default=None, max_length=1000)


class PrescriptionEdit(BaseModel):
    items: list[PrescriptionItem] = Field(min_length=1, max_length=30)
    advice_en: str | None = Field(default=None, max_length=1000)
    advice_hi: str | None = Field(default=None, max_length=1000)


class LabTest(BaseModel):
    code: str = Field(min_length=2, max_length=32)
    name: str = Field(min_length=2, max_length=120)


class LabOrderIn(BaseModel):
    encounter_id: uuid.UUID | None = None
    tests: list[LabTest] = Field(min_length=1, max_length=20)


class LabOrderUpdate(BaseModel):
    status: LabOrderStatus


class WorklistItem(BaseModel):
    order: LabOrderOut
    patient_id: uuid.UUID
    patient_name: str
    patient_mrn: str


class ReferralIn(BaseModel):
    to_user_id: uuid.UUID
    reason: str = Field(min_length=5, max_length=500)
    valid_days: int = Field(default=30, ge=1, le=180)


class ReferralUpdate(BaseModel):
    status: ReferralStatus


class ReferralOut(ORM):
    id: uuid.UUID
    patient_id: uuid.UUID
    from_user_id: uuid.UUID
    to_user_id: uuid.UUID
    reason: str
    valid_until: datetime
    status: ReferralStatus


class FollowUpIn(BaseModel):
    slot_start: datetime


class DocumentDownload(BaseModel):
    id: uuid.UUID
    kind: str
    sha256: str


def result_values(raw: str) -> dict[str, Any]:
    """Parse the JSON object of result values sent with a lab result upload."""
    try:
        values = json.loads(raw)
    except ValueError as exc:
        raise ValueError("values must be a JSON object") from exc
    if not isinstance(values, dict):
        raise ValueError("values must be a JSON object")
    return values
