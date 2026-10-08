import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.db.enums import InvoiceStatus, PaymentMethod


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ServiceIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    price_paise: int = Field(ge=0, le=10_000_000)


class ServiceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    price_paise: int | None = Field(default=None, ge=0, le=10_000_000)
    is_active: bool | None = None


class ServiceOut(ORM):
    id: uuid.UUID
    name: str
    price_paise: int
    is_active: bool


class InvoiceItemIn(BaseModel):
    service_id: uuid.UUID
    qty: int = Field(default=1, ge=1, le=100)


class InvoiceIn(BaseModel):
    appointment_id: uuid.UUID | None = None
    items: list[InvoiceItemIn] = Field(min_length=1, max_length=50)


class InvoiceItemOut(BaseModel):
    id: uuid.UUID
    service_id: uuid.UUID
    service_name: str
    qty: int
    price_paise: int


class PaymentIn(BaseModel):
    method: PaymentMethod
    amount_paise: int = Field(gt=0, le=100_000_000)


class PaymentOut(ORM):
    id: uuid.UUID
    method: PaymentMethod
    amount_paise: int
    received_by: uuid.UUID
    received_at: datetime


class InvoiceOut(BaseModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    appointment_id: uuid.UUID | None
    status: InvoiceStatus
    total_paise: int
    paid_paise: int
    created_at: datetime
    items: list[InvoiceItemOut]
    payments: list[PaymentOut]


class InvoiceListItem(BaseModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    patient_name: str
    status: InvoiceStatus
    total_paise: int
    created_at: datetime


class DailyRevenue(BaseModel):
    day: date
    total_paise: int
    by_method: dict[str, int]
    payments: int
    invoices_paid: int
