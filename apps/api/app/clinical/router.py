import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from fastapi.responses import Response

from app.access.deps import Db, ReqInfo
from app.chart.schemas import (
    AllergyOut,
    ConditionOut,
    LabOrderOut,
    NoteOut,
    PrescriptionOut,
    VitalOut,
)
from app.clinic.schemas import AppointmentOut
from app.clinical import schemas, service
from app.core.errors import ProblemError
from app.core.ratelimit import EXPORT_PRINT, limiter
from app.db.enums import Role
from app.identity.deps import ClinicPrincipal, require_roles

router = APIRouter(tags=["clinical"])
R = Role


def _only(*allowed: Role) -> Any:
    return Depends(require_roles(*allowed))


Doctor = Annotated[ClinicPrincipal, _only(R.DOCTOR)]
Clinicians = Annotated[ClinicPrincipal, _only(R.DOCTOR, R.NURSE)]
LabTech = Annotated[ClinicPrincipal, _only(R.LAB_TECH)]
LabPeople = Annotated[ClinicPrincipal, _only(R.LAB_TECH, R.DOCTOR)]
DocumentReaders = Annotated[ClinicPrincipal, _only(R.DOCTOR, R.LAB_TECH, R.PATIENT)]


# Encounters ---------------------------------------------------------------------------------------


@router.post(
    "/appointments/{appointment_id}/encounter",
    response_model=schemas.EncounterOut,
    status_code=status.HTTP_201_CREATED,
)
async def start_encounter(
    principal: Doctor, appointment_id: uuid.UUID, db: Db, req: ReqInfo
) -> schemas.EncounterOut:
    encounter = await service.start_encounter(db, principal, appointment_id, req)
    return schemas.EncounterOut.model_validate(encounter)


@router.post("/encounters/{encounter_id}/close", response_model=schemas.EncounterOut)
async def close_encounter(
    principal: Doctor, encounter_id: uuid.UUID, db: Db, req: ReqInfo
) -> schemas.EncounterOut:
    encounter = await service.close_encounter(db, principal, encounter_id, req)
    return schemas.EncounterOut.model_validate(encounter)


# Vitals, allergies, conditions --------------------------------------------------------------------


@router.post("/patients/{patient_id}/vitals", response_model=VitalOut, status_code=201)
async def add_vitals(
    principal: Clinicians, patient_id: uuid.UUID, body: schemas.VitalsIn, db: Db, req: ReqInfo
) -> VitalOut:
    return VitalOut.model_validate(await service.add_vitals(db, principal, patient_id, body, req))


@router.post("/patients/{patient_id}/allergies", response_model=AllergyOut, status_code=201)
async def add_allergy(
    principal: Clinicians, patient_id: uuid.UUID, body: schemas.AllergyIn, db: Db, req: ReqInfo
) -> AllergyOut:
    return AllergyOut.model_validate(
        await service.add_allergy(db, principal, patient_id, body, req)
    )


@router.patch("/allergies/{allergy_id}", response_model=AllergyOut)
async def update_allergy(
    principal: Clinicians,
    allergy_id: uuid.UUID,
    body: schemas.AllergyUpdate,
    db: Db,
    req: ReqInfo,
) -> AllergyOut:
    return AllergyOut.model_validate(
        await service.update_allergy(db, principal, allergy_id, body, req)
    )


@router.post("/patients/{patient_id}/conditions", response_model=ConditionOut, status_code=201)
async def add_condition(
    principal: Doctor, patient_id: uuid.UUID, body: schemas.ConditionIn, db: Db, req: ReqInfo
) -> ConditionOut:
    condition = await service.add_condition(db, principal, patient_id, body, req)
    return ConditionOut.model_validate(condition)


@router.patch("/conditions/{condition_id}", response_model=ConditionOut)
async def update_condition(
    principal: Doctor,
    condition_id: uuid.UUID,
    body: schemas.ConditionUpdate,
    db: Db,
    req: ReqInfo,
) -> ConditionOut:
    condition = await service.update_condition(db, principal, condition_id, body, req)
    return ConditionOut.model_validate(condition)


