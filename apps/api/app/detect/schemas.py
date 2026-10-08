import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field

from app.db.enums import AccessAction, AlertStatus, ResourceType, ReviewOutcome, Role


class TopFeature(BaseModel):
    feature: str
    value: float
    z: float


class AlertOut(BaseModel):
    id: uuid.UUID
    access_event_id: uuid.UUID
    day: date
    score: float
    rank_in_day: int
    status: AlertStatus
    model_version: str
    policy_version: str
    scorer: str
    created_at: datetime
    at: datetime
    user_id: uuid.UUID
    user_name: str
    role: Role
    resource: ResourceType
    action: AccessAction
    template_code: str | None
    strength: float
    top_features: list[TopFeature]


class TimelineEntry(BaseModel):
    at: datetime
    role: Role
    resource: ResourceType
    action: AccessAction
    decision: str
    template_code: str | None
    strength: float | None
    # A short pseudonym, so the timeline shows patterns without naming other patients.
    patient_ref: str
    is_this_alert: bool


class ReviewOut(BaseModel):
    reviewer_user_id: uuid.UUID
    outcome: ReviewOutcome
    note: str | None
    at: datetime


class AlertDetail(AlertOut):
    patient_id: uuid.UUID
    patient_name: str
    patient_mrn: str
    forgery_flags: dict[str, Any]
    evidence: dict[str, Any]
    features: dict[str, float]
    timeline: list[TimelineEntry]
    reviews: list[ReviewOut]


class ReviewIn(BaseModel):
    outcome: ReviewOutcome
    note: str | None = Field(default=None, max_length=1000)
