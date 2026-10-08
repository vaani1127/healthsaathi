import uuid
from datetime import date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status

from app.access.deps import Db, ReqInfo
from app.clinic import schemas, service
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, Page
from app.core.ratelimit import SEARCH, limiter
from app.db.enums import STAFF_ROLES, Role
from app.identity.deps import ClinicPrincipal, require_roles

router = APIRouter(tags=["clinic"])

R = Role
ANY_STAFF = tuple(STAFF_ROLES - {R.PLATFORM_ADMIN})


def _only(*allowed: Role) -> Any:
    return Depends(require_roles(*allowed))


Staff = Annotated[ClinicPrincipal, _only(*ANY_STAFF)]
StaffOrPatient = Annotated[ClinicPrincipal, _only(*ANY_STAFF, R.PATIENT)]
Admin = Annotated[ClinicPrincipal, _only(R.CLINIC_ADMIN)]
Reception = Annotated[ClinicPrincipal, _only(R.RECEPTION)]
FrontDesk = Annotated[ClinicPrincipal, _only(R.RECEPTION, R.CLINIC_ADMIN)]
Searchers = Annotated[
    ClinicPrincipal, _only(R.RECEPTION, R.NURSE, R.DOCTOR, R.LAB_TECH, R.CLINIC_ADMIN)
]
PatientEditors = Annotated[ClinicPrincipal, _only(R.RECEPTION, R.CLINIC_ADMIN, R.DOCTOR)]
Bookers = Annotated[ClinicPrincipal, _only(R.RECEPTION, R.DOCTOR, R.CLINIC_ADMIN)]
QueueViewers = Annotated[ClinicPrincipal, _only(R.RECEPTION, R.NURSE, R.DOCTOR, R.CLINIC_ADMIN)]
QueueWorkers = Annotated[ClinicPrincipal, _only(R.RECEPTION, R.NURSE, R.DOCTOR)]
CareTeamEditors = Annotated[ClinicPrincipal, _only(R.CLINIC_ADMIN, R.DOCTOR)]


# Patients -----------------------------------------------------------------------------------------


@router.post("/patients", response_model=schemas.PatientOut, status_code=status.HTTP_201_CREATED)
async def create_patient(
    principal: FrontDesk,
    body: schemas.PatientCreate,
    db: Db,
    req: ReqInfo,
) -> schemas.PatientOut:
    patient = await service.create_patient(db, principal, body, req)
    return schemas.PatientOut.model_validate(patient)


