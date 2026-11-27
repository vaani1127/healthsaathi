import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.access.deps import Db, ReqInfo
from app.db.enums import STAFF_ROLES, RecordType, Role
from app.identity.deps import ClinicPrincipal, require_roles
from app.records import schemas, service

router = APIRouter(tags=["records"])


def _only(*allowed: Role) -> Any:
    return Depends(require_roles(*allowed))


# The access policy then decides per record type (for example reception and nurses cannot open
# notes); a patient may check only records of their own chart.
RecordReaders = Annotated[
    ClinicPrincipal, _only(*(STAFF_ROLES - {Role.PLATFORM_ADMIN}), Role.PATIENT)
]


@router.get(
    "/records/{record_type}/{record_id}/integrity",
    response_model=schemas.RecordIntegrity,
    summary="Check a clinical record against its committed versions",
)
async def record_integrity(
    principal: RecordReaders,
    record_type: RecordType,
    record_id: uuid.UUID,
    db: Db,
    req: ReqInfo,
) -> schemas.RecordIntegrity:
    """Tamper-evident check of one clinical record: recomputes the salted commitment of its
    newest version from the record as it is now, and returns, for every version, the audit leaf,
    its inclusion proof to a signed tree head and where that head was anchored."""
    return await service.integrity(db, principal, record_type, record_id, req)


Admin = Annotated[ClinicPrincipal, _only(Role.CLINIC_ADMIN)]


@router.get(
    "/audit/records-status",
    response_model=schemas.RecordsStatus,
    tags=["audit"],
    summary="Check every clinical record of the clinic against its committed versions",
)
async def records_status(principal: Admin, db: Db) -> schemas.RecordsStatus:
    """Counts per verdict. Computed on demand and reused until the next checkpoint is signed or
    anchored, because it reads every committed record. Returns counts only, no patient data."""
    return await service.records_status(db, principal.clinic_id)
