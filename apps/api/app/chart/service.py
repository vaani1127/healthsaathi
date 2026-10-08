"""Role-specific patient charts, break-glass access and its review queue.

Each chart only reads the tables its role may see, and records one access per resource type it
returns, so the access log shows exactly which parts of the record each person opened.
"""

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.service import (
    BREAK_GLASS_WINDOW,
    AccessRecord,
    RequestInfo,
    clinic_timezone,
    db_now,
    record_and_explain,
    record_many,
)
from app.chart import schemas
from app.clinic.schemas import AppointmentOut, PatientOut, PatientSummary
from app.clinical.notes_crypto import decrypt_body
from app.core.errors import ProblemError
from app.db.enums import (
    AccessAction,
    AppointmentStatus,
    ConditionStatus,
    LabOrderStatus,
    ResourceType,
    Role,
)
from app.db.models import (
    Allergy,
    Appointment,
    BreakGlassEvent,
    ClinicalNote,
    Condition,
    Consent,
    ConsentNotice,
    Document,
    Invoice,
    LabOrder,
    LabResult,
    Membership,
    Patient,
    Prescription,
    User,
    Vital,
)
from app.identity.deps import ClinicPrincipal
from app.ledger.writer import append_audit_event
from app.realtime.hub import notify
from e2d_core.explain.model import AccessReason

R = ResourceType
VIEW = AccessAction.VIEW
RECENT_VITALS = 10
RECENT_APPOINTMENTS = 10
LAB_RELEASED_GRACE = timedelta(days=2)
REVIEW_WINDOW = timedelta(hours=24)


async def _patient(db: AsyncSession, patient_id: uuid.UUID) -> Patient:
    patient = await db.get(Patient, patient_id)
    if patient is None:
        raise ProblemError(404, "patient-not-found")
    return patient


async def _appointments(db: AsyncSession, patient_id: uuid.UUID) -> list[AppointmentOut]:
    rows = await db.scalars(
        select(Appointment)
        .where(Appointment.patient_id == patient_id)
        .order_by(Appointment.slot_start.desc())
        .limit(RECENT_APPOINTMENTS)
    )
    return [AppointmentOut.model_validate(a) for a in rows]


async def _vitals(
    db: AsyncSession, patient_id: uuid.UUID, limit: int = RECENT_VITALS
) -> list[schemas.VitalOut]:
    rows = await db.scalars(
        select(Vital)
        .where(Vital.patient_id == patient_id)
        .order_by(Vital.recorded_at.desc())
        .limit(limit)
    )
    return [schemas.VitalOut.model_validate(v) for v in rows]


async def _allergies(
    db: AsyncSession, patient_id: uuid.UUID, active_only: bool = False
) -> list[schemas.AllergyOut]:
    stmt = select(Allergy).where(Allergy.patient_id == patient_id)
    if active_only:
        stmt = stmt.where(Allergy.is_active.is_(True))
    rows = await db.scalars(stmt.order_by(Allergy.created_at))
    return [schemas.AllergyOut.model_validate(a) for a in rows]


async def _conditions(
    db: AsyncSession, patient_id: uuid.UUID, active_only: bool = False
) -> list[schemas.ConditionOut]:
    stmt = select(Condition).where(Condition.patient_id == patient_id)
    if active_only:
        stmt = stmt.where(Condition.status == ConditionStatus.ACTIVE)
    rows = await db.scalars(stmt.order_by(Condition.created_at))
    return [schemas.ConditionOut.model_validate(c) for c in rows]


async def _notes(db: AsyncSession, patient_id: uuid.UUID) -> list[schemas.NoteOut]:
    rows = await db.scalars(
        select(ClinicalNote)
        .where(ClinicalNote.patient_id == patient_id)
        .order_by(ClinicalNote.created_at.desc())
    )
    return [
        schemas.NoteOut(
            id=n.id,
            version=n.version,
            parent_id=n.parent_id,
            author_user_id=n.author_user_id,
            created_at=n.created_at,
            signed_at=n.signed_at,
            body=decrypt_body(n.id, n.body_enc),
        )
        for n in rows
    ]


async def _prescriptions(db: AsyncSession, patient_id: uuid.UUID) -> list[schemas.PrescriptionOut]:
    rows = await db.scalars(
        select(Prescription)
        .where(Prescription.patient_id == patient_id)
        .order_by(Prescription.created_at.desc())
    )
    return [schemas.PrescriptionOut.model_validate(p) for p in rows]


