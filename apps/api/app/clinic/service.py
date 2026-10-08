"""Clinic operations: patients, schedules, shifts, appointments, queue tokens, care team.

Every function that reads or writes patient data records the access through
`access.record_and_explain()` in the same transaction.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.service import RequestInfo, clinic_timezone, record_and_explain, record_many
from app.clinic import schemas
from app.core.errors import ProblemError
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor
from app.db.enums import (
    STAFF_ROLES,
    AccessAction,
    AppointmentKind,
    AppointmentSource,
    AppointmentStatus,
    QueueStatus,
    RecordStatus,
    ResourceType,
    Role,
    ShiftStatus,
)
from app.db.models import (
    Appointment,
    CareTeamAssignment,
    Membership,
    Patient,
    QueueToken,
    Schedule,
    Shift,
    User,
)
from app.identity.deps import ClinicPrincipal
from app.ledger.writer import lock_clinic
from app.realtime.hub import notify

DEMOGRAPHICS = ResourceType.DEMOGRAPHICS
DEFAULT_SLOT = timedelta(minutes=15)

APPOINTMENT_TRANSITIONS: dict[AppointmentStatus, frozenset[AppointmentStatus]] = {
    AppointmentStatus.BOOKED: frozenset(
        {AppointmentStatus.CHECKED_IN, AppointmentStatus.CANCELLED, AppointmentStatus.NO_SHOW}
    ),
    AppointmentStatus.CHECKED_IN: frozenset(
        {AppointmentStatus.IN_CONSULT, AppointmentStatus.CANCELLED, AppointmentStatus.NO_SHOW}
    ),
    AppointmentStatus.IN_CONSULT: frozenset({AppointmentStatus.COMPLETED}),
    AppointmentStatus.COMPLETED: frozenset(),
    AppointmentStatus.CANCELLED: frozenset(),
    AppointmentStatus.NO_SHOW: frozenset(),
}

QUEUE_TRANSITIONS: dict[QueueStatus, frozenset[QueueStatus]] = {
    QueueStatus.WAITING: frozenset(
        {QueueStatus.WITH_NURSE, QueueStatus.WITH_DOCTOR, QueueStatus.SKIPPED}
    ),
    QueueStatus.WITH_NURSE: frozenset({QueueStatus.WITH_DOCTOR, QueueStatus.WAITING}),
    QueueStatus.WITH_DOCTOR: frozenset({QueueStatus.DONE}),
    QueueStatus.SKIPPED: frozenset({QueueStatus.WAITING}),
    QueueStatus.DONE: frozenset(),
}

# A token status change also moves the appointment along.
QUEUE_TO_APPOINTMENT = {
    QueueStatus.WITH_DOCTOR: AppointmentStatus.IN_CONSULT,
    QueueStatus.DONE: AppointmentStatus.COMPLETED,
}


def _now() -> datetime:
    return datetime.now(UTC)


async def _touch(
    db: AsyncSession,
    principal: ClinicPrincipal,
    patient_id: uuid.UUID,
    action: AccessAction,
    req: RequestInfo,
) -> None:
    """Record a demographics access for a workflow change that shows or edits a patient."""
    await record_and_explain(db, principal, patient_id, DEMOGRAPHICS, action, req)


async def local_today(db: AsyncSession, clinic_id: uuid.UUID) -> date:
    return _now().astimezone(ZoneInfo(await clinic_timezone(db, clinic_id))).date()


async def require_member(
    db: AsyncSession, clinic_id: uuid.UUID, user_id: uuid.UUID, roles: frozenset[Role]
) -> None:
    found = await db.scalar(
        select(Membership.id).where(
            Membership.clinic_id == clinic_id,
            Membership.user_id == user_id,
            Membership.role.in_(roles),
            Membership.is_active.is_(True),
        )
    )
    if found is None:
        names = ", ".join(sorted(r.value for r in roles))
        raise ProblemError(422, "not-a-member", f"That user is not an active {names} here.")


async def staff_directory(db: AsyncSession, clinic_id: uuid.UUID) -> list[schemas.StaffMember]:
    rows = await db.execute(
        select(User.id, User.name, Membership.role)
        .join(Membership, Membership.user_id == User.id)
        .where(
            Membership.clinic_id == clinic_id,
            Membership.is_active.is_(True),
            Membership.role.in_(STAFF_ROLES),
        )
        .order_by(Membership.role, User.name)
    )
    return [schemas.StaffMember(user_id=r.id, name=r.name, role=r.role) for r in rows]


# Patients -----------------------------------------------------------------------------------------


async def _next_mrn(db: AsyncSession, clinic_id: uuid.UUID) -> str:
    await lock_clinic(db, "mrn", clinic_id)
    count = await db.scalar(
        select(func.count()).select_from(Patient).where(Patient.clinic_id == clinic_id)
    )
    n = (count or 0) + 1
    while True:
        mrn = f"P{n:06d}"
        taken = await db.scalar(
            select(Patient.id).where(Patient.clinic_id == clinic_id, Patient.mrn == mrn)
        )
        if taken is None:
            return mrn
        n += 1


async def create_patient(
    db: AsyncSession, principal: ClinicPrincipal, body: schemas.PatientCreate, req: RequestInfo
) -> Patient:
    patient = Patient(
        clinic_id=principal.clinic_id,
        mrn=await _next_mrn(db, principal.clinic_id),
        created_by=principal.user_id,
        **body.model_dump(),
    )
    db.add(patient)
    await db.flush()
    await db.refresh(patient)
    await record_and_explain(db, principal, patient.id, DEMOGRAPHICS, AccessAction.CREATE, req)
    return patient


async def get_patient(
    db: AsyncSession, principal: ClinicPrincipal, patient_id: uuid.UUID, req: RequestInfo
) -> Patient:
    await record_and_explain(db, principal, patient_id, DEMOGRAPHICS, AccessAction.VIEW, req)
    patient = await db.get(Patient, patient_id)
    assert patient is not None
    return patient


async def update_patient(
    db: AsyncSession,
    principal: ClinicPrincipal,
    patient_id: uuid.UUID,
    body: schemas.PatientUpdate,
    req: RequestInfo,
) -> Patient:
    await record_and_explain(db, principal, patient_id, DEMOGRAPHICS, AccessAction.EDIT, req)
    patient = await db.get(Patient, patient_id)
    assert patient is not None
    for key, value in body.model_dump(exclude_unset=True).items():
        if value is None and key in ("name", "sex"):
            raise ProblemError(422, "required-field", f"{key} cannot be empty.")
        setattr(patient, key, value)
    await db.flush()
    return patient


async def search_patients(
    db: AsyncSession,
    principal: ClinicPrincipal,
    query: str | None,
    cursor: str | None,
    limit: int,
    req: RequestInfo,
) -> tuple[list[Patient], str | None]:
    limit = max(1, min(limit or DEFAULT_LIMIT, MAX_LIMIT))
    stmt: Select[Patient] = select(Patient).where(Patient.clinic_id == principal.clinic_id)
    if query:
        term = query.strip()
        like = f"%{term.replace('%', '').replace('_', '')}%"
        stmt = stmt.where(
            or_(Patient.name.ilike(like), Patient.mrn == term.upper(), Patient.phone.ilike(like))
        )
    if cursor:
        name, last_id = decode_cursor(cursor, 2)
        stmt = stmt.where(
            or_(Patient.name > name, and_(Patient.name == name, Patient.id > uuid.UUID(last_id)))
        )
    rows = list((await db.scalars(stmt.order_by(Patient.name, Patient.id).limit(limit + 1))).all())
    page, more = rows[:limit], len(rows) > limit
    await record_many(db, principal, [p.id for p in page], DEMOGRAPHICS, AccessAction.VIEW, req)
    next_cursor = encode_cursor(page[-1].name, page[-1].id) if more and page else None
    return page, next_cursor


# Schedules and shifts -----------------------------------------------------------------------------


async def list_schedules(
    db: AsyncSession, clinic_id: uuid.UUID, doctor_user_id: uuid.UUID | None
) -> Sequence[Schedule]:
    stmt = select(Schedule).where(Schedule.clinic_id == clinic_id)
    if doctor_user_id:
        stmt = stmt.where(Schedule.doctor_user_id == doctor_user_id)
    return (await db.scalars(stmt.order_by(Schedule.weekday, Schedule.start_time))).all()


async def create_schedule(
    db: AsyncSession, principal: ClinicPrincipal, body: schemas.ScheduleCreate
) -> Schedule:
    await require_member(db, principal.clinic_id, body.doctor_user_id, frozenset({Role.DOCTOR}))
    schedule = Schedule(
        clinic_id=principal.clinic_id, created_by=principal.user_id, **body.model_dump()
    )
    db.add(schedule)
    await db.flush()
    await db.refresh(schedule)
    return schedule


async def update_schedule(
    db: AsyncSession,
    principal: ClinicPrincipal,
    schedule_id: uuid.UUID,
    body: schemas.ScheduleUpdate,
) -> Schedule:
    schedule = await db.get(Schedule, schedule_id)
    if schedule is None:
        raise ProblemError(404, "schedule-not-found")
    schedule.status = body.status
    await db.flush()
    return schedule


async def list_shifts(
    db: AsyncSession,
    clinic_id: uuid.UUID,
    start: datetime,
    end: datetime,
    user_id: uuid.UUID | None,
) -> Sequence[Shift]:
    stmt = select(Shift).where(
        Shift.clinic_id == clinic_id, Shift.starts_at < end, Shift.ends_at > start
    )
    if user_id:
        stmt = stmt.where(Shift.user_id == user_id)
    return (await db.scalars(stmt.order_by(Shift.starts_at))).all()


async def create_shift(
    db: AsyncSession, principal: ClinicPrincipal, body: schemas.ShiftCreate
) -> Shift:
    await require_member(db, principal.clinic_id, body.user_id, frozenset({body.role}))
    shift = Shift(clinic_id=principal.clinic_id, created_by=principal.user_id, **body.model_dump())
    db.add(shift)
    await db.flush()
    await db.refresh(shift)
    return shift


async def cancel_shift(db: AsyncSession, shift_id: uuid.UUID) -> Shift:
    shift = await db.get(Shift, shift_id)
    if shift is None:
        raise ProblemError(404, "shift-not-found")
    shift.status = ShiftStatus.CANCELLED
    await db.flush()
    return shift


# Appointments -------------------------------------------------------------------------------------


async def _slot_length(
    db: AsyncSession, clinic_id: uuid.UUID, doctor_id: uuid.UUID, start: datetime, tz: str
) -> timedelta:
    local = start.astimezone(ZoneInfo(tz))
    minutes = await db.scalar(
        select(Schedule.slot_minutes).where(
            Schedule.clinic_id == clinic_id,
            Schedule.doctor_user_id == doctor_id,
            Schedule.weekday == local.weekday(),
            Schedule.start_time <= local.time(),
            Schedule.end_time > local.time(),
            Schedule.status == RecordStatus.ACTIVE,
        )
    )
    return timedelta(minutes=minutes) if minutes else DEFAULT_SLOT


async def _check_free(
    db: AsyncSession, clinic_id: uuid.UUID, doctor_id: uuid.UUID, start: datetime, end: datetime
) -> None:
    clash = await db.scalar(
        select(Appointment.id).where(
            Appointment.clinic_id == clinic_id,
            Appointment.doctor_user_id == doctor_id,
            Appointment.status.not_in([AppointmentStatus.CANCELLED, AppointmentStatus.NO_SHOW]),
            Appointment.kind != AppointmentKind.WALKIN,
            Appointment.slot_start < end,
            Appointment.slot_end > start,
        )
    )
    if clash is not None:
        raise ProblemError(409, "slot-taken", "The doctor already has an appointment then.")


def _source_for(role: Role) -> AppointmentSource:
    return {
        Role.DOCTOR: AppointmentSource.DOCTOR,
        Role.PATIENT: AppointmentSource.PATIENT,
    }.get(role, AppointmentSource.RECEPTION)


async def create_appointment(
    db: AsyncSession, principal: ClinicPrincipal, body: schemas.AppointmentCreate, req: RequestInfo
) -> Appointment:
    if body.kind == AppointmentKind.WALKIN:
        raise ProblemError(422, "use-walkin-endpoint")
    # Booking shows who the patient is but does not change their record.
    await _touch(db, principal, body.patient_id, AccessAction.VIEW, req)
    await require_member(db, principal.clinic_id, body.doctor_user_id, frozenset({Role.DOCTOR}))
    tz = await clinic_timezone(db, principal.clinic_id)
    start = body.slot_start
    if start.tzinfo is None:
        raise ProblemError(422, "timezone-required", "slot_start must include a time zone.")
    end = body.slot_end or start + await _slot_length(
        db, principal.clinic_id, body.doctor_user_id, start, tz
    )
    if end <= start:
        raise ProblemError(422, "bad-slot", "slot_end must be after slot_start.")
    await lock_clinic(db, f"doctor-slots:{body.doctor_user_id}", principal.clinic_id)
    await _check_free(db, principal.clinic_id, body.doctor_user_id, start, end)
    appointment = Appointment(
        clinic_id=principal.clinic_id,
        patient_id=body.patient_id,
        doctor_user_id=body.doctor_user_id,
        slot_start=start,
        slot_end=end,
        kind=body.kind,
        source=_source_for(principal.role),
        created_by=principal.user_id,
    )
    db.add(appointment)
    await db.flush()
    await db.refresh(appointment)
    await notify(db, principal.clinic_id, "appointments.changed", appointment_id=appointment.id)
    return appointment


async def list_appointments(
    db: AsyncSession,
    principal: ClinicPrincipal,
    day: date | None,
    doctor_user_id: uuid.UUID | None,
    req: RequestInfo,
) -> list[tuple[Appointment, Patient]]:
    tz = ZoneInfo(await clinic_timezone(db, principal.clinic_id))
    day = day or await local_today(db, principal.clinic_id)
    start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    stmt = (
        select(Appointment, Patient)
        .join(
            Patient,
            and_(Patient.id == Appointment.patient_id, Patient.clinic_id == Appointment.clinic_id),
        )
        .where(
            Appointment.clinic_id == principal.clinic_id,
            Appointment.slot_start >= start,
            Appointment.slot_start < start + timedelta(days=1),
        )
        .order_by(Appointment.slot_start, Appointment.id)
    )
    if doctor_user_id:
        stmt = stmt.where(Appointment.doctor_user_id == doctor_user_id)
    rows = [(a, p) for a, p in (await db.execute(stmt)).all()]
    await record_many(db, principal, [p.id for _, p in rows], DEMOGRAPHICS, AccessAction.VIEW, req)
    return rows


async def _set_appointment_status(appointment: Appointment, status: AppointmentStatus) -> None:
    if status == appointment.status:
        return
    if status not in APPOINTMENT_TRANSITIONS[appointment.status]:
        raise ProblemError(
            409,
            "invalid-transition",
            f"Cannot change an appointment from {appointment.status.value} to {status.value}.",
        )
    appointment.status = status


async def update_appointment(
    db: AsyncSession,
    principal: ClinicPrincipal,
    appointment_id: uuid.UUID,
    body: schemas.AppointmentUpdate,
    req: RequestInfo,
) -> Appointment:
    appointment = await db.get(Appointment, appointment_id)
    if appointment is None:
        raise ProblemError(404, "appointment-not-found")
    await record_and_explain(
        db, principal, appointment.patient_id, DEMOGRAPHICS, AccessAction.EDIT, req
    )
    await _set_appointment_status(appointment, body.status)
    if body.status in (AppointmentStatus.CANCELLED, AppointmentStatus.NO_SHOW):
        token = await db.scalar(
            select(QueueToken).where(
                QueueToken.clinic_id == principal.clinic_id,
                QueueToken.appointment_id == appointment.id,
            )
        )
        if token is not None and token.status == QueueStatus.WAITING:
            token.status = QueueStatus.SKIPPED
    await db.flush()
    await notify(db, principal.clinic_id, "appointments.changed", appointment_id=appointment.id)
    await notify(db, principal.clinic_id, "queue.changed")
    return appointment


# Queue --------------------------------------------------------------------------------------------


async def _issue_token(
    db: AsyncSession, principal: ClinicPrincipal, appointment: Appointment
) -> QueueToken:
    existing = await db.scalar(
        select(QueueToken).where(
            QueueToken.clinic_id == principal.clinic_id,
            QueueToken.appointment_id == appointment.id,
        )
    )
    if existing is not None:
        raise ProblemError(409, "token-exists", "This appointment already has a token.")
    if appointment.status not in (AppointmentStatus.BOOKED, AppointmentStatus.CHECKED_IN):
        raise ProblemError(409, "invalid-transition", "Only booked appointments can get a token.")
    today = await local_today(db, principal.clinic_id)
    await lock_clinic(db, f"queue:{today.isoformat()}", principal.clinic_id)
    last = await db.scalar(
        select(func.max(QueueToken.token_no)).where(
            QueueToken.clinic_id == principal.clinic_id, QueueToken.day == today
        )
    )
    token = QueueToken(
        clinic_id=principal.clinic_id,
        appointment_id=appointment.id,
        token_no=(last or 0) + 1,
        day=today,
        created_by=principal.user_id,
    )
    db.add(token)
    appointment.status = AppointmentStatus.CHECKED_IN
    await db.flush()
    await db.refresh(token)
    await notify(db, principal.clinic_id, "queue.changed", token_id=token.id)
    return token


async def issue_token(
    db: AsyncSession, principal: ClinicPrincipal, body: schemas.TokenCreate, req: RequestInfo
) -> QueueToken:
    appointment = await db.get(Appointment, body.appointment_id)
    if appointment is None:
        raise ProblemError(404, "appointment-not-found")
    await record_and_explain(
        db, principal, appointment.patient_id, DEMOGRAPHICS, AccessAction.EDIT, req
    )
    return await _issue_token(db, principal, appointment)


async def create_walkin(
    db: AsyncSession, principal: ClinicPrincipal, body: schemas.WalkInCreate, req: RequestInfo
) -> tuple[Appointment, QueueToken]:
    await _touch(db, principal, body.patient_id, AccessAction.VIEW, req)
    await require_member(db, principal.clinic_id, body.doctor_user_id, frozenset({Role.DOCTOR}))
    now = _now()
    appointment = Appointment(
        clinic_id=principal.clinic_id,
        patient_id=body.patient_id,
        doctor_user_id=body.doctor_user_id,
        slot_start=now,
        slot_end=now + DEFAULT_SLOT,
        kind=AppointmentKind.WALKIN,
        source=AppointmentSource.RECEPTION,
        created_by=principal.user_id,
    )
    db.add(appointment)
    await db.flush()
    await db.refresh(appointment)
    token = await _issue_token(db, principal, appointment)
    await notify(db, principal.clinic_id, "appointments.changed", appointment_id=appointment.id)
    return appointment, token


async def queue_for_day(
    db: AsyncSession,
    principal: ClinicPrincipal,
    day: date | None,
    doctor_user_id: uuid.UUID | None,
    req: RequestInfo,
) -> list[tuple[QueueToken, Appointment, Patient]]:
    day = day or await local_today(db, principal.clinic_id)
    stmt = (
        select(QueueToken, Appointment, Patient)
        .join(
            Appointment,
            and_(
                Appointment.id == QueueToken.appointment_id,
                Appointment.clinic_id == QueueToken.clinic_id,
            ),
        )
        .join(
            Patient,
            and_(Patient.id == Appointment.patient_id, Patient.clinic_id == Appointment.clinic_id),
        )
        .where(QueueToken.clinic_id == principal.clinic_id, QueueToken.day == day)
        .order_by(QueueToken.token_no)
    )
    if doctor_user_id:
        stmt = stmt.where(Appointment.doctor_user_id == doctor_user_id)
    rows = [(t, a, p) for t, a, p in (await db.execute(stmt)).all()]
    await record_many(
        db, principal, [p.id for _, _, p in rows], DEMOGRAPHICS, AccessAction.VIEW, req
    )
    return rows


async def update_token(
    db: AsyncSession,
    principal: ClinicPrincipal,
    token_id: uuid.UUID,
    body: schemas.TokenUpdate,
    req: RequestInfo,
) -> QueueToken:
    token = await db.get(QueueToken, token_id)
    if token is None:
        raise ProblemError(404, "token-not-found")
    appointment = await db.get(Appointment, token.appointment_id)
    assert appointment is not None
    # Moving a token along the queue shows the patient's name but does not change their record.
    await record_and_explain(
        db, principal, appointment.patient_id, DEMOGRAPHICS, AccessAction.VIEW, req
    )

    if body.status is not None and body.status != token.status:
        if body.status not in QUEUE_TRANSITIONS[token.status]:
            raise ProblemError(
                409,
                "invalid-transition",
                f"Cannot change a token from {token.status.value} to {body.status.value}.",
            )
        token.status = body.status
        follow = QUEUE_TO_APPOINTMENT.get(body.status)
        if follow is not None:
            await _set_appointment_status(appointment, follow)
    if body.unassign_nurse:
        token.assigned_nurse_user_id = None
    elif body.assigned_nurse_user_id is not None:
        await require_member(
            db, principal.clinic_id, body.assigned_nurse_user_id, frozenset({Role.NURSE})
        )
        token.assigned_nurse_user_id = body.assigned_nurse_user_id
    await db.flush()
    await notify(db, principal.clinic_id, "queue.changed", token_id=token.id)
    return token


# Care team ----------------------------------------------------------------------------------------


async def add_care_team_member(
    db: AsyncSession, principal: ClinicPrincipal, body: schemas.CareTeamCreate, req: RequestInfo
) -> CareTeamAssignment:
    await record_and_explain(db, principal, body.patient_id, DEMOGRAPHICS, AccessAction.EDIT, req)
    if body.role not in STAFF_ROLES:
        raise ProblemError(422, "not-a-staff-role")
    await require_member(db, principal.clinic_id, body.user_id, frozenset({body.role}))
    starts = body.starts_at or _now()
    if body.ends_at is not None and body.ends_at <= starts:
        raise ProblemError(422, "bad-window", "ends_at must be after starts_at.")
    assignment = CareTeamAssignment(
        clinic_id=principal.clinic_id,
        patient_id=body.patient_id,
        user_id=body.user_id,
        role=body.role,
        starts_at=starts,
        ends_at=body.ends_at,
        created_by=principal.user_id,
    )
    db.add(assignment)
    await db.flush()
    await db.refresh(assignment)
    return assignment


async def list_care_team(
    db: AsyncSession, principal: ClinicPrincipal, patient_id: uuid.UUID, req: RequestInfo
) -> Sequence[CareTeamAssignment]:
    await record_and_explain(db, principal, patient_id, DEMOGRAPHICS, AccessAction.VIEW, req)
    return (
        await db.scalars(
            select(CareTeamAssignment)
            .where(
                CareTeamAssignment.clinic_id == principal.clinic_id,
                CareTeamAssignment.patient_id == patient_id,
            )
            .order_by(CareTeamAssignment.starts_at)
        )
    ).all()


async def end_care_team_member(
    db: AsyncSession, principal: ClinicPrincipal, assignment_id: uuid.UUID, req: RequestInfo
) -> CareTeamAssignment:
    assignment = await db.get(CareTeamAssignment, assignment_id)
    if assignment is None:
        raise ProblemError(404, "assignment-not-found")
    await record_and_explain(
        db, principal, assignment.patient_id, DEMOGRAPHICS, AccessAction.EDIT, req
    )
    assignment.status = RecordStatus.INACTIVE
    assignment.ends_at = assignment.ends_at or _now()
    await db.flush()
    return assignment
