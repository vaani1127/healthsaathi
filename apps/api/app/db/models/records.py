"""Record versions (ADR 0011): one row per committed version of a clinical record.

The row keeps the random salt and the commitment SHA-256(salt || JCS(content)); the audit log keeps
a `record_version` leaf with the same commitment. Append-only for every role: a trigger blocks
UPDATE, DELETE and TRUNCATE, and the app role has SELECT and INSERT only (migration 0008).
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, ClinicScopedMixin, clinic_fk, scoped_args, str_enum, user_fk
from app.db.enums import RecordType


class RecordVersion(ClinicScopedMixin, Base):
    __tablename__ = "record_versions"
    __table_args__ = scoped_args(
        clinic_fk(["patient_id"], "patients"),
        clinic_fk(["prev_version_id"], "record_versions"),
        UniqueConstraint("clinic_id", "record_type", "record_id", "version"),
        CheckConstraint("version >= 1", name="version"),
        CheckConstraint("octet_length(salt) = 32", name="salt_len"),
        CheckConstraint("octet_length(commitment) = 32", name="commitment_len"),
        Index("ix_record_versions_record", "clinic_id", "record_id"),
    )

    patient_id: Mapped[uuid.UUID]
    record_type: Mapped[RecordType] = mapped_column(str_enum(RecordType, "record_type"))
    record_id: Mapped[uuid.UUID]
    # 1, 2, ... for one record_id. A signed note or prescription row never changes, so it has only
    # version 1; its earlier note versions are linked through prev_version_id.
    version: Mapped[int]
    prev_version_id: Mapped[uuid.UUID | None]
    content_schema: Mapped[str] = mapped_column(String(32))
    salt: Mapped[bytes] = mapped_column(LargeBinary)
    commitment: Mapped[bytes] = mapped_column(LargeBinary)
    audit_seq: Mapped[int] = mapped_column(BigInteger)
    backfilled: Mapped[bool] = mapped_column(server_default="false")
    created_by: Mapped[uuid.UUID] = user_fk()
    created_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
