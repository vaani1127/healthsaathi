import uuid
from datetime import date, datetime, time

from sqlalchemy import CheckConstraint, Index, SmallInteger, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (
    Base,
    ClinicScopedMixin,
    WorkflowMixin,
    clinic_fk,
    scoped_args,
    str_enum,
    user_fk,
)
from app.db.enums import (
    AppointmentKind,
    AppointmentSource,
    AppointmentStatus,
    EncounterStatus,
    QueueStatus,
    RecordStatus,
    ReferralStatus,
    Role,
    ShiftStatus,
    StatusEntity,
)


class Schedule(ClinicScopedMixin, WorkflowMixin, Base):
    __tablename__ = "schedules"
    __table_args__ = scoped_args(
        CheckConstraint("weekday BETWEEN 0 AND 6", name="weekday"),
        CheckConstraint("end_time > start_time", name="time_order"),
        CheckConstraint("slot_minutes BETWEEN 5 AND 120", name="slot_minutes"),
    )

    doctor_user_id: Mapped[uuid.UUID] = user_fk()
    weekday: Mapped[int] = mapped_column(SmallInteger)
    start_time: Mapped[time]
    end_time: Mapped[time]
    slot_minutes: Mapped[int]
    status: Mapped[RecordStatus] = mapped_column(
        str_enum(RecordStatus, "record_status"), server_default=RecordStatus.ACTIVE.value
    )


class Shift(ClinicScopedMixin, WorkflowMixin, Base):
    __tablename__ = "shifts"
    __table_args__ = scoped_args(
        CheckConstraint("ends_at > starts_at", name="time_order"),
        Index("ix_shifts_user_starts", "user_id", "starts_at"),
    )

    user_id: Mapped[uuid.UUID] = user_fk()
    starts_at: Mapped[datetime]
    ends_at: Mapped[datetime]
    role: Mapped[Role] = mapped_column(str_enum(Role, "role"))
    status: Mapped[ShiftStatus] = mapped_column(
        str_enum(ShiftStatus, "shift_status"), server_default=ShiftStatus.SCHEDULED.value
    )


class Appointment(ClinicScopedMixin, WorkflowMixin, Base):
    __tablename__ = "appointments"
    __table_args__ = scoped_args(
        clinic_fk(["patient_id"], "patients"),
        CheckConstraint("slot_end > slot_start", name="time_order"),
        Index("ix_appointments_patient", "clinic_id", "patient_id"),
        Index("ix_appointments_doctor_slot", "doctor_user_id", "slot_start"),
    )

    patient_id: Mapped[uuid.UUID]
    doctor_user_id: Mapped[uuid.UUID] = user_fk()
    slot_start: Mapped[datetime]
    slot_end: Mapped[datetime]
    kind: Mapped[AppointmentKind] = mapped_column(str_enum(AppointmentKind, "appointment_kind"))
    status: Mapped[AppointmentStatus] = mapped_column(
        str_enum(AppointmentStatus, "appointment_status"),
        server_default=AppointmentStatus.BOOKED.value,
    )
    source: Mapped[AppointmentSource] = mapped_column(
        str_enum(AppointmentSource, "appointment_source")
    )


class QueueToken(ClinicScopedMixin, WorkflowMixin, Base):
    __tablename__ = "queue_tokens"
    __table_args__ = scoped_args(
        clinic_fk(["appointment_id"], "appointments"),
        UniqueConstraint("clinic_id", "day", "token_no"),
        UniqueConstraint("clinic_id", "appointment_id"),
    )

    appointment_id: Mapped[uuid.UUID]
    token_no: Mapped[int]
    day: Mapped[date]
    status: Mapped[QueueStatus] = mapped_column(
        str_enum(QueueStatus, "queue_status"), server_default=QueueStatus.WAITING.value
    )
    assigned_nurse_user_id: Mapped[uuid.UUID | None] = user_fk(nullable=True)


class Encounter(ClinicScopedMixin, WorkflowMixin, Base):
    __tablename__ = "encounters"
    __table_args__ = scoped_args(
        clinic_fk(["appointment_id"], "appointments"),
        clinic_fk(["patient_id"], "patients"),
        Index("ix_encounters_patient", "clinic_id", "patient_id"),
    )

    appointment_id: Mapped[uuid.UUID | None]
    patient_id: Mapped[uuid.UUID]
    doctor_user_id: Mapped[uuid.UUID] = user_fk()
    started_at: Mapped[datetime]
    ended_at: Mapped[datetime | None]
    status: Mapped[EncounterStatus] = mapped_column(
        str_enum(EncounterStatus, "encounter_status"), server_default=EncounterStatus.OPEN.value
    )


class Referral(ClinicScopedMixin, WorkflowMixin, Base):
    __tablename__ = "referrals"
    __table_args__ = scoped_args(
        clinic_fk(["patient_id"], "patients"),
        Index("ix_referrals_to_user", "to_user_id", "patient_id"),
    )

    patient_id: Mapped[uuid.UUID]
    from_user_id: Mapped[uuid.UUID] = user_fk()
    to_user_id: Mapped[uuid.UUID] = user_fk()
    reason: Mapped[str] = mapped_column(Text)
    valid_until: Mapped[datetime]
    status: Mapped[ReferralStatus] = mapped_column(
        str_enum(ReferralStatus, "referral_status"), server_default=ReferralStatus.ACTIVE.value
    )


class CareTeamAssignment(ClinicScopedMixin, WorkflowMixin, Base):
    __tablename__ = "care_team_assignments"
    __table_args__ = scoped_args(
        clinic_fk(["patient_id"], "patients"),
        CheckConstraint("ends_at IS NULL OR ends_at > starts_at", name="time_order"),
        Index("ix_care_team_user_patient", "user_id", "patient_id"),
    )

    patient_id: Mapped[uuid.UUID]
    user_id: Mapped[uuid.UUID] = user_fk()
    role: Mapped[Role] = mapped_column(str_enum(Role, "role"))
    starts_at: Mapped[datetime]
    ends_at: Mapped[datetime | None]
    status: Mapped[RecordStatus] = mapped_column(
        str_enum(RecordStatus, "record_status"), server_default=RecordStatus.ACTIVE.value
    )


class StatusEvent(ClinicScopedMixin, Base):
    """Append-only status history: one row per status an appointment, lab order, referral or
    invoice takes, creation included (written by app.db.status_history). The explanation engine
    reads a record's status at the access time from here."""

    __tablename__ = "status_events"
    __table_args__ = scoped_args(
        Index("ix_status_events_entity", "clinic_id", "entity_id", "at"),
    )

    entity_type: Mapped[StatusEntity] = mapped_column(str_enum(StatusEntity, "status_entity"))
    entity_id: Mapped[uuid.UUID]
    status: Mapped[str] = mapped_column(String(32))
    at: Mapped[datetime] = mapped_column(server_default=func.now())