async def _lab_orders(
    db: AsyncSession, patient_id: uuid.UUID, worklist_after: datetime | None = None
) -> list[schemas.LabOrderOut]:
    stmt = (
        select(LabOrder, LabResult)
        .outerjoin(
            LabResult,
            and_(LabResult.lab_order_id == LabOrder.id, LabResult.clinic_id == LabOrder.clinic_id),
        )
        .where(LabOrder.patient_id == patient_id)
        .order_by(LabOrder.created_at.desc())
    )
    if worklist_after is not None:
        stmt = stmt.where(
            LabOrder.status != LabOrderStatus.CANCELLED,
            or_(LabResult.released_at.is_(None), LabResult.released_at >= worklist_after),
        )
    return [
        schemas.LabOrderOut(
            id=o.id,
            tests=o.tests,
            status=o.status,
            ordered_by=o.ordered_by,
            created_at=o.created_at,
            result=schemas.LabResultOut.model_validate(r) if r is not None else None,
        )
        for o, r in (await db.execute(stmt)).all()
    ]


async def _documents(db: AsyncSession, patient_id: uuid.UUID) -> list[schemas.DocumentOut]:
    rows = await db.scalars(
        select(Document)
        .where(Document.patient_id == patient_id)
        .order_by(Document.created_at.desc())
    )
    return [schemas.DocumentOut.model_validate(d) for d in rows]


async def _invoices(db: AsyncSession, patient_id: uuid.UUID) -> list[schemas.InvoiceSummary]:
    rows = await db.scalars(
        select(Invoice).where(Invoice.patient_id == patient_id).order_by(Invoice.created_at.desc())
    )
    return [schemas.InvoiceSummary.model_validate(i) for i in rows]


