import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from app.access.deps import Db, ReqInfo
from app.billing import schemas, service
from app.core.ratelimit import EXPORT_PRINT, limiter
from app.db.enums import STAFF_ROLES, Role
from app.identity.deps import ClinicPrincipal, require_roles

router = APIRouter(tags=["billing"])
R = Role


def _only(*allowed: Role) -> Any:
    return Depends(require_roles(*allowed))


Staff = Annotated[ClinicPrincipal, _only(*(STAFF_ROLES - {R.PLATFORM_ADMIN}))]
Admin = Annotated[ClinicPrincipal, _only(R.CLINIC_ADMIN)]
Cashier = Annotated[ClinicPrincipal, _only(R.RECEPTION)]
InvoiceReaders = Annotated[ClinicPrincipal, _only(R.RECEPTION, R.CLINIC_ADMIN, R.PATIENT)]
Reporters = Annotated[ClinicPrincipal, _only(R.RECEPTION, R.CLINIC_ADMIN)]


@router.get("/services", response_model=list[schemas.ServiceOut])
async def list_services(principal: Staff, db: Db, active: bool = True) -> list[schemas.ServiceOut]:
    rows = await service.list_services(db, principal.clinic_id, active)
    return [schemas.ServiceOut.model_validate(s) for s in rows]


@router.post("/services", response_model=schemas.ServiceOut, status_code=201)
async def create_service(principal: Admin, body: schemas.ServiceIn, db: Db) -> schemas.ServiceOut:
    return schemas.ServiceOut.model_validate(await service.create_service(db, principal, body))


@router.patch("/services/{service_id}", response_model=schemas.ServiceOut)
async def update_service(
    principal: Admin, service_id: uuid.UUID, body: schemas.ServiceUpdate, db: Db
) -> schemas.ServiceOut:
    return schemas.ServiceOut.model_validate(await service.update_service(db, service_id, body))


@router.post("/patients/{patient_id}/invoices", response_model=schemas.InvoiceOut, status_code=201)
async def create_invoice(
    principal: Cashier, patient_id: uuid.UUID, body: schemas.InvoiceIn, db: Db, req: ReqInfo
) -> schemas.InvoiceOut:
    invoice = await service.create_invoice(db, principal, patient_id, body, req)
    return await service.invoice_out(db, invoice)


@router.get("/invoices", response_model=list[schemas.InvoiceListItem])
async def list_invoices(
    principal: Reporters, db: Db, req: ReqInfo, day: date | None = None
) -> list[schemas.InvoiceListItem]:
    return await service.list_invoices(db, principal, day, req)


@router.get("/invoices/{invoice_id}", response_model=schemas.InvoiceOut)
async def get_invoice(
    principal: InvoiceReaders, invoice_id: uuid.UUID, db: Db, req: ReqInfo
) -> schemas.InvoiceOut:
    return await service.invoice_out(db, await service.get_invoice(db, principal, invoice_id, req))


@router.get("/invoices/{invoice_id}/receipt", response_model=schemas.InvoiceOut)
@limiter.limit(EXPORT_PRINT)
async def receipt(
    request: Request, principal: InvoiceReaders, invoice_id: uuid.UUID, db: Db, req: ReqInfo
) -> schemas.InvoiceOut:
    invoice = await service.get_invoice(db, principal, invoice_id, req, print_view=True)
    return await service.invoice_out(db, invoice)


@router.post("/invoices/{invoice_id}/issue", response_model=schemas.InvoiceOut)
async def issue_invoice(
    principal: Cashier, invoice_id: uuid.UUID, db: Db, req: ReqInfo
) -> schemas.InvoiceOut:
    return await service.invoice_out(
        db, await service.issue_invoice(db, principal, invoice_id, req)
    )


@router.post("/invoices/{invoice_id}/void", response_model=schemas.InvoiceOut)
async def void_invoice(
    principal: Cashier, invoice_id: uuid.UUID, db: Db, req: ReqInfo
) -> schemas.InvoiceOut:
    return await service.invoice_out(db, await service.void_invoice(db, principal, invoice_id, req))


@router.post("/invoices/{invoice_id}/payments", response_model=schemas.InvoiceOut, status_code=201)
async def add_payment(
    principal: Cashier,
    invoice_id: uuid.UUID,
    body: schemas.PaymentIn,
    db: Db,
    req: ReqInfo,
) -> schemas.InvoiceOut:
    invoice = await service.add_payment(db, principal, invoice_id, body, req)
    return await service.invoice_out(db, invoice)


@router.get("/reports/daily-revenue", response_model=schemas.DailyRevenue)
async def daily_revenue(
    principal: Reporters, db: Db, day: date | None = None
) -> schemas.DailyRevenue:
    return await service.daily_revenue(db, principal.clinic_id, day)
