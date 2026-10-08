"""Patient portal: own record, access log with explanations, "I do not recognise this", export, and
portal sign-up by reception.

The access log hides the patient's own views of their record, so opening the log does not fill it.
"""

import uuid
from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.service import RequestInfo, db_now, record_and_explain
from app.chart.service import patient_chart
from app.consent.service import history as consent_history
from app.core.config import get_settings
from app.core.errors import ProblemError
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor
from app.db.enums import AccessAction, QueryStatus, ResourceType, Role
from app.db.models import (
    AccessEvent,
    AccessExplanation,
    Appointment,
    LabOrder,
    Membership,
    Patient,
    PatientAccessQuery,
    QueueToken,
    Referral,
    User,
)
from app.identity.deps import ClinicPrincipal
from app.notify.email import Email, send_email
from app.portal import schemas

DEMOGRAPHICS = ResourceType.DEMOGRAPHICS


async def my_patient(db: AsyncSession, principal: ClinicPrincipal, req: RequestInfo) -> Patient:
    patient = await db.scalar(
        select(Patient).where(
            Patient.clinic_id == principal.clinic_id, Patient.user_id == principal.user_id
        )
    )
    if patient is None:
        raise ProblemError(404, "no-patient-record", "No record is linked to your account here.")
    await record_and_explain(db, principal, patient.id, DEMOGRAPHICS, AccessAction.VIEW, req)
    return patient


# Access log ---------------------------------------------------------------------------------------


async def _because(db: AsyncSession, explanation: AccessExplanation | None) -> schemas.Because:
    if explanation is None or explanation.template_code is None:
        return schemas.Because(template=None)
    refs = explanation.evidence.get("refs") or []
    reason = explanation.evidence.get("reason") or {}
    because = schemas.Because(
        template=explanation.template_code,
        reason_code=reason.get("code"),
    )
    if not refs:
        return because
    ref = refs[-1] if explanation.template_code == "T_FOLLOWUP" else refs[0]
    kind, ident = ref["kind"], uuid.UUID(ref["id"])
    because.kind = kind
    if kind == "appointment":
        appointment = await db.get(Appointment, ident)
        if appointment is not None:
            because.slot_start = appointment.slot_start
            because.token_no = await db.scalar(
                select(QueueToken.token_no).where(QueueToken.appointment_id == ident)
            )
    elif kind == "queue_token":
        token = await db.get(QueueToken, ident)
        if token is not None:
            because.token_no = token.token_no
    elif kind == "lab_order":
        order = await db.get(LabOrder, ident)
        if order is not None:
            because.tests = [str(t.get("name") or t.get("code")) for t in order.tests]
    elif kind == "referral":
        referral = await db.get(Referral, ident)
        if referral is not None:
            because.from_name = await db.scalar(
                select(User.name).where(User.id == referral.from_user_id)
            )
    return because


async def access_log(
    db: AsyncSession,
    principal: ClinicPrincipal,
    patient_id: uuid.UUID,
    cursor: str | None,
    limit: int,
    req: RequestInfo,
    record: bool = True,
) -> tuple[list[schemas.AccessLogEntry], str | None]:
    if record:
        await record_and_explain(db, principal, patient_id, DEMOGRAPHICS, AccessAction.VIEW, req)
    patient = await db.get(Patient, patient_id)
    assert patient is not None
    limit = max(1, min(limit or DEFAULT_LIMIT, MAX_LIMIT))
    stmt = (
        select(AccessEvent, AccessExplanation, User.name, PatientAccessQuery.status)
        .join(User, User.id == AccessEvent.user_id)
        .outerjoin(AccessExplanation, AccessExplanation.access_event_id == AccessEvent.id)
        .outerjoin(
            PatientAccessQuery,
            and_(
                PatientAccessQuery.access_event_id == AccessEvent.id,
                PatientAccessQuery.patient_id == patient_id,
            ),
        )
        .where(AccessEvent.patient_id == patient_id)
    )
    if patient.user_id is not None:
        stmt = stmt.where(AccessEvent.user_id != patient.user_id)
    if cursor:
        raw_at, last_id = decode_cursor(cursor, 2)
        at = datetime.fromisoformat(raw_at)
        stmt = stmt.where(
            or_(
                AccessEvent.at < at,
                and_(AccessEvent.at == at, AccessEvent.id < uuid.UUID(last_id)),
            )
        )
    rows = (
        await db.execute(
            stmt.order_by(AccessEvent.at.desc(), AccessEvent.id.desc()).limit(limit + 1)
        )
    ).all()
    page, more = rows[:limit], len(rows) > limit
    items = [
        schemas.AccessLogEntry(
            id=event.id,
            at=event.at,
            user_name=name,
            role=event.role,
            resource=event.resource_type,
            action=event.action,
            decision=event.decision,
            break_glass=event.break_glass_id is not None,
            because=await _because(db, explanation),
            query_status=query_status,
        )
        for event, explanation, name, query_status in page
    ]
    next_cursor = (
        encode_cursor(page[-1][0].at.isoformat(), page[-1][0].id) if more and page else None
    )
    return items, next_cursor


