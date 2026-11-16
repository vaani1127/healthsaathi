import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends

from app.access.deps import Db, ReqInfo
from app.db.enums import AlertStatus, Role
from app.detect import schemas, service
from app.identity.deps import ClinicPrincipal, require_roles

router = APIRouter(tags=["alerts"])

Admin = Annotated[ClinicPrincipal, Depends(require_roles(Role.CLINIC_ADMIN))]


@router.get("/alerts", response_model=list[schemas.AlertOut])
async def list_alerts(
    principal: Admin,
    db: Db,
    day: date | None = None,
    status: AlertStatus | None = None,
    include_retired: bool = False,
) -> list[schemas.AlertOut]:
    """Retired alerts (provisional, not in the day's final list, never reviewed) are left out
    unless `include_retired` is true. They are never in `status=open`: they are not open for
    review."""
    return await service.list_alerts(db, principal.clinic_id, day, status, include_retired)


@router.get("/alerts/days/{day}", response_model=schemas.DayStatus)
async def day_status(principal: Admin, day: date, db: Db) -> schemas.DayStatus:
    """Whether the day's alert list is final, and from when it can be."""
    return await service.day_status(db, principal.clinic_id, day)


@router.get("/alerts/{alert_id}", response_model=schemas.AlertDetail)
async def alert_detail(
    principal: Admin, alert_id: uuid.UUID, db: Db, req: ReqInfo
) -> schemas.AlertDetail:
    return await service.alert_detail(db, principal, alert_id, req)


@router.post("/alerts/{alert_id}/review", response_model=schemas.AlertDetail)
async def review_alert(
    principal: Admin, alert_id: uuid.UUID, body: schemas.ReviewIn, db: Db, req: ReqInfo
) -> schemas.AlertDetail:
    await service.review_alert(db, principal, alert_id, body)
    return await service.alert_detail(db, principal, alert_id, req)
