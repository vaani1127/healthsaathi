import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.db.enums import ConsentChannel, ConsentEventKind


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class NoticeIn(BaseModel):
    text_en: str = Field(min_length=20, max_length=10000)
    text_hi: str = Field(min_length=20, max_length=10000)
    purposes: list[str] = Field(min_length=1, max_length=10)


class NoticeOut(ORM):
    id: uuid.UUID
    version: int
    text_en: str
    text_hi: str
    purposes: list[str]
    published_at: datetime | None


class ConsentEventOut(ORM):
    id: uuid.UUID
    kind: ConsentEventKind
    at: datetime
    by_user_id: uuid.UUID | None


class ConsentOut(BaseModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    notice_id: uuid.UUID
    notice_version: int
    granted_at: datetime
    channel: ConsentChannel
    withdrawn_at: datetime | None
    events: list[ConsentEventOut]
