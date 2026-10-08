import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from app.access.deps import Db, ReqInfo
from app.clinic.schemas import PatientOut
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT
from app.core.ratelimit import EXPORT_PRINT, limiter
from app.db.enums import Role
from app.identity.deps import ClinicPrincipal, require_roles
from app.portal import schemas, service

router = APIRouter(tags=["portal"])
R = Role


def _only(*allowed: Role) -> Any:
    return Depends(require_roles(*allowed))


PatientUser = Annotated[ClinicPrincipal, _only(R.PATIENT)]
LogReaders = Annotated[ClinicPrincipal, _only(R.PATIENT, R.CLINIC_ADMIN)]
Admin = Annotated[ClinicPrincipal, _only(R.CLINIC_ADMIN)]
Reception = Annotated[ClinicPrincipal, _only(R.RECEPTION)]


@router.get("/me/patient", response_model=PatientOut)
async def my_patient(principal: PatientUser, db: Db, req: ReqInfo) -> PatientOut:
    return PatientOut.model_validate(await service.my_patient(db, principal, req))


@router.get("/patients/{patient_id}/access-log", response_model=schemas.AccessLogPage)
async def access_log(
    principal: LogReaders,
    patient_id: uuid.UUID,
    db: Db,
    req: ReqInfo,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> schemas.AccessLogPage:
    items, next_cursor = await service.access_log(db, principal, patient_id, cursor, limit, req)
    return schemas.AccessLogPage(items=items, next_cursor=next_cursor)


@router.post(
    "/access-events/{access_event_id}/query", response_model=schemas.QueryOut, status_code=201
)
async def raise_query(
    principal: PatientUser,
    access_event_id: uuid.UUID,
    body: schemas.QueryIn,
    db: Db,
    req: ReqInfo,
) -> schemas.QueryOut:
    query = await service.raise_query(db, principal, access_event_id, body, req)
    return schemas.QueryOut.model_validate(query)


@router.get("/access-queries", response_model=list[schemas.QueryOut])
async def list_queries(principal: Admin, db: Db, open_only: bool = True) -> list[schemas.QueryOut]:
    rows = await service.list_queries(db, principal, open_only)
    return [schemas.QueryOut.model_validate(q) for q in rows]


@router.patch("/access-queries/{query_id}", response_model=schemas.QueryOut)
async def update_query(
    principal: Admin, query_id: uuid.UUID, body: schemas.QueryUpdate, db: Db
) -> schemas.QueryOut:
    return schemas.QueryOut.model_validate(await service.update_query(db, query_id, body))


@router.get("/patients/{patient_id}/export", response_model=schemas.PatientExport)
@limiter.limit(EXPORT_PRINT)
async def export(
    request: Request, principal: PatientUser, patient_id: uuid.UUID, db: Db, req: ReqInfo
) -> schemas.PatientExport:
    return await service.export(db, principal, patient_id, req)


@router.post("/patients/{patient_id}/portal-access", response_model=PatientOut)
async def portal_access(
    principal: Reception,
    patient_id: uuid.UUID,
    body: schemas.PortalAccessIn,
    db: Db,
    req: ReqInfo,
) -> PatientOut:
    patient = await service.grant_portal_access(db, principal, patient_id, body, req)
    return PatientOut.model_validate(patient)
