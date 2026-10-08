import uuid
from datetime import date, datetime

from sqlalchemy import ForeignKey, Index, LargeBinary, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (
    Base,
    ClinicScopedMixin,
    CreatedAtMixin,
    IdMixin,
    scoped_args,
    str_enum,
    user_fk,
)
from app.db.enums import Role, Sex


class Clinic(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "clinics"

    name: Mapped[str] = mapped_column(Text)
    city: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text, server_default="Asia/Kolkata")
    signer_pubkey: Mapped[bytes | None] = mapped_column(LargeBinary)


class User(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "users"
    __table_args__ = (Index("uq_users_email_lower", text("lower(email)"), unique=True),)

    email: Mapped[str] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(server_default="true")


class Membership(ClinicScopedMixin, CreatedAtMixin, Base):
    __tablename__ = "memberships"
    __table_args__ = scoped_args(UniqueConstraint("clinic_id", "user_id", "role"))

    user_id: Mapped[uuid.UUID] = user_fk(index=True)
    role: Mapped[Role] = mapped_column(str_enum(Role, "role"))
    is_active: Mapped[bool] = mapped_column(server_default="true")


class StaffProfile(ClinicScopedMixin, Base):
    __tablename__ = "staff_profiles"
    __table_args__ = scoped_args(UniqueConstraint("clinic_id", "user_id"))

    user_id: Mapped[uuid.UUID] = user_fk()
    registration_no: Mapped[str | None] = mapped_column(Text)
    specialization: Mapped[str | None] = mapped_column(Text)
    department: Mapped[str | None] = mapped_column(Text)


class Patient(ClinicScopedMixin, CreatedAtMixin, Base):
    __tablename__ = "patients"
    __table_args__ = scoped_args(UniqueConstraint("clinic_id", "mrn"))

    user_id: Mapped[uuid.UUID | None] = user_fk(nullable=True, index=True)
    mrn: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(Text)
    dob: Mapped[date | None]
    sex: Mapped[Sex] = mapped_column(str_enum(Sex, "sex"))
    phone: Mapped[str | None] = mapped_column(Text)
    address: Mapped[str | None] = mapped_column(Text)
    abha_number: Mapped[str | None] = mapped_column(String(32))
    emergency_contact: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = user_fk()


class Device(IdMixin, Base):
    __tablename__ = "devices"
    __table_args__ = (UniqueConstraint("user_id", "fingerprint_hash"),)

    user_id: Mapped[uuid.UUID] = user_fk()
    fingerprint_hash: Mapped[str] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(Text)
    first_seen: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen: Mapped[datetime] = mapped_column(server_default=func.now())


class UserSession(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "sessions"

    user_id: Mapped[uuid.UUID] = user_fk(index=True)
    device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id"))
    refresh_hash: Mapped[str] = mapped_column(String(64), unique=True)
    family_id: Mapped[uuid.UUID] = mapped_column(index=True)
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]


class MfaSecret(Base):
    __tablename__ = "mfa_secrets"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    totp_secret_enc: Mapped[bytes] = mapped_column(LargeBinary)
    enabled_at: Mapped[datetime | None]


class EmailOtp(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "email_otps"
    __table_args__ = (Index("ix_email_otps_email_purpose", "email", "purpose"),)

    email: Mapped[str] = mapped_column(Text)
    code_hash: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(String(32))
    expires_at: Mapped[datetime]
    attempts: Mapped[int] = mapped_column(server_default="0")
    used_at: Mapped[datetime | None]