async def _consent(db: AsyncSession, patient_id: uuid.UUID) -> schemas.ConsentStatus | None:
    row = (
        await db.execute(
            select(ConsentNotice.version, Consent.granted_at, Consent.withdrawn_at)
            .join(
                ConsentNotice,
                and_(
                    ConsentNotice.id == Consent.notice_id,
                    ConsentNotice.clinic_id == Consent.clinic_id,
                ),
            )
            .where(Consent.patient_id == patient_id)
            .order_by(Consent.granted_at.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    return schemas.ConsentStatus(
        notice_version=row.version, granted_at=row.granted_at, withdrawn_at=row.withdrawn_at
    )


async def _today_appointment(
    db: AsyncSession, principal: ClinicPrincipal, patient_id: uuid.UUID
) -> AppointmentOut | None:
    tz = ZoneInfo(await clinic_timezone(db, principal.clinic_id))
    now = await db_now(db)
    start = datetime.combine(now.astimezone(tz).date(), datetime.min.time(), tzinfo=tz)
    row = await db.scalar(
        select(Appointment)
        .where(
            Appointment.patient_id == patient_id,
            Appointment.slot_start >= start,
            Appointment.slot_start < start + timedelta(days=1),
            Appointment.status != AppointmentStatus.CANCELLED,
        )
        .order_by(Appointment.slot_start)
        .limit(1)
    )
    return AppointmentOut.model_validate(row) if row else None


async def build_chart(
    db: AsyncSession,
    principal: ClinicPrincipal,
    patient_id: uuid.UUID,
    req: RequestInfo,
    reason: AccessReason | None = None,
) -> (
    schemas.ReceptionChart
    | schemas.NurseChart
    | schemas.LabChart
    | schemas.DoctorChart
    | schemas.AdminChart
    | schemas.PatientChart
):
    async def record(resource: ResourceType, ask_reason: bool = False) -> AccessRecord:
        return await record_and_explain(
            db, principal, patient_id, resource, VIEW, req, reason=reason, require_reason=ask_reason
        )

    role = principal.role
    if role == Role.DOCTOR:
        first = await record(R.DEMOGRAPHICS, ask_reason=True)
        for resource in (R.VITALS, R.ALLERGIES, R.NOTES, R.PRESCRIPTIONS, R.LAB, R.DOCUMENTS):
            await record(resource)
        patient = await _patient(db, patient_id)
        return schemas.DoctorChart(
            patient=PatientOut.model_validate(patient),
            appointments=await _appointments(db, patient_id),
            vitals=await _vitals(db, patient_id),
            allergies=await _allergies(db, patient_id),
            conditions=await _conditions(db, patient_id),
            notes=await _notes(db, patient_id),
            prescriptions=await _prescriptions(db, patient_id),
            lab_orders=await _lab_orders(db, patient_id),
            documents=await _documents(db, patient_id),
            explanation=first.explanation.template_code,
        )
    if role == Role.NURSE:
        for resource in (R.DEMOGRAPHICS, R.VITALS, R.ALLERGIES):
            await record(resource)
        patient = await _patient(db, patient_id)
        return schemas.NurseChart(
            patient=PatientSummary.model_validate(patient),
            today_appointment=await _today_appointment(db, principal, patient_id),
            vitals=await _vitals(db, patient_id),
            allergies=await _allergies(db, patient_id),
        )
    if role == Role.LAB_TECH:
        for resource in (R.DEMOGRAPHICS, R.LAB):
            await record(resource)
        patient = await _patient(db, patient_id)
        cutoff = await db_now(db) - LAB_RELEASED_GRACE
        return schemas.LabChart(
            patient=PatientSummary.model_validate(patient),
            worklist=await _lab_orders(db, patient_id, worklist_after=cutoff),
        )
    if role == Role.RECEPTION:
        for resource in (R.DEMOGRAPHICS, R.BILLING, R.CONSENT):
            await record(resource)
        patient = await _patient(db, patient_id)
        return schemas.ReceptionChart(
            patient=PatientOut.model_validate(patient),
            appointments=await _appointments(db, patient_id),
            invoices=await _invoices(db, patient_id),
            consent=await _consent(db, patient_id),
        )
    if role == Role.PATIENT:
        return await patient_chart(db, principal, patient_id, req)
    if role == Role.CLINIC_ADMIN:
        await record(R.DEMOGRAPHICS)
        return schemas.AdminChart(patient=PatientOut.model_validate(await _patient(db, patient_id)))
    raise ProblemError(403, "forbidden-role", "Your role has no chart view.")


PATIENT_RESOURCES = (
    R.DEMOGRAPHICS,
    R.VITALS,
    R.ALLERGIES,
    R.NOTES,
    R.PRESCRIPTIONS,
    R.LAB,
    R.DOCUMENTS,
    R.BILLING,
    R.CONSENT,
)


async def patient_chart(
    db: AsyncSession,
    principal: ClinicPrincipal,
    patient_id: uuid.UUID,
    req: RequestInfo,
    action: AccessAction = VIEW,
) -> schemas.PatientChart:
    for resource in PATIENT_RESOURCES:
        await record_and_explain(db, principal, patient_id, resource, action, req)
    patient = await _patient(db, patient_id)
    orders = [o for o in await _lab_orders(db, patient_id) if o.result and o.result.released_at]
    released_docs = {o.result.document_id for o in orders if o.result and o.result.document_id}
    return schemas.PatientChart(
        patient=PatientOut.model_validate(patient),
        appointments=await _appointments(db, patient_id),
        vitals=await _vitals(db, patient_id),
        allergies=await _allergies(db, patient_id),
        conditions=await _conditions(db, patient_id),
        notes=[n for n in await _notes(db, patient_id) if n.signed_at],
        prescriptions=[p for p in await _prescriptions(db, patient_id) if p.signed_at],
        lab_orders=orders,
        documents=[d for d in await _documents(db, patient_id) if d.id in released_docs],
        invoices=await _invoices(db, patient_id),
        consent=await _consent(db, patient_id),
    )


# Break-glass ----------------------------------------------------------------------------------

EMERGENCY_RESOURCES = (R.DEMOGRAPHICS, R.ALLERGIES, R.NOTES, R.PRESCRIPTIONS, R.VITALS)


async def _current_medicines(db: AsyncSession, patient_id: uuid.UUID) -> list[dict[str, object]]:
    latest = await db.scalar(
        select(Prescription)
        .where(Prescription.patient_id == patient_id, Prescription.signed_at.is_not(None))
        .order_by(Prescription.signed_at.desc())
        .limit(1)
    )
    return list(latest.items) if latest else []


async def emergency_view(
    db: AsyncSession,
    principal: ClinicPrincipal,
    patient_id: uuid.UUID,
    break_glass: BreakGlassEvent,
    req: RequestInfo,
) -> schemas.EmergencyChart:
    for resource in EMERGENCY_RESOURCES:
        await record_and_explain(
            db, principal, patient_id, resource, VIEW, req, break_glass_id=break_glass.id
        )
    patient = await _patient(db, patient_id)
    vitals = await _vitals(db, patient_id, limit=1)
    return schemas.EmergencyChart(
        break_glass_id=break_glass.id,
        expires_at=break_glass.at + BREAK_GLASS_WINDOW,
        patient=PatientSummary.model_validate(patient),
        allergies=await _allergies(db, patient_id, active_only=True),
        conditions=await _conditions(db, patient_id, active_only=True),
        current_medicines=await _current_medicines(db, patient_id),
        last_vitals=vitals[0] if vitals else None,
    )


async def start_break_glass(
    db: AsyncSession,
    principal: ClinicPrincipal,
    patient_id: uuid.UUID,
    body: schemas.BreakGlassIn,
    req: RequestInfo,
) -> schemas.EmergencyChart:
    await _patient(db, patient_id)
    event = BreakGlassEvent(
        clinic_id=principal.clinic_id,
        user_id=principal.user_id,
        patient_id=patient_id,
        reason_code=body.reason_code,
        reason_text=body.reason_text.strip(),
        at=await db_now(db),
    )
    db.add(event)
    await db.flush()
    await append_audit_event(
        db,
        principal.clinic_id,
        "break_glass",
        {
            "type": "break_glass",
            "break_glass_id": str(event.id),
            "user_id": str(principal.user_id),
            "patient_id": str(patient_id),
            "reason_code": body.reason_code,
            "at": event.at.isoformat(),
        },
    )
    await notify(db, principal.clinic_id, "break_glass.created", break_glass_id=event.id)
    return await emergency_view(db, principal, patient_id, event, req)


async def reopen_emergency(
    db: AsyncSession,
    principal: ClinicPrincipal,
    patient_id: uuid.UUID,
    break_glass_id: uuid.UUID,
    req: RequestInfo,
) -> schemas.EmergencyChart:
    event = await db.get(BreakGlassEvent, break_glass_id)
    if event is None or event.patient_id != patient_id:
        raise ProblemError(404, "break-glass-not-found")
    return await emergency_view(db, principal, patient_id, event, req)


async def review_queue(
    db: AsyncSession,
    principal: ClinicPrincipal,
    pending_only: bool,
    req: RequestInfo,
    only_id: uuid.UUID | None = None,
) -> list[schemas.BreakGlassItem]:
    """Break-glass events awaiting review, plus the last 24 hours of reviewed ones."""
    stmt = (
        select(BreakGlassEvent, User.name, Patient)
        .join(User, User.id == BreakGlassEvent.user_id)
        .join(
            Patient,
            and_(
                Patient.id == BreakGlassEvent.patient_id,
                Patient.clinic_id == BreakGlassEvent.clinic_id,
            ),
        )
        .where(BreakGlassEvent.clinic_id == principal.clinic_id)
        .order_by(BreakGlassEvent.at.desc())
    )
    if only_id is not None:
        stmt = stmt.where(BreakGlassEvent.id == only_id)
    elif pending_only:
        stmt = stmt.where(BreakGlassEvent.reviewed_at.is_(None))
    else:
        since = await db_now(db) - REVIEW_WINDOW
        stmt = stmt.where(or_(BreakGlassEvent.reviewed_at.is_(None), BreakGlassEvent.at >= since))
    rows = (await db.execute(stmt)).all()
    roles = await _staff_roles(db, principal.clinic_id, {e.user_id for e, _, _ in rows})
    await record_many(db, principal, [p.id for _, _, p in rows], R.DEMOGRAPHICS, VIEW, req)
    return [
        schemas.BreakGlassItem(
            id=e.id,
            at=e.at,
            user_id=e.user_id,
            user_name=name,
            user_role=roles.get(e.user_id),
            patient=PatientSummary.model_validate(p),
            reason_code=e.reason_code,
            reason_text=e.reason_text,
            reviewed_at=e.reviewed_at,
            reviewed_by=e.reviewed_by,
            outcome=e.outcome,
        )
        for e, name, p in rows
    ]


async def _staff_roles(
    db: AsyncSession, clinic_id: uuid.UUID, users: set[uuid.UUID]
) -> dict[uuid.UUID, Role]:
    if not users:
        return {}
    rows = await db.execute(
        select(Membership.user_id, func.min(Membership.role))
        .where(
            Membership.clinic_id == clinic_id,
            Membership.user_id.in_(users),
            Membership.role.in_([Role.DOCTOR, Role.NURSE]),
        )
        .group_by(Membership.user_id)
    )
    return {u: Role(r) for u, r in rows.all()}


async def review_break_glass(
    db: AsyncSession,
    principal: ClinicPrincipal,
    break_glass_id: uuid.UUID,
    body: schemas.BreakGlassReviewIn,
) -> BreakGlassEvent:
    event = await db.get(BreakGlassEvent, break_glass_id)
    if event is None:
        raise ProblemError(404, "break-glass-not-found")
    if event.reviewed_at is not None:
        raise ProblemError(409, "already-reviewed")
    if event.user_id == principal.user_id:
        raise ProblemError(
            403, "own-break-glass", "Someone else must review your break-glass access."
        )
    event.reviewed_by = principal.user_id
    event.reviewed_at = await db_now(db)
    event.outcome = body.outcome
    await db.flush()
    await append_audit_event(
        db,
        principal.clinic_id,
        "break_glass_review",
        {
            "type": "break_glass_review",
            "break_glass_id": str(event.id),
            "reviewer_user_id": str(principal.user_id),
            "outcome": body.outcome.value,
            "note": body.note,
            "at": event.reviewed_at.isoformat(),
        },
    )
    return event