@router.get("/patients", response_model=Page[schemas.PatientSummary])
@limiter.limit(SEARCH)
async def search_patients(
    request: Request,
    principal: Searchers,
    db: Db,
    req: ReqInfo,
    q: Annotated[str | None, Query(max_length=60)] = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> Page[schemas.PatientSummary]:
    patients, next_cursor = await service.search_patients(db, principal, q, cursor, limit, req)
    return Page(
        items=[schemas.PatientSummary.model_validate(p) for p in patients], next_cursor=next_cursor
    )


@router.get("/patients/{patient_id}", response_model=schemas.PatientOut)
async def get_patient(
    principal: StaffOrPatient,
    patient_id: uuid.UUID,
    db: Db,
    req: ReqInfo,
) -> schemas.PatientOut:
    return schemas.PatientOut.model_validate(
        await service.get_patient(db, principal, patient_id, req)
    )


@router.patch("/patients/{patient_id}", response_model=schemas.PatientOut)
async def update_patient(
    principal: PatientEditors,
    patient_id: uuid.UUID,
    body: schemas.PatientUpdate,
    db: Db,
    req: ReqInfo,
) -> schemas.PatientOut:
    patient = await service.update_patient(db, principal, patient_id, body, req)
    return schemas.PatientOut.model_validate(patient)


@router.get("/patients/{patient_id}/care-team", response_model=list[schemas.CareTeamOut])
async def list_care_team(
    principal: Staff,
    patient_id: uuid.UUID,
    db: Db,
    req: ReqInfo,
) -> list[schemas.CareTeamOut]:
    rows = await service.list_care_team(db, principal, patient_id, req)
    return [schemas.CareTeamOut.model_validate(r) for r in rows]


# Staff, schedules, shifts -------------------------------------------------------------------------


@router.get("/staff", response_model=list[schemas.StaffMember])
async def staff_directory(principal: Staff, db: Db) -> list[schemas.StaffMember]:
    return await service.staff_directory(db, principal.clinic_id)


@router.get("/schedules", response_model=list[schemas.ScheduleOut])
async def list_schedules(
    principal: Staff,
    db: Db,
    doctor_user_id: uuid.UUID | None = None,
) -> list[schemas.ScheduleOut]:
    rows = await service.list_schedules(db, principal.clinic_id, doctor_user_id)
    return [schemas.ScheduleOut.model_validate(r) for r in rows]


@router.post("/schedules", response_model=schemas.ScheduleOut, status_code=status.HTTP_201_CREATED)
async def create_schedule(
    principal: Admin,
    body: schemas.ScheduleCreate,
    db: Db,
) -> schemas.ScheduleOut:
    return schemas.ScheduleOut.model_validate(await service.create_schedule(db, principal, body))


@router.patch("/schedules/{schedule_id}", response_model=schemas.ScheduleOut)
async def update_schedule(
    principal: Admin,
    schedule_id: uuid.UUID,
    body: schemas.ScheduleUpdate,
    db: Db,
) -> schemas.ScheduleOut:
    schedule = await service.update_schedule(db, principal, schedule_id, body)
    return schemas.ScheduleOut.model_validate(schedule)


@router.get("/shifts", response_model=list[schemas.ShiftOut])
async def list_shifts(
    principal: Staff,
    db: Db,
    start: datetime,
    end: datetime | None = None,
    user_id: uuid.UUID | None = None,
) -> list[schemas.ShiftOut]:
    rows = await service.list_shifts(
        db, principal.clinic_id, start, end or start + timedelta(days=1), user_id
    )
    return [schemas.ShiftOut.model_validate(r) for r in rows]


@router.post("/shifts", response_model=schemas.ShiftOut, status_code=status.HTTP_201_CREATED)
async def create_shift(
    principal: Admin,
    body: schemas.ShiftCreate,
    db: Db,
) -> schemas.ShiftOut:
    return schemas.ShiftOut.model_validate(await service.create_shift(db, principal, body))


@router.post("/shifts/{shift_id}/cancel", response_model=schemas.ShiftOut)
async def cancel_shift(principal: Admin, shift_id: uuid.UUID, db: Db) -> schemas.ShiftOut:
    return schemas.ShiftOut.model_validate(await service.cancel_shift(db, shift_id))


# Appointments -------------------------------------------------------------------------------------


@router.post(
    "/appointments", response_model=schemas.AppointmentOut, status_code=status.HTTP_201_CREATED
)
async def create_appointment(
    principal: Bookers,
    body: schemas.AppointmentCreate,
    db: Db,
    req: ReqInfo,
) -> schemas.AppointmentOut:
    appointment = await service.create_appointment(db, principal, body, req)
    return schemas.AppointmentOut.model_validate(appointment)


@router.get("/appointments", response_model=list[schemas.AppointmentListItem])
async def list_appointments(
    principal: QueueViewers,
    db: Db,
    req: ReqInfo,
    day: date | None = None,
    doctor_user_id: uuid.UUID | None = None,
) -> list[schemas.AppointmentListItem]:
    rows = await service.list_appointments(db, principal, day, doctor_user_id, req)
    return [
        schemas.AppointmentListItem(
            **schemas.AppointmentOut.model_validate(a).model_dump(),
            patient=schemas.PatientSummary.model_validate(p),
        )
        for a, p in rows
    ]


@router.patch("/appointments/{appointment_id}", response_model=schemas.AppointmentOut)
async def update_appointment(
    principal: Bookers,
    appointment_id: uuid.UUID,
    body: schemas.AppointmentUpdate,
    db: Db,
    req: ReqInfo,
) -> schemas.AppointmentOut:
    appointment = await service.update_appointment(db, principal, appointment_id, body, req)
    return schemas.AppointmentOut.model_validate(appointment)


@router.post("/walkins", response_model=schemas.WalkInOut, status_code=status.HTTP_201_CREATED)
async def create_walkin(
    principal: Reception,
    body: schemas.WalkInCreate,
    db: Db,
    req: ReqInfo,
) -> schemas.WalkInOut:
    appointment, token = await service.create_walkin(db, principal, body, req)
    return schemas.WalkInOut(
        appointment=schemas.AppointmentOut.model_validate(appointment),
        token=schemas.TokenOut.model_validate(token),
    )


# Queue --------------------------------------------------------------------------------------------


@router.post("/queue/tokens", response_model=schemas.TokenOut, status_code=status.HTTP_201_CREATED)
async def issue_token(
    principal: Reception,
    body: schemas.TokenCreate,
    db: Db,
    req: ReqInfo,
) -> schemas.TokenOut:
    return schemas.TokenOut.model_validate(await service.issue_token(db, principal, body, req))


@router.get("/queue", response_model=list[schemas.QueueItem])
async def queue(
    principal: QueueViewers,
    db: Db,
    req: ReqInfo,
    day: date | None = None,
    doctor_user_id: uuid.UUID | None = None,
) -> list[schemas.QueueItem]:
    rows = await service.queue_for_day(db, principal, day, doctor_user_id, req)
    return [
        schemas.QueueItem(
            **schemas.TokenOut.model_validate(t).model_dump(),
            doctor_user_id=a.doctor_user_id,
            patient=schemas.PatientSummary.model_validate(p),
        )
        for t, a, p in rows
    ]


@router.patch("/queue/tokens/{token_id}", response_model=schemas.TokenOut)
async def update_token(
    principal: QueueWorkers,
    token_id: uuid.UUID,
    body: schemas.TokenUpdate,
    db: Db,
    req: ReqInfo,
) -> schemas.TokenOut:
    return schemas.TokenOut.model_validate(
        await service.update_token(db, principal, token_id, body, req)
    )


# Care team ----------------------------------------------------------------------------------------


@router.post("/care-team", response_model=schemas.CareTeamOut, status_code=status.HTTP_201_CREATED)
async def add_care_team_member(
    principal: CareTeamEditors,
    body: schemas.CareTeamCreate,
    db: Db,
    req: ReqInfo,
) -> schemas.CareTeamOut:
    assignment = await service.add_care_team_member(db, principal, body, req)
    return schemas.CareTeamOut.model_validate(assignment)


@router.post("/care-team/{assignment_id}/end", response_model=schemas.CareTeamOut)
async def end_care_team_member(
    principal: CareTeamEditors,
    assignment_id: uuid.UUID,
    db: Db,
    req: ReqInfo,
) -> schemas.CareTeamOut:
    assignment = await service.end_care_team_member(db, principal, assignment_id, req)
    return schemas.CareTeamOut.model_validate(assignment)
