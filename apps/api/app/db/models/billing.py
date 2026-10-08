import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, Index, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (
    Base,
    ClinicScopedMixin,
    CreatedAtMixin,
    WorkflowMixin,
    clinic_fk,
    scoped_args,
    str_enum,
    user_fk,
)
from app.db.enums import InvoiceStatus, PaymentMethod


class Service(ClinicScopedMixin, CreatedAtMixin, Base):
    __tablename__ = "services"
    __table_args__ = scoped_args(
        UniqueConstraint("clinic_id", "name"),
        CheckConstraint("price_paise >= 0", name="price"),
    )

    name: Mapped[str] = mapped_column(Text)
    price_paise: Mapped[int] = mapped_column(BigInteger)
    is_active: Mapped[bool] = mapped_column(server_default="true")


class Invoice(ClinicScopedMixin, WorkflowMixin, Base):
    __tablename__ = "invoices"
    __table_args__ = scoped_args(
        clinic_fk(["patient_id"], "patients"),
        clinic_fk(["appointment_id"], "appointments"),
        CheckConstraint("total_paise >= 0", name="total"),
        Index("ix_invoices_patient", "clinic_id", "patient_id"),
    )

    patient_id: Mapped[uuid.UUID]
    appointment_id: Mapped[uuid.UUID | None]
    status: Mapped[InvoiceStatus] = mapped_column(
        str_enum(InvoiceStatus, "invoice_status"), server_default=InvoiceStatus.DRAFT.value
    )
    total_paise: Mapped[int] = mapped_column(BigInteger, server_default="0")


class InvoiceItem(ClinicScopedMixin, Base):
    __tablename__ = "invoice_items"
    __table_args__ = scoped_args(
        clinic_fk(["invoice_id"], "invoices"),
        clinic_fk(["service_id"], "services"),
        CheckConstraint("qty > 0", name="qty"),
        CheckConstraint("price_paise >= 0", name="price"),
    )

    invoice_id: Mapped[uuid.UUID] = mapped_column(index=True)
    service_id: Mapped[uuid.UUID]
    qty: Mapped[int] = mapped_column(server_default="1")
    price_paise: Mapped[int] = mapped_column(BigInteger)


class Payment(ClinicScopedMixin, Base):
    __tablename__ = "payments"
    __table_args__ = scoped_args(
        clinic_fk(["invoice_id"], "invoices"),
        CheckConstraint("amount_paise > 0", name="amount"),
    )

    invoice_id: Mapped[uuid.UUID] = mapped_column(index=True)
    method: Mapped[PaymentMethod] = mapped_column(str_enum(PaymentMethod, "payment_method"))
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    received_by: Mapped[uuid.UUID] = user_fk()
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())
