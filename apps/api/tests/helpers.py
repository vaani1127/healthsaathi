"""Shared helpers for API tests: clinics, signed-in actors, counting log rows."""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db import TenantContext
from app.db import models as m
from app.db.enums import Role, Sex
from app.identity import service, tokens
from app.identity.deps import ClinicPrincipal


@dataclass(frozen=True)
class Actor:
    user_id: uuid.UUID
    clinic_id: uuid.UUID
    role: Role
    session_id: uuid.UUID
    token: str

    @property
    def headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.token}"}

    @property
    def principal(self) -> ClinicPrincipal:
        amr = ("email_otp",) if self.role == Role.PATIENT else ("pwd", "otp")
        return ClinicPrincipal(
            user_id=self.user_id,
            session_id=self.session_id,
            token_id="test",
            amr=amr,
            clinic_id=self.clinic_id,
            role=self.role,
        )

    @property
    def ctx(self) -> TenantContext:
        return TenantContext(clinic_id=self.clinic_id, user_id=self.user_id, role=self.role.value)


async def make_clinic(engine: AsyncEngine, name: str | None = None) -> uuid.UUID:
    async with AsyncSession(engine) as s, s.begin():
        clinic = m.Clinic(name=name or f"Clinic {uuid.uuid4().hex[:8]}", city="Test", state="Test")
        s.add(clinic)
        await s.flush()
        return clinic.id


async def make_actor(
    engine: AsyncEngine, clinic_id: uuid.UUID, role: Role, user_id: uuid.UUID | None = None
) -> Actor:
    async with AsyncSession(engine) as s, s.begin():
        if user_id is None:
            user = m.User(
                email=f"{role.value}.{uuid.uuid4().hex[:12]}@api.test", name=f"{role.value} actor"
            )
            s.add(user)
            await s.flush()
            user_id = user.id
        s.add(m.Membership(clinic_id=clinic_id, user_id=user_id, role=role))
    kind: service.SessionKind = "email" if role == Role.PATIENT else "mfa"
    issued = await service.create_session(
        user_id, kind, service.DeviceInfo("test-device", "pytest")
    )
    token, _ = tokens.access_token(
        user_id, issued.session_id, service.AMR[kind], clinic_id, role.value
    )
    return Actor(user_id, clinic_id, role, issued.session_id, token)


async def make_patient(
    engine: AsyncEngine,
    clinic_id: uuid.UUID,
    created_by: uuid.UUID,
    user_id: uuid.UUID | None = None,
    name: str = "Test Patient",
) -> uuid.UUID:
    async with AsyncSession(engine) as s, s.begin():
        patient = m.Patient(
            clinic_id=clinic_id,
            mrn=f"T{uuid.uuid4().hex[:8].upper()}",
            name=name,
            sex=Sex.FEMALE,
            created_by=created_by,
            user_id=user_id,
        )
        s.add(patient)
        await s.flush()
        return patient.id


async def access_events(
    engine: AsyncEngine, clinic_id: uuid.UUID
) -> list[tuple[m.AccessEvent, m.AccessExplanation | None]]:
    async with AsyncSession(engine) as s:
        rows = await s.execute(
            select(m.AccessEvent, m.AccessExplanation)
            .outerjoin(m.AccessExplanation, m.AccessExplanation.access_event_id == m.AccessEvent.id)
            .where(m.AccessEvent.clinic_id == clinic_id)
            .order_by(m.AccessEvent.at)
        )
        return [(e, x) for e, x in rows.all()]


async def count_rows(
    engine: AsyncEngine, model: type[m.AuditEvent] | type[m.AccessEvent], clinic_id: uuid.UUID
) -> int:
    async with AsyncSession(engine) as s:
        return int(
            await s.scalar(
                select(func.count()).select_from(model).where(model.clinic_id == clinic_id)
            )
            or 0
        )
