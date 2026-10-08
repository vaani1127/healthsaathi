import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.access.deps import Db, ReqInfo
from app.chart import schemas, service
from app.db.enums import STAFF_ROLES, Role
from app.identity.deps import ClinicPrincipal, require_roles
from e2d_core.explain.model import AccessReason

router = APIRouter(tags=["chart"])

R = Role


def _only(*allowed: Role) -> Any:
    return Depends(require_roles(*allowed))


ChartReaders = Annotated[ClinicPrincipal, _only(*(STAFF_ROLES - {R.PLATFORM_ADMIN}))]
Doctor = Annotated[ClinicPrincipal, _only(R.DOCTOR)]
Clinicians = Annotated[ClinicPrincipal, _only(R.DOCTOR, R.NURSE)]
Admin = Annotated[ClinicPrincipal, _only(R.CLINIC_ADMIN)]


@router.get(
    "/patients/{patient_id}/chart",
    response_model=schemas.Chart,
    responses={428: {"description": "A doctor must give a reason to open this chart."}},
)
async def chart(
    principal: ChartReaders, patient_id: uuid.UUID, db: Db, req: ReqInfo
) -> (
    schemas.ReceptionChart
    | schemas.NurseChart
    | schemas.LabChart
    | schemas.DoctorChart
    | schemas.AdminChart
):
    return await service.build_chart(db, principal, patient_id, req)


@router.post("/patients/{patient_id}/chart", response_model=schemas.DoctorChart)
async def chart_with_reason(
    principal: Doctor,
    patient_id: uuid.UUID,
    body: schemas.AccessReasonIn,
    db: Db,
    req: ReqInfo,
) -> schemas.DoctorChart:
    reason = AccessReason(body.reason_code, body.reason_text.strip())
    result = await service.build_chart(db, principal, patient_id, req, reason=reason)
    assert isinstance(result, schemas.DoctorChart)
    return result


@router.post(
    "/patients/{patient_id}/break-glass",
    response_model=schemas.EmergencyChart,
    status_code=status.HTTP_201_CREATED,
)
async def break_glass(
    principal: Clinicians,
    patient_id: uuid.UUID,
    body: schemas.BreakGlassIn,
    db: Db,
    req: ReqInfo,
) -> schemas.EmergencyChart:
    return await service.start_break_glass(db, principal, patient_id, body, req)


@router.get("/patients/{patient_id}/emergency", response_model=schemas.EmergencyChart)
async def emergency(
    principal: Clinicians,
    patient_id: uuid.UUID,
    break_glass_id: uuid.UUID,
    db: Db,
    req: ReqInfo,
) -> schemas.EmergencyChart:
    return await service.reopen_emergency(db, principal, patient_id, break_glass_id, req)


@router.get("/break-glass", response_model=list[schemas.BreakGlassItem])
async def break_glass_queue(
    principal: Admin,
    db: Db,
    req: ReqInfo,
    pending: Annotated[bool, Query()] = False,
) -> list[schemas.BreakGlassItem]:
    return await service.review_queue(db, principal, pending, req)


@router.post("/break-glass/{break_glass_id}/review", response_model=schemas.BreakGlassItem)
async def review_break_glass(
    principal: Admin,
    break_glass_id: uuid.UUID,
    body: schemas.BreakGlassReviewIn,
    db: Db,
    req: ReqInfo,
) -> schemas.BreakGlassItem:
    event = await service.review_break_glass(db, principal, break_glass_id, body)
    (item,) = await service.review_queue(db, principal, False, req, only_id=event.id)
    return item
