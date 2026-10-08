import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Index,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (
    Base,
    ClinicScopedMixin,
    CreatedAtMixin,
    clinic_fk,
    scoped_args,
    str_enum,
    user_fk,
)
from app.db.enums import AllergySeverity, ConditionStatus, LabOrderStatus


class Vital(ClinicScopedMixin, CreatedAtMixin, Base):
    __tablename__ = "vitals"
    __table_args__ = scoped_args(
        clinic_fk(["patient_id"], "patients"),
        clinic_fk(["encounter_id"], "encounters"),
        Index("ix_vitals_patient_time", "clinic_id", "patient_id", "recorded_at"),
    )

    patient_id: Mapped[uuid.UUID]
    encounter_id: Mapped[uuid.UUID | None]
    recorded_by: Mapped[uuid.UUID] = user_fk()
    recorded_at: Mapped[datetime] = mapped_column(server_default=func.now())
    bp_sys: Mapped[int | None] = mapped_column(SmallInteger)
    bp_dia: Mapped[int | None] = mapped_column(SmallInteger)
    pulse: Mapped[int | None] = mapped_column(SmallInteger)
    temp_c: Mapped[Decimal | None] = mapped_column(Numeric(4, 1))
    spo2: Mapped[int | None] = mapped_column(SmallInteger)
    rr: Mapped[int | None] = mapped_column(SmallInteger)
    weight_kg: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    height_cm: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))


class Allergy(ClinicScopedMixin, CreatedAtMixin, Base):
    __tablename__ = "allergies"
    __table_args__ = scoped_args(clinic_fk(["patient_id"], "patients"))

    patient_id: Mapped[uuid.UUID] = mapped_column(index=True)
    substance: Mapped[str] = mapped_column(Text)
    reaction: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[AllergySeverity] = mapped_column(str_enum(AllergySeverity, "severity"))
    is_active: Mapped[bool] = mapped_column(server_default="true")
    recorded_by: Mapped[uuid.UUID] = user_fk()


class Condition(ClinicScopedMixin, CreatedAtMixin, Base):
    __tablename__ = "conditions"
    __table_args__ = scoped_args(clinic_fk(["patient_id"], "patients"))

    patient_id: Mapped[uuid.UUID] = mapped_column(index=True)
    code: Mapped[str | None] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text)
    onset: Mapped[date | None]
    status: Mapped[ConditionStatus] = mapped_column(
        str_enum(ConditionStatus, "condition_status"), server_default=ConditionStatus.ACTIVE.value
    )
    recorded_by: Mapped[uuid.UUID] = user_fk()


class ClinicalNote(ClinicScopedMixin, CreatedAtMixin, Base):
    """Versioned note. A signed version is never changed; an edit creates a new version."""

    __tablename__ = "clinical_notes"
    __table_args__ = scoped_args(
        clinic_fk(["patient_id"], "patients"),
        clinic_fk(["encounter_id"], "encounters"),
        clinic_fk(["parent_id"], "clinical_notes"),
        CheckConstraint("version >= 1", name="version"),
        Index("ix_clinical_notes_patient", "clinic_id", "patient_id"),
    )

    patient_id: Mapped[uuid.UUID]
    encounter_id: Mapped[uuid.UUID]
    author_user_id: Mapped[uuid.UUID] = user_fk()
    version: Mapped[int] = mapped_column(server_default="1")
    parent_id: Mapped[uuid.UUID | None]
    body_enc: Mapped[bytes] = mapped_column(LargeBinary)
    signed_at: Mapped[datetime | None]


class Prescription(ClinicScopedMixin, CreatedAtMixin, Base):
    __tablename__ = "prescriptions"
    __table_args__ = scoped_args(
        clinic_fk(["patient_id"], "patients"),
        clinic_fk(["encounter_id"], "encounters"),
        clinic_fk(["parent_id"], "prescriptions"),
        CheckConstraint("version >= 1", name="version"),
        Index("ix_prescriptions_patient", "clinic_id", "patient_id"),
    )

    patient_id: Mapped[uuid.UUID]
    encounter_id: Mapped[uuid.UUID]
    author_user_id: Mapped[uuid.UUID] = user_fk()
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    advice_en: Mapped[str | None] = mapped_column(Text)
    advice_hi: Mapped[str | None] = mapped_column(Text)
    signed_at: Mapped[datetime | None]
    version: Mapped[int] = mapped_column(server_default="1")
    parent_id: Mapped[uuid.UUID | None]


class LabOrder(ClinicScopedMixin, CreatedAtMixin, Base):
    __tablename__ = "lab_orders"
    __table_args__ = scoped_args(
        clinic_fk(["patient_id"], "patients"),
        clinic_fk(["encounter_id"], "encounters"),
        Index("ix_lab_orders_patient", "clinic_id", "patient_id"),
        Index("ix_lab_orders_status", "clinic_id", "status"),
    )

    patient_id: Mapped[uuid.UUID]
    encounter_id: Mapped[uuid.UUID | None]
    ordered_by: Mapped[uuid.UUID] = user_fk()
    tests: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    status: Mapped[LabOrderStatus] = mapped_column(
        str_enum(LabOrderStatus, "lab_order_status"), server_default=LabOrderStatus.ORDERED.value
    )
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class Document(ClinicScopedMixin, CreatedAtMixin, Base):
    __tablename__ = "documents"
    __table_args__ = scoped_args(clinic_fk(["patient_id"], "patients"))

    patient_id: Mapped[uuid.UUID] = mapped_column(index=True)
    kind: Mapped[str] = mapped_column(String(32))
    blob_path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    uploaded_by: Mapped[uuid.UUID] = user_fk()


class LabResult(ClinicScopedMixin, Base):
    __tablename__ = "lab_results"
    __table_args__ = scoped_args(
        clinic_fk(["lab_order_id"], "lab_orders"),
        clinic_fk(["patient_id"], "patients"),
        clinic_fk(["document_id"], "documents"),
        UniqueConstraint("clinic_id", "lab_order_id"),
    )

    lab_order_id: Mapped[uuid.UUID]
    patient_id: Mapped[uuid.UUID] = mapped_column(index=True)
    values: Mapped[dict[str, Any]] = mapped_column(JSONB)
    document_id: Mapped[uuid.UUID | None]
    resulted_by: Mapped[uuid.UUID] = user_fk()
    resulted_at: Mapped[datetime] = mapped_column(server_default=func.now())
    released_at: Mapped[datetime | None]
