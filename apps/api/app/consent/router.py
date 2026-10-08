import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.access.deps import Db, ReqInfo
from app.consent import schemas, service
from app.db.enums import STAFF_ROLES, Role
from app.identity.deps import ClinicPrincipal, require_roles

router = APIRouter(tags=["consent"])
R = Role


def _only(*allowed: Role) -> Any:
    return Depends(require_roles(*allowed))


Anyone = Annotated[ClinicPrincipal, _only(*(STAFF_ROLES - {R.PLATFORM_ADMIN}), R.PATIENT)]
Admin = Annotated[ClinicPrincipal, _only(R.CLINIC_ADMIN)]
Granters = Annotated[ClinicPrincipal, _only(R.RECEPTION, R.PATIENT)]
Readers = Annotated[ClinicPrincipal, _only(R.RECEPTION, R.CLINIC_ADMIN, R.DOCTOR, R.PATIENT)]


@router.get("/consent-notices", response_model=list[schemas.NoticeOut])
async def list_notices(principal: Anyone, db: Db) -> list[schemas.NoticeOut]:
    include_drafts = principal.role == R.CLINIC_ADMIN
    rows = await service.list_notices(db, principal.clinic_id, include_drafts)
    return [schemas.NoticeOut.model_validate(n) for n in rows]


@router.get("/consent-notices/current", response_model=schemas.NoticeOut | None)
async def current_notice(principal: Anyone, db: Db) -> schemas.NoticeOut | None:
    notice = await service.current_notice(db, principal.clinic_id)
    return schemas.NoticeOut.model_validate(notice) if notice else None


@router.post("/consent-notices", response_model=schemas.NoticeOut, status_code=201)
async def create_notice(principal: Admin, body: schemas.NoticeIn, db: Db) -> schemas.NoticeOut:
    return schemas.NoticeOut.model_validate(await service.create_notice(db, principal, body))


@router.post("/consent-notices/{notice_id}/publish", response_model=schemas.NoticeOut)
async def publish_notice(principal: Admin, notice_id: uuid.UUID, db: Db) -> schemas.NoticeOut:
    return schemas.NoticeOut.model_validate(await service.publish_notice(db, notice_id))


@router.post("/patients/{patient_id}/consents", response_model=schemas.ConsentOut, status_code=201)
async def grant(
    principal: Granters, patient_id: uuid.UUID, db: Db, req: ReqInfo
) -> schemas.ConsentOut:
    return await service.consent_out(db, await service.grant(db, principal, patient_id, req))


@router.get("/patients/{patient_id}/consents", response_model=list[schemas.ConsentOut])
async def history(
    principal: Readers, patient_id: uuid.UUID, db: Db, req: ReqInfo
) -> list[schemas.ConsentOut]:
    return await service.history(db, principal, patient_id, req)


@router.post("/consents/{consent_id}/withdraw", response_model=schemas.ConsentOut)
async def withdraw(
    principal: Granters, consent_id: uuid.UUID, db: Db, req: ReqInfo
) -> schemas.ConsentOut:
    return await service.consent_out(db, await service.withdraw(db, principal, consent_id, req))
