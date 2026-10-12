"""Write the FHIR bundles of the 10 most active patients, for the HAPI validator (P17).

Each bundle is produced the way a patient gets it: reception gives the patient a portal account if
they have none, then the patient's own export runs (`portal.service.export`), which logs the
export like any other. Use on a seeded demo database:

    python -m hs_demo.fhir_samples OUT_DIR [COUNT]
"""

import asyncio
import json
import sys
import uuid
from pathlib import Path

from sqlalchemy import func, select

from app.access.service import RequestInfo
from app.core.db import TenantContext, get_engine, tenant_session
from app.db.enums import Role
from app.db.models import AccessEvent, Clinic, Membership, Patient
from app.identity.deps import ClinicPrincipal
from app.portal import schemas as portal_schemas
from app.portal import service as portal
from e2d_core.ids import uuid7

REQUEST = RequestInfo(request_id="fhir-samples")


def principal(user_id: uuid.UUID, clinic_id: uuid.UUID, role: Role) -> ClinicPrincipal:
    return ClinicPrincipal(
        user_id=user_id,
        session_id=uuid7(),
        token_id="fhir-samples",  # noqa: S106 - a label, not a secret
        amr=("pwd", "otp") if role != Role.PATIENT else ("email_otp",),
        clinic_id=clinic_id,
        role=role,
    )


async def busiest_patients(count: int) -> list[tuple[uuid.UUID, uuid.UUID]]:
    found: list[tuple[int, uuid.UUID, uuid.UUID]] = []
    async with tenant_session(TenantContext(role=Role.PLATFORM_ADMIN)) as db:
        clinics = (await db.scalars(select(Clinic.id))).all()
    for clinic_id in clinics:
        async with tenant_session(TenantContext(clinic_id=clinic_id, role="system")) as db:
            rows = await db.execute(
                select(AccessEvent.patient_id, func.count())
                .where(AccessEvent.clinic_id == clinic_id)
                .group_by(AccessEvent.patient_id)
            )
            found += [(n, clinic_id, pid) for pid, n in rows]
    found.sort(key=lambda x: (-x[0], str(x[2])))
    return [(clinic_id, pid) for _, clinic_id, pid in found[:count]]


async def patient_user(clinic_id: uuid.UUID, patient_id: uuid.UUID) -> uuid.UUID:
    async with tenant_session(TenantContext(clinic_id=clinic_id, role="system")) as db:
        patient = await db.get(Patient, patient_id)
        assert patient is not None
        if patient.user_id is not None:
            return patient.user_id
        reception = await db.scalar(
            select(Membership.user_id)
            .where(Membership.clinic_id == clinic_id, Membership.role == Role.RECEPTION)
            .limit(1)
        )
        assert reception is not None
    staff = principal(reception, clinic_id, Role.RECEPTION)
    email = f"fhir.{str(patient_id)[-12:]}@demo.healthsaathi.test"
    async with tenant_session(staff.tenant()) as db:
        linked = await portal.grant_portal_access(
            db, staff, patient_id, portal_schemas.PortalAccessIn(email=email), REQUEST
        )
        assert linked.user_id is not None
        return linked.user_id


async def write_samples(out: Path, count: int) -> list[Path]:
    await asyncio.to_thread(out.mkdir, parents=True, exist_ok=True)
    written = []
    for clinic_id, patient_id in await busiest_patients(count):
        user_id = await patient_user(clinic_id, patient_id)
        me = principal(user_id, clinic_id, Role.PATIENT)
        async with tenant_session(me.tenant()) as db:
            export = await portal.export(db, me, patient_id, REQUEST)
        path = out / f"bundle-{patient_id}.json"
        body = json.dumps(export.fhir, indent=2, ensure_ascii=False).encode()
        await asyncio.to_thread(path.write_bytes, body)
        written.append(path)
    return written


async def main(argv: list[str]) -> int:
    out = Path(argv[0]) if argv else Path("fhir-samples")
    count = int(argv[1]) if len(argv) > 1 else 10
    try:
        paths = await write_samples(out, count)
    finally:
        await get_engine().dispose()
    for path in paths:
        entries = json.loads(path.read_text(encoding="utf-8"))["entry"]
        kinds: dict[str, int] = {}
        for entry in entries:
            kinds[entry["resource"]["resourceType"]] = (
                kinds.get(entry["resource"]["resourceType"], 0) + 1
            )
        print(path.name, json.dumps(kinds, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