# Notes --------------------------------------------------------------------------------------------


@router.post("/patients/{patient_id}/notes", response_model=NoteOut, status_code=201)
async def create_note(
    principal: Doctor, patient_id: uuid.UUID, body: schemas.NoteIn, db: Db, req: ReqInfo
) -> NoteOut:
    return service.note_out(await service.create_note(db, principal, patient_id, body, req))


@router.patch("/notes/{note_id}", response_model=NoteOut)
async def edit_note(
    principal: Doctor, note_id: uuid.UUID, body: schemas.NoteEdit, db: Db, req: ReqInfo
) -> NoteOut:
    return service.note_out(await service.edit_note(db, principal, note_id, body, req))


@router.post("/notes/{note_id}/sign", response_model=NoteOut)
async def sign_note(principal: Doctor, note_id: uuid.UUID, db: Db, req: ReqInfo) -> NoteOut:
    return service.note_out(await service.sign_note(db, principal, note_id, req))


@router.post("/notes/{note_id}/versions", response_model=NoteOut, status_code=201)
async def new_note_version(
    principal: Doctor, note_id: uuid.UUID, body: schemas.NoteEdit, db: Db, req: ReqInfo
) -> NoteOut:
    return service.note_out(await service.new_note_version(db, principal, note_id, body, req))


@router.get("/notes/{note_id}/history", response_model=list[NoteOut])
async def note_history(
    principal: Doctor, note_id: uuid.UUID, db: Db, req: ReqInfo
) -> list[NoteOut]:
    return await service.note_history(db, principal, note_id, req)


# Prescriptions ------------------------------------------------------------------------------------


@router.post(
    "/patients/{patient_id}/prescriptions", response_model=PrescriptionOut, status_code=201
)
async def create_prescription(
    principal: Doctor,
    patient_id: uuid.UUID,
    body: schemas.PrescriptionIn,
    db: Db,
    req: ReqInfo,
) -> PrescriptionOut:
    rx = await service.create_prescription(db, principal, patient_id, body, req)
    return PrescriptionOut.model_validate(rx)


@router.patch("/prescriptions/{rx_id}", response_model=PrescriptionOut)
async def edit_prescription(
    principal: Doctor,
    rx_id: uuid.UUID,
    body: schemas.PrescriptionEdit,
    db: Db,
    req: ReqInfo,
) -> PrescriptionOut:
    rx = await service.edit_prescription(db, principal, rx_id, body, req)
    return PrescriptionOut.model_validate(rx)


@router.post("/prescriptions/{rx_id}/sign", response_model=PrescriptionOut)
async def sign_prescription(
    principal: Doctor, rx_id: uuid.UUID, db: Db, req: ReqInfo
) -> PrescriptionOut:
    return PrescriptionOut.model_validate(
        await service.sign_prescription(db, principal, rx_id, req)
    )


@router.post("/prescriptions/{rx_id}/versions", response_model=PrescriptionOut, status_code=201)
async def new_prescription_version(
    principal: Doctor,
    rx_id: uuid.UUID,
    body: schemas.PrescriptionEdit,
    db: Db,
    req: ReqInfo,
) -> PrescriptionOut:
    rx = await service.new_prescription_version(db, principal, rx_id, body, req)
    return PrescriptionOut.model_validate(rx)


@router.get("/prescriptions/{rx_id}/print", response_model=PrescriptionOut)
@limiter.limit(EXPORT_PRINT)
async def print_prescription(
    request: Request, principal: Doctor, rx_id: uuid.UUID, db: Db, req: ReqInfo
) -> PrescriptionOut:
    rx = await service.get_prescription_for_print(db, principal, rx_id, req)
    return PrescriptionOut.model_validate(rx)


# Lab ----------------------------------------------------------------------------------------------


