import hmac
import uuid
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header

from app.access.deps import Db, ReqInfo
from app.anchor import job, receipts
from app.core.config import get_settings
from app.core.errors import ProblemError
from app.db.enums import Role
from app.identity.deps import ClinicPrincipal, require_roles

router = APIRouter(tags=["audit"])

PatientUser = Annotated[ClinicPrincipal, Depends(require_roles(Role.PATIENT))]
Admin = Annotated[ClinicPrincipal, Depends(require_roles(Role.CLINIC_ADMIN))]


@router.get("/access-events/{access_event_id}/receipt", response_model=receipts.Receipt)
async def receipt(
    principal: PatientUser, access_event_id: uuid.UUID, db: Db, req: ReqInfo
) -> receipts.Receipt:
    return await receipts.receipt_for(db, principal, access_event_id, req)


@router.get("/audit/status", response_model=receipts.AuditStatus)
async def audit_status(principal: Admin, db: Db) -> receipts.AuditStatus:
    return await receipts.audit_status(db, principal.clinic_id)


@router.post("/internal/anchor/run")
async def run_anchor_job(authorization: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    """Called by the scheduled GitHub Action. Disabled unless ANCHOR_TRIGGER_TOKEN is set."""
    expected = get_settings().anchor_trigger_token
    given = (authorization or "").removeprefix("Bearer ").strip()
    if expected is None or not hmac.compare_digest(
        given.encode(), expected.get_secret_value().encode()
    ):
        raise ProblemError(401, "not-authenticated")
    backends = job.backends_from_settings()
    try:
        runs = await job.run(backends)
    finally:
        if backends.evm is not None:
            await backends.evm.close()
    if runs is None:
        return {"status": "already_running"}
    return {"status": "done", "clinics": [asdict(r) for r in runs]}
