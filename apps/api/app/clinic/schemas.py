import uuid
from datetime import date, datetime, time

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db.enums import (
    AppointmentKind,
    AppointmentSource,
    AppointmentStatus,
    QueueStatus,
    RecordStatus,
    Role,
    Sex,
    ShiftStatus,
)


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class PatientCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    dob: date | None = None
    sex: Sex
    phone: str | None = Field(default=None, max_length=20)
    address: str | None = Field(default=None, max_length=300)
    abha_number: str | None = Field(default=None, max_length=32)
    emergency_contact: str | None = Field(default=None, max_length=120)


class PatientUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    dob: date | None = None
    sex: Sex | None = None
    phone: str | None = Field(default=None, max_length=20)
    address: str | None = Field(default=None, max_length=300)
    abha_number: str | None = Field(default=None, max_length=32)
    emergency_contact: str | None = Field(default=None, max_length=120)


class PatientOut(ORM):
    id: uuid.UUID
    mrn: str
    name: str
    dob: date | None
    sex: Sex
    phone: str | None
    address: str | None
    abha_number: str | None
    emergency_contact: str | None
    created_at: datetime


class PatientSummary(ORM):
    id: uuid.UUID
    mrn: str
    name: str
    dob: date | None
    sex: Sex


class StaffMember(BaseModel):
    user_id: uuid.UUID
    name: str
    role: Role


class ScheduleCreate(BaseModel):
    doctor_user_id: uuid.UUID
    weekday: int = Field(ge=0, le=6)
    start_time: time
    end_time: time
    slot_minutes: int = Field(default=15, ge=5, le=120)

    @model_validator(mode="after")
    def _order(self) -> "ScheduleCreate":
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class ScheduleUpdate(BaseModel):
    status: RecordStatus


class ScheduleOut(ORM):
    id: uuid.UUID
    doctor_user_id: uuid.UUID
    weekday: int
    start_time: time
    end_time: time
    slot_minutes: int
    status: RecordStatus


class ShiftCreate(BaseModel):
    user_id: uuid.UUID
    role: Role
    starts_at: datetime
    ends_at: datetime

    @model_validator(mode="after")
    def _order(self) -> "ShiftCreate":
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self


class ShiftOut(ORM):
    id: uuid.UUID
    user_id: uuid.UUID
    role: Role
    starts_at: datetime
    ends_at: datetime
    status: ShiftStatus


class AppointmentCreate(BaseModel):
    patient_id: uuid.UUID
    doctor_user_id: uuid.UUID
    slot_start: datetime
    slot_end: datetime | None = None
    kind: AppointmentKind = AppointmentKind.NEW


class AppointmentUpdate(BaseModel):
    status: AppointmentStatus


class AppointmentOut(ORM):
    id: uuid.UUID
    patient_id: uuid.UUID
    doctor_user_id: uuid.UUID
    slot_start: datetime
    slot_end: datetime
    kind: AppointmentKind
    status: AppointmentStatus
    source: AppointmentSource
    created_at: datetime


class AppointmentListItem(AppointmentOut):
    patient: PatientSummary


class WalkInCreate(BaseModel):
    patient_id: uuid.UUID
    doctor_user_id: uuid.UUID


class TokenCreate(BaseModel):
    appointment_id: uuid.UUID


class TokenUpdate(BaseModel):
    status: QueueStatus | None = None
    assigned_nurse_user_id: uuid.UUID | None = None
    unassign_nurse: bool = False


class TokenOut(ORM):
    id: uuid.UUID
    appointment_id: uuid.UUID
    token_no: int
    day: date
    status: QueueStatus
    assigned_nurse_user_id: uuid.UUID | None


class QueueItem(TokenOut):
    doctor_user_id: uuid.UUID
    patient: PatientSummary


class WalkInOut(BaseModel):
    appointment: AppointmentOut
    token: TokenOut


class CareTeamCreate(BaseModel):
    patient_id: uuid.UUID
    user_id: uuid.UUID
    role: Role
    starts_at: datetime | None = None
    ends_at: datetime | None = None


class CareTeamOut(ORM):
    id: uuid.UUID
    patient_id: uuid.UUID
    user_id: uuid.UUID
    role: Role
    starts_at: datetime
    ends_at: datetime | None
    status: RecordStatus
