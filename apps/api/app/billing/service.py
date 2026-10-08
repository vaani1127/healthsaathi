"""Billing: services price list, invoices, payments (cash, UPI, card on an offline terminal) and the
daily revenue report. Amounts are integers in paise."""

import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.service import RequestInfo, clinic_timezone, db_now, record_and_explain, record_many
from app.billing import schemas
from app.clinic.service import local_today
from app.core.errors import ProblemError
from app.db.enums import AccessAction, InvoiceStatus, ResourceType
from app.db.models import Appointment, Invoice, InvoiceItem, Patient, Payment, Service
from app.identity.deps import ClinicPrincipal

BILLING = ResourceType.BILLING


async def _record(
    db: AsyncSession,
    principal: ClinicPrincipal,
    patient_id: uuid.UUID,
    action: AccessAction,
    req: RequestInfo,
    **detail: object,
) -> None:
    await record_and_explain(
        db,
        principal,
        patient_id,
        BILLING,
        action,
        req,
        detail={k: str(v) for k, v in detail.items()},
    )


# Services -----------------------------------------------------------------------------------------


async def list_services(
    db: AsyncSession, clinic_id: uuid.UUID, active_only: bool
) -> Sequence[Service]:
    stmt = select(Service).where(Service.clinic_id == clinic_id)
    if active_only:
        stmt = stmt.where(Service.is_active.is_(True))
    return (await db.scalars(stmt.order_by(Service.name))).all()


async def create_service(
    db: AsyncSession, principal: ClinicPrincipal, body: schemas.ServiceIn
) -> Service:
    exists = await db.scalar(
        select(Service.id).where(
            Service.clinic_id == principal.clinic_id, Service.name == body.name
        )
    )
    if exists is not None:
        raise ProblemError(409, "service-exists", "A service with this name already exists.")
    service = Service(clinic_id=principal.clinic_id, **body.model_dump())
    db.add(service)
    await db.flush()
    await db.refresh(service)
    return service


async def update_service(
    db: AsyncSession, service_id: uuid.UUID, body: schemas.ServiceUpdate
) -> Service:
    service = await db.get(Service, service_id)
    if service is None:
        raise ProblemError(404, "service-not-found")
    for key, value in body.model_dump(exclude_unset=True, exclude_none=True).items():
        setattr(service, key, value)
    await db.flush()
    return service


# Invoices -----------------------------------------------------------------------------------------


async def invoice_out(db: AsyncSession, invoice: Invoice) -> schemas.InvoiceOut:
    items = (
        await db.execute(
            select(InvoiceItem, Service.name)
            .join(
                Service,
                and_(
                    Service.id == InvoiceItem.service_id, Service.clinic_id == InvoiceItem.clinic_id
                ),
            )
            .where(InvoiceItem.invoice_id == invoice.id)
            .order_by(Service.name)
        )
    ).all()
    payments = (
        await db.scalars(
            select(Payment).where(Payment.invoice_id == invoice.id).order_by(Payment.received_at)
        )
    ).all()
    return schemas.InvoiceOut(
        id=invoice.id,
        patient_id=invoice.patient_id,
        appointment_id=invoice.appointment_id,
        status=invoice.status,
        total_paise=invoice.total_paise,
        paid_paise=sum(p.amount_paise for p in payments),
        created_at=invoice.created_at,
        items=[
            schemas.InvoiceItemOut(
                id=i.id,
                service_id=i.service_id,
                service_name=name,
                qty=i.qty,
                price_paise=i.price_paise,
            )
            for i, name in items
        ],
        payments=[schemas.PaymentOut.model_validate(p) for p in payments],
    )


async def create_invoice(
    db: AsyncSession,
    principal: ClinicPrincipal,
    patient_id: uuid.UUID,
    body: schemas.InvoiceIn,
    req: RequestInfo,
) -> Invoice:
    await _record(db, principal, patient_id, AccessAction.CREATE, req, op="invoice.create")
    if body.appointment_id is not None:
        appointment = await db.get(Appointment, body.appointment_id)
        if appointment is None or appointment.patient_id != patient_id:
            raise ProblemError(
                422, "appointment-not-found", "That appointment is not for this patient."
            )
    service_ids = {i.service_id for i in body.items}
    services = {
        s.id: s
        for s in (
            await db.scalars(
                select(Service).where(Service.id.in_(service_ids), Service.is_active.is_(True))
            )
        ).all()
    }
    missing = service_ids - set(services)
    if missing:
        raise ProblemError(422, "service-not-found", "One of the services is not available.")

    invoice = Invoice(
        clinic_id=principal.clinic_id,
        patient_id=patient_id,
        appointment_id=body.appointment_id,
        created_by=principal.user_id,
        total_paise=sum(services[i.service_id].price_paise * i.qty for i in body.items),
    )
    db.add(invoice)
    await db.flush()
    for item in body.items:
        db.add(
            InvoiceItem(
                clinic_id=principal.clinic_id,
                invoice_id=invoice.id,
                service_id=item.service_id,
                qty=item.qty,
                price_paise=services[item.service_id].price_paise,
            )
        )
    await db.flush()
    await db.refresh(invoice)
    return invoice


async def _invoice(db: AsyncSession, invoice_id: uuid.UUID, lock: bool = False) -> Invoice:
    invoice = await db.get(Invoice, invoice_id, with_for_update=lock)
    if invoice is None:
        raise ProblemError(404, "invoice-not-found")
    return invoice