async def raise_query(
    db: AsyncSession,
    principal: ClinicPrincipal,
    access_event_id: uuid.UUID,
    body: schemas.QueryIn,
    req: RequestInfo,
) -> PatientAccessQuery:
    event = await db.scalar(select(AccessEvent).where(AccessEvent.id == access_event_id))
    if event is None:
        raise ProblemError(404, "access-not-found")
    await record_and_explain(
        db,
        principal,
        event.patient_id,
        DEMOGRAPHICS,
        AccessAction.VIEW,
        req,
        detail={"op": "access.query", "access_event_id": str(event.id)},
    )
    existing = await db.scalar(
        select(PatientAccessQuery.id).where(PatientAccessQuery.access_event_id == event.id)
    )
    if existing is not None:
        raise ProblemError(409, "already-reported", "You already reported this access.")
    query = PatientAccessQuery(
        clinic_id=principal.clinic_id,
        patient_id=event.patient_id,
        access_event_id=event.id,
        message=body.message.strip(),
    )
    db.add(query)
    await db.flush()
    await db.refresh(query)
    return query


async def list_queries(
    db: AsyncSession, principal: ClinicPrincipal, open_only: bool
) -> list[PatientAccessQuery]:
    stmt = select(PatientAccessQuery).where(PatientAccessQuery.clinic_id == principal.clinic_id)
    if open_only:
        stmt = stmt.where(PatientAccessQuery.status == QueryStatus.OPEN)
    return list((await db.scalars(stmt.order_by(PatientAccessQuery.created_at.desc()))).all())


async def update_query(
    db: AsyncSession, query_id: uuid.UUID, body: schemas.QueryUpdate
) -> PatientAccessQuery:
    query = await db.get(PatientAccessQuery, query_id)
    if query is None:
        raise ProblemError(404, "query-not-found")
    query.status = body.status
    await db.flush()
    return query


# Export and portal sign-up ------------------------------------------------------------------------

EXPORT_NOTE = (
    "This file is a copy of your record at this clinic and the log of who opened it. "
    "Keep it private."
)


async def export(
    db: AsyncSession, principal: ClinicPrincipal, patient_id: uuid.UUID, req: RequestInfo
) -> schemas.PatientExport:
    record = await patient_chart(db, principal, patient_id, req, action=AccessAction.EXPORT)
    consents = await consent_history(db, principal, patient_id, req)
    entries: list[schemas.AccessLogEntry] = []
    cursor: str | None = None
    while True:
        page, cursor = await access_log(
            db, principal, patient_id, cursor, MAX_LIMIT, req, record=False
        )
        entries.extend(page)
        if cursor is None:
            break
    return schemas.PatientExport(
        exported_at=await db_now(db),
        record=record,
        consents=consents,
        access_log=entries,
        note=EXPORT_NOTE,
    )


async def grant_portal_access(
    db: AsyncSession,
    principal: ClinicPrincipal,
    patient_id: uuid.UUID,
    body: schemas.PortalAccessIn,
    req: RequestInfo,
) -> Patient:
    await record_and_explain(
        db,
        principal,
        patient_id,
        DEMOGRAPHICS,
        AccessAction.EDIT,
        req,
        detail={"op": "portal.invite"},
    )
    patient = await db.get(Patient, patient_id)
    assert patient is not None
    email = body.email.strip().lower()
    user = await db.scalar(select(User).where(func.lower(User.email) == email))
    if user is None:
        user = User(email=email, name=patient.name)
        db.add(user)
        await db.flush()
    if patient.user_id is not None and patient.user_id != user.id:
        raise ProblemError(409, "already-linked", "This record is linked to another account.")
    other = await db.scalar(
        select(Patient.id).where(
            Patient.clinic_id == principal.clinic_id,
            Patient.user_id == user.id,
            Patient.id != patient.id,
        )
    )
    if other is not None:
        raise ProblemError(409, "email-in-use", "This email is already linked to another patient.")
    patient.user_id = user.id
    membership = await db.scalar(
        select(Membership).where(
            Membership.clinic_id == principal.clinic_id,
            Membership.user_id == user.id,
            Membership.role == Role.PATIENT,
        )
    )
    if membership is None:
        db.add(Membership(clinic_id=principal.clinic_id, user_id=user.id, role=Role.PATIENT))
    await db.flush()
    await send_email(
        Email(
            to=email,
            subject="Your HealthSaathi patient account",
            text=(
                "You can now see your records, prescriptions, lab results and who opened your "
                f"record. Sign in with this email at {get_settings().public_app_url}/patient/login"
            ),
        )
    )
    return patient