@router.post("/patients/{patient_id}/lab-orders", response_model=LabOrderOut, status_code=201)
async def order_lab(
    principal: Doctor, patient_id: uuid.UUID, body: schemas.LabOrderIn, db: Db, req: ReqInfo
) -> LabOrderOut:
    order = await service.order_lab(db, principal, patient_id, body, req)
    return service.lab_order_out(order, None)


@router.patch("/lab-orders/{order_id}", response_model=LabOrderOut)
async def update_lab_order(
    principal: LabPeople,
    order_id: uuid.UUID,
    body: schemas.LabOrderUpdate,
    db: Db,
    req: ReqInfo,
) -> LabOrderOut:
    order = await service.update_lab_order(db, principal, order_id, body, req)
    return service.lab_order_out(order, None)


@router.get("/lab/worklist", response_model=list[schemas.WorklistItem])
async def lab_worklist(principal: LabTech, db: Db, req: ReqInfo) -> list[schemas.WorklistItem]:
    return await service.worklist(db, principal, req)


@router.post("/lab-orders/{order_id}/result", response_model=LabOrderOut, status_code=201)
async def record_result(
    principal: LabTech,
    order_id: uuid.UUID,
    db: Db,
    req: ReqInfo,
    values: Annotated[str, Form(description="JSON object of result values")],
    file: Annotated[UploadFile | None, File()] = None,
) -> LabOrderOut:
    try:
        parsed = schemas.result_values(values)
    except ValueError as exc:
        raise ProblemError(422, "bad-values", str(exc)) from exc
    upload = None
    if file is not None:
        upload = (await file.read(), file.content_type or "application/octet-stream")
    order, result = await service.record_result(db, principal, order_id, parsed, upload, req)
    return service.lab_order_out(order, result)


@router.post("/lab-orders/{order_id}/release", response_model=LabOrderOut)
async def release_result(
    principal: LabPeople, order_id: uuid.UUID, db: Db, req: ReqInfo
) -> LabOrderOut:
    order, result = await service.release_result(db, principal, order_id, req)
    return service.lab_order_out(order, result)


@router.get("/documents/{document_id}")
async def download_document(
    principal: DocumentReaders, document_id: uuid.UUID, db: Db, req: ReqInfo
) -> Response:
    document, data = await service.download_document(db, principal, document_id, req)
    extension = document.blob_path.rsplit(".", 1)[-1]
    media = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg"}.get(extension)
    return Response(
        data,
        media_type=media or "application/octet-stream",
        headers={
            "content-disposition": (
                f'attachment; filename="{document.kind}-{document.id}.{extension}"'
            ),
            "x-content-sha256": document.sha256,
        },
    )


# Referrals and follow-ups -------------------------------------------------------------------------


@router.post(
    "/patients/{patient_id}/referrals", response_model=schemas.ReferralOut, status_code=201
)
async def refer(
    principal: Doctor, patient_id: uuid.UUID, body: schemas.ReferralIn, db: Db, req: ReqInfo
) -> schemas.ReferralOut:
    return schemas.ReferralOut.model_validate(
        await service.refer(db, principal, patient_id, body, req)
    )


@router.patch("/referrals/{referral_id}", response_model=schemas.ReferralOut)
async def update_referral(
    principal: Doctor,
    referral_id: uuid.UUID,
    body: schemas.ReferralUpdate,
    db: Db,
    req: ReqInfo,
) -> schemas.ReferralOut:
    referral = await service.update_referral(db, principal, referral_id, body, req)
    return schemas.ReferralOut.model_validate(referral)


@router.post("/patients/{patient_id}/follow-ups", response_model=AppointmentOut, status_code=201)
async def book_follow_up(
    principal: Doctor, patient_id: uuid.UUID, body: schemas.FollowUpIn, db: Db, req: ReqInfo
) -> AppointmentOut:
    appointment = await service.book_follow_up(db, principal, patient_id, body, req)
    return AppointmentOut.model_validate(appointment)
