import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, ClinicScopedMixin, clinic_fk, scoped_args, str_enum, user_fk
from app.db.enums import ConsentChannel, ConsentEventKind


class ConsentNotice(ClinicScopedMixin, Base):
    __tablename__ = "consent_notices"
    __table_args__ = scoped_args(
        UniqueConstraint("clinic_id", "version"),
        CheckConstraint("version >= 1", name="version"),
    )

    version: Mapped[int]
    text_en: Mapped[str] = mapped_column(Text)
    text_hi: Mapped[str] = mapped_column(Text)
    purposes: Mapped[list[str]] = mapped_column(JSONB)
    published_at: Mapped[datetime | None]


class Consent(ClinicScopedMixin, Base):
    __tablename__ = "consents"
    __table_args__ = scoped_args(
        clinic_fk(["patient_id"], "patients"),
        clinic_fk(["notice_id"], "consent_notices"),
    )

    patient_id: Mapped[uuid.UUID] = mapped_column(index=True)
    notice_id: Mapped[uuid.UUID]
    granted_at: Mapped[datetime] = mapped_column(server_default=func.now())
    channel: Mapped[ConsentChannel] = mapped_column(str_enum(ConsentChannel, "consent_channel"))
    withdrawn_at: Mapped[datetime | None]
    salt_hash: Mapped[str] = mapped_column(String(64))


class ConsentEvent(ClinicScopedMixin, Base):
    __tablename__ = "consent_events"
    __table_args__ = scoped_args(clinic_fk(["consent_id"], "consents"))

    consent_id: Mapped[uuid.UUID] = mapped_column(index=True)
    kind: Mapped[ConsentEventKind] = mapped_column(str_enum(ConsentEventKind, "consent_event_kind"))
    at: Mapped[datetime] = mapped_column(server_default=func.now())
    by_user_id: Mapped[uuid.UUID | None] = user_fk(nullable=True)