async def get_invoice(
    db: AsyncSession,
    principal: ClinicPrincipal,
    invoice_id: uuid.UUID,
    req: RequestInfo,
    print_view: bool = False,
) -> Invoice:
    invoice = await _invoice(db, invoice_id)
    action = AccessAction.PRINT if print_view else AccessAction.VIEW
    await _record(db, principal, invoice.patient_id, action, req, invoice_id=invoice.id)
    return invoice


async def issue_invoice(
    db: AsyncSession, principal: ClinicPrincipal, invoice_id: uuid.UUID, req: RequestInfo
) -> Invoice:
    invoice = await _invoice(db, invoice_id)
    await _record(db, principal, invoice.patient_id, AccessAction.EDIT, req, op="invoice.issue")
    if invoice.status != InvoiceStatus.DRAFT:
        raise ProblemError(409, "invalid-transition", "Only a draft invoice can be issued.")
    invoice.status = InvoiceStatus.ISSUED
    await db.flush()
    return invoice


async def void_invoice(
    db: AsyncSession, principal: ClinicPrincipal, invoice_id: uuid.UUID, req: RequestInfo
) -> Invoice:
    invoice = await _invoice(db, invoice_id)
    await _record(db, principal, invoice.patient_id, AccessAction.EDIT, req, op="invoice.void")
    if invoice.status not in (InvoiceStatus.DRAFT, InvoiceStatus.ISSUED):
        raise ProblemError(409, "invalid-transition", "A paid or void invoice cannot be voided.")
    paid = await db.scalar(
        select(func.count()).select_from(Payment).where(Payment.invoice_id == invoice.id)
    )
    if paid:
        raise ProblemError(409, "has-payments", "An invoice with payments cannot be voided.")
    invoice.status = InvoiceStatus.VOID
    await db.flush()
    return invoice


async def add_payment(
    db: AsyncSession,
    principal: ClinicPrincipal,
    invoice_id: uuid.UUID,
    body: schemas.PaymentIn,
    req: RequestInfo,
) -> Invoice:
    # Lock the invoice so two payments at once cannot both pass the amount-due check.
    invoice = await _invoice(db, invoice_id, lock=True)
    await _record(db, principal, invoice.patient_id, AccessAction.EDIT, req, op="invoice.payment")
    if invoice.status != InvoiceStatus.ISSUED:
        raise ProblemError(409, "invalid-transition", "Payments go on issued invoices only.")
    paid = int(
        await db.scalar(
            select(func.coalesce(func.sum(Payment.amount_paise), 0)).where(
                Payment.invoice_id == invoice.id
            )
        )
        or 0
    )
    if paid + body.amount_paise > invoice.total_paise:
        raise ProblemError(422, "overpayment", "The payment is more than the amount due.")
    db.add(
        Payment(
            clinic_id=principal.clinic_id,
            invoice_id=invoice.id,
            method=body.method,
            amount_paise=body.amount_paise,
            received_by=principal.user_id,
            received_at=await db_now(db),
        )
    )
    if paid + body.amount_paise == invoice.total_paise:
        invoice.status = InvoiceStatus.PAID
    await db.flush()
    return invoice


async def _day_bounds(
    db: AsyncSession, clinic_id: uuid.UUID, day: date | None
) -> tuple[date, datetime, datetime]:
    tz = ZoneInfo(await clinic_timezone(db, clinic_id))
    day = day or await local_today(db, clinic_id)
    start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    return day, start, start + timedelta(days=1)


async def list_invoices(
    db: AsyncSession, principal: ClinicPrincipal, day: date | None, req: RequestInfo
) -> list[schemas.InvoiceListItem]:
    _, start, end = await _day_bounds(db, principal.clinic_id, day)
    rows = (
        await db.execute(
            select(Invoice, Patient.name)
            .join(
                Patient,
                and_(Patient.id == Invoice.patient_id, Patient.clinic_id == Invoice.clinic_id),
            )
            .where(Invoice.created_at >= start, Invoice.created_at < end)
            .order_by(Invoice.created_at)
        )
    ).all()
    await record_many(
        db, principal, [i.patient_id for i, _ in rows], BILLING, AccessAction.VIEW, req
    )
    return [
        schemas.InvoiceListItem(
            id=i.id,
            patient_id=i.patient_id,
            patient_name=name,
            status=i.status,
            total_paise=i.total_paise,
            created_at=i.created_at,
        )
        for i, name in rows
    ]


async def daily_revenue(
    db: AsyncSession, clinic_id: uuid.UUID, day: date | None
) -> schemas.DailyRevenue:
    """Totals only, no patient data, so no access is recorded."""
    day, start, end = await _day_bounds(db, clinic_id, day)
    rows = (
        await db.execute(
            select(Payment.method, Payment.amount_paise, Payment.invoice_id).where(
                Payment.clinic_id == clinic_id,
                Payment.received_at >= start,
                Payment.received_at < end,
            )
        )
    ).all()
    by_method: dict[str, int] = defaultdict(int)
    for method, amount, _ in rows:
        by_method[method.value] += amount
    paid_invoices = await db.scalar(
        select(func.count())
        .select_from(Invoice)
        .where(
            Invoice.clinic_id == clinic_id,
            Invoice.status == InvoiceStatus.PAID,
            Invoice.updated_at >= start,
            Invoice.updated_at < end,
        )
    )
    return schemas.DailyRevenue(
        day=day,
        total_paise=sum(by_method.values()),
        by_method=dict(sorted(by_method.items())),
        payments=len(rows),
        invoices_paid=int(paid_invoices or 0),
    )
