"""Access, audit and ledger tables.

access_events and audit_events are partitioned by month on `at`, so their primary keys include
`at`. Postgres can only reference a partitioned table through its full key, so tables that point
at an access event (explanations, alerts, patient queries) store the id without a foreign key;
the access service writes them in the same transaction as the event.
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    REAL,
    BigInteger,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    LargeBinary,
    Sequence,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (
    Base,
    ClinicScopedMixin,
    CreatedAtMixin,
    IdMixin,
    clinic_fk,
    scoped_args,
    str_enum,
    user_fk,
)
from app.db.enums import (
    AccessAction,
    AccessDecision,
    AlertStatus,
    AnchorBackend,
    AnchorStatus,
    QueryStatus,
    ResourceType,
    ReviewOutcome,
    Role,
)
from e2d_core.ids import uuid7

PARTITIONED_TABLES = ("access_events", "audit_events")


class BreakGlassEvent(ClinicScopedMixin, Base):
    __tablename__ = "break_glass_events"
    __table_args__ = scoped_args(
        clinic_fk(["patient_id"], "patients"),
        Index("ix_break_glass_review", "clinic_id", "reviewed_at"),
    )

    user_id: Mapped[uuid.UUID] = user_fk()
    patient_id: Mapped[uuid.UUID]
    reason_code: Mapped[str] = mapped_column(String(32))
    reason_text: Mapped[str] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(server_default=func.now())
    reviewed_by: Mapped[uuid.UUID | None] = user_fk(nullable=True)
    reviewed_at: Mapped[datetime | None]
    outcome: Mapped[ReviewOutcome | None] = mapped_column(str_enum(ReviewOutcome, "outcome"))


class AccessEvent(Base):
    __tablename__ = "access_events"
    __table_args__ = (
        clinic_fk(["patient_id"], "patients"),
        clinic_fk(["break_glass_id"], "break_glass_events"),
        Index("ix_access_events_clinic_at", "clinic_id", "at"),
        Index("ix_access_events_patient_at", "clinic_id", "patient_id", "at"),
        Index("ix_access_events_user_at", "user_id", "at"),
        {"postgresql_partition_by": "RANGE (at)"},
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    at: Mapped[datetime] = mapped_column(primary_key=True, server_default=func.now())
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"))
    user_id: Mapped[uuid.UUID] = user_fk()
    role: Mapped[Role] = mapped_column(str_enum(Role, "role"))
    patient_id: Mapped[uuid.UUID]
    resource_type: Mapped[ResourceType] = mapped_column(str_enum(ResourceType, "resource_type"))
    action: Mapped[AccessAction] = mapped_column(str_enum(AccessAction, "action"))
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sessions.id"))
    device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id"))
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(64))
    policy_version: Mapped[str] = mapped_column(String(64))
    decision: Mapped[AccessDecision] = mapped_column(str_enum(AccessDecision, "decision"))
    break_glass_id: Mapped[uuid.UUID | None]


class AccessExplanation(Base):
    __tablename__ = "access_explanations"
    __table_args__ = (Index("ix_access_explanations_clinic", "clinic_id", "computed_at"),)

    access_event_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"))
    template_code: Mapped[str | None] = mapped_column(String(32))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB)
    strength: Mapped[float] = mapped_column(REAL)
    forgery_flags: Mapped[dict[str, Any]] = mapped_column(JSONB)
    computed_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Alert(ClinicScopedMixin, CreatedAtMixin, Base):
    __tablename__ = "alerts"
    __table_args__ = scoped_args(
        UniqueConstraint("clinic_id", "access_event_id"),
        Index("ix_alerts_day_rank", "clinic_id", "day", "rank_in_day"),
    )

    access_event_id: Mapped[uuid.UUID]
    day: Mapped[date]
    score: Mapped[float] = mapped_column(Float)
    rank_in_day: Mapped[int]
    features: Mapped[dict[str, Any]] = mapped_column(JSONB)
    model_version: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[str] = mapped_column(String(64))
    status: Mapped[AlertStatus] = mapped_column(
        str_enum(AlertStatus, "alert_status"), server_default=AlertStatus.OPEN.value
    )


class AlertReview(ClinicScopedMixin, Base):
    __tablename__ = "alert_reviews"
    __table_args__ = scoped_args(clinic_fk(["alert_id"], "alerts"))

    alert_id: Mapped[uuid.UUID] = mapped_column(index=True)
    reviewer_user_id: Mapped[uuid.UUID] = user_fk()
    outcome: Mapped[ReviewOutcome] = mapped_column(str_enum(ReviewOutcome, "outcome"))
    note: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(server_default=func.now())


class PatientAccessQuery(ClinicScopedMixin, CreatedAtMixin, Base):
    __tablename__ = "patient_access_queries"
    __table_args__ = scoped_args(clinic_fk(["patient_id"], "patients"))

    patient_id: Mapped[uuid.UUID] = mapped_column(index=True)
    access_event_id: Mapped[uuid.UUID]
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[QueryStatus] = mapped_column(
        str_enum(QueryStatus, "query_status"), server_default=QueryStatus.OPEN.value
    )


AUDIT_SEQ = Sequence("audit_events_seq_seq", metadata=Base.metadata)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        UniqueConstraint("id", "at"),
        Index("ix_audit_events_clinic_seq", "clinic_id", "seq"),
        Index(
            "ix_audit_events_access_event",
            text("(payload ->> 'access_event_id')"),
            postgresql_where=text("kind = 'access'"),
        ),
        CheckConstraint("octet_length(payload_hash) = 32", name="payload_hash_len"),
        CheckConstraint("octet_length(chain_hash) = 32", name="chain_hash_len"),
        {"postgresql_partition_by": "RANGE (at)"},
    )

    seq: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, server_default=AUDIT_SEQ.next_value()
    )
    at: Mapped[datetime] = mapped_column(primary_key=True, server_default=func.now())
    id: Mapped[uuid.UUID] = mapped_column(default=uuid7)
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"))
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    payload_hash: Mapped[bytes] = mapped_column(LargeBinary)
    prev_hash: Mapped[bytes] = mapped_column(LargeBinary)
    chain_hash: Mapped[bytes] = mapped_column(LargeBinary)


class MerkleCheckpoint(ClinicScopedMixin, Base):
    __tablename__ = "merkle_checkpoints"
    __table_args__ = scoped_args(
        UniqueConstraint("clinic_id", "tree_size"),
        CheckConstraint("tree_size >= 0", name="tree_size"),
    )

    tree_size: Mapped[int] = mapped_column(BigInteger)
    root: Mapped[bytes] = mapped_column(LargeBinary)
    prev_root: Mapped[bytes | None] = mapped_column(LargeBinary)
    signed_at: Mapped[datetime] = mapped_column(server_default=func.now())
    signature: Mapped[bytes] = mapped_column(LargeBinary)
    key_id: Mapped[str] = mapped_column(String(16))


class AnchorReceipt(ClinicScopedMixin, Base):
    __tablename__ = "anchor_receipts"
    __table_args__ = scoped_args(
        clinic_fk(["checkpoint_id"], "merkle_checkpoints"),
        UniqueConstraint("checkpoint_id", "backend"),
    )

    checkpoint_id: Mapped[uuid.UUID]
    backend: Mapped[AnchorBackend] = mapped_column(str_enum(AnchorBackend, "anchor_backend"))
    status: Mapped[AnchorStatus] = mapped_column(
        str_enum(AnchorStatus, "anchor_status"), server_default=AnchorStatus.PENDING.value
    )
    tx_ref: Mapped[str | None] = mapped_column(Text)
    block_ref: Mapped[str | None] = mapped_column(Text)
    proof: Mapped[bytes | None] = mapped_column(LargeBinary)
    submitted_at: Mapped[datetime] = mapped_column(server_default=func.now())
    confirmed_at: Mapped[datetime | None]


class PolicyVersion(IdMixin, Base):
    __tablename__ = "policy_versions"

    version: Mapped[str] = mapped_column(String(32))
    yaml: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    activated_at: Mapped[datetime] = mapped_column(server_default=func.now())
