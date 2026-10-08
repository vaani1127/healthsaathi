import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.chart.schemas import PatientChart
from app.consent.schemas import ConsentOut
from app.db.enums import AccessAction, AccessDecision, QueryStatus, ResourceType, Role
from app.identity.schemas import EmailStr


class Because(BaseModel):
    """Why an access was expected, with the details the patient needs to recognise it."""

    template: str | None
    kind: str | None = None
    token_no: int | None = None
    slot_start: datetime | None = None
    tests: list[str] | None = None
    from_name: str | None = None
    reason_code: str | None = None


class AccessLogEntry(BaseModel):
    id: uuid.UUID
    at: datetime
    user_name: str
    role: Role
    resource: ResourceType
    action: AccessAction
    decision: AccessDecision
    break_glass: bool
    because: Because
    query_status: QueryStatus | None


class AccessLogPage(BaseModel):
    items: list[AccessLogEntry]
    next_cursor: str | None = None


class QueryIn(BaseModel):
    message: str = Field(min_length=3, max_length=500)


class QueryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    patient_id: uuid.UUID
    access_event_id: uuid.UUID
    message: str
    status: QueryStatus
    created_at: datetime


class QueryUpdate(BaseModel):
    status: QueryStatus


class PortalAccessIn(BaseModel):
    email: EmailStr


class PatientExport(BaseModel):
    exported_at: datetime
    record: PatientChart
    consents: list[ConsentOut]
    access_log: list[AccessLogEntry]
    note: str
