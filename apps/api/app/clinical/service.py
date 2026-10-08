"""Clinical records: encounters, vitals, allergies, conditions, notes, prescriptions, lab orders and
results, referrals and follow-ups.

Every operation records the access first (same transaction), so a refused access never writes.
Signed notes and prescriptions are never changed: edits create a new version, and a database
trigger rejects any update to a signed row (migration 0002).
"""

import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.service import RequestInfo, db_now, record_and_explain, record_many
from app.chart.schemas import LabOrderOut, LabResultOut, NoteOut
from app.clinic import schemas as clinic_schemas
from app.clinic import service as clinic_service
from app.clinical import schemas
from app.clinical.notes_crypto import decrypt_body, encrypt_body
from app.core.config import get_settings
from app.core.errors import ProblemError
from app.db.enums import (
    AccessAction,
    AppointmentKind,
    AppointmentStatus,
    EncounterStatus,
    LabOrderStatus,
    ReferralStatus,
    ResourceType,
    Role,
)
from app.db.models import (
    Allergy,
    Appointment,
    ClinicalNote,
    Condition,
    Document,
    Encounter,
    LabOrder,
    LabResult,
    Patient,
    Prescription,
    Referral,
    Vital,
)
from app.identity.deps import ClinicPrincipal
from app.realtime.hub import notify
from app.storage import get_storage, sha256_hex
from e2d_core.ids import uuid7

R = ResourceType
A = AccessAction

ALLOWED_UPLOADS = {"application/pdf": "pdf", "image/png": "png", "image/jpeg": "jpg"}


async def _record(
    db: AsyncSession,
    principal: ClinicPrincipal,
    patient_id: uuid.UUID,
    resource: ResourceType,
    action: AccessAction,
    req: RequestInfo,
    **detail: object,
) -> None:
    await record_and_explain(
        db,
        principal,
        patient_id,
        resource,
        action,
        req,
        detail={k: str(v) for k, v in detail.items()},
    )


async def _encounter_for(
    db: AsyncSession, encounter_id: uuid.UUID | None, patient_id: uuid.UUID
) -> Encounter | None:
    if encounter_id is None:
        return None
    encounter = await db.get(Encounter, encounter_id)
    if encounter is None or encounter.patient_id != patient_id:
        raise ProblemError(422, "encounter-not-found", "That consultation is not for this patient.")
    return encounter


# Encounters ---------------------------------------------------------------------------------------


async def start_encounter(
    db: AsyncSession, principal: ClinicPrincipal, appointment_id: uuid.UUID, req: RequestInfo
) -> Encounter:
    appointment = await db.get(Appointment, appointment_id)
    if appointment is None:
        raise ProblemError(404, "appointment-not-found")
    await _record(
        db, principal, appointment.patient_id, R.DEMOGRAPHICS, A.EDIT, req, op="encounter.start"
    )
    if appointment.doctor_user_id != principal.user_id:
        raise ProblemError(
            403, "not-your-appointment", "Only the appointment's doctor can start it."
        )
    existing = await db.scalar(
        select(Encounter).where(
            Encounter.appointment_id == appointment.id, Encounter.status == EncounterStatus.OPEN
        )
    )
    if existing is not None:
        return existing
    if appointment.status in (AppointmentStatus.CANCELLED, AppointmentStatus.NO_SHOW):
        raise ProblemError(409, "invalid-transition", "This appointment was cancelled.")
    encounter = Encounter(
        clinic_id=principal.clinic_id,
        appointment_id=appointment.id,
        patient_id=appointment.patient_id,
        doctor_user_id=principal.user_id,
        started_at=await db_now(db),
        created_by=principal.user_id,
    )
    db.add(encounter)
    appointment.status = AppointmentStatus.IN_CONSULT
    await db.flush()
    await db.refresh(encounter)
    await notify(db, principal.clinic_id, "appointments.changed", appointment_id=appointment.id)
    return encounter


async def close_encounter(
    db: AsyncSession, principal: ClinicPrincipal, encounter_id: uuid.UUID, req: RequestInfo
) -> Encounter:
    encounter = await db.get(Encounter, encounter_id)
    if encounter is None:
        raise ProblemError(404, "encounter-not-found")
    await _record(
        db, principal, encounter.patient_id, R.DEMOGRAPHICS, A.EDIT, req, op="encounter.close"
    )
    if encounter.status == EncounterStatus.CLOSED:
        return encounter
    encounter.status = EncounterStatus.CLOSED
    encounter.ended_at = await db_now(db)
    if encounter.appointment_id is not None:
        appointment = await db.get(Appointment, encounter.appointment_id)
        if appointment is not None and appointment.status == AppointmentStatus.IN_CONSULT:
            appointment.status = AppointmentStatus.COMPLETED
    await db.flush()
    await notify(db, principal.clinic_id, "appointments.changed")
    return encounter


# Vitals, allergies, conditions --------------------------------------------------------------------


async def add_vitals(
    db: AsyncSession,
    principal: ClinicPrincipal,
    patient_id: uuid.UUID,
    body: schemas.VitalsIn,
    req: RequestInfo,
) -> Vital:
    await _record(db, principal, patient_id, R.VITALS, A.CREATE, req, op="vitals.add")
    await _encounter_for(db, body.encounter_id, patient_id)
    vital = Vital(
        clinic_id=principal.clinic_id,
        patient_id=patient_id,
        recorded_by=principal.user_id,
        recorded_at=await db_now(db),
        **body.model_dump(),
    )
    db.add(vital)
    await db.flush()
    await db.refresh(vital)
    return vital


async def add_allergy(
    db: AsyncSession,
    principal: ClinicPrincipal,
    patient_id: uuid.UUID,
    body: schemas.AllergyIn,
    req: RequestInfo,
) -> Allergy:
    await _record(db, principal, patient_id, R.ALLERGIES, A.CREATE, req, op="allergy.add")
    allergy = Allergy(
        clinic_id=principal.clinic_id,
        patient_id=patient_id,
        recorded_by=principal.user_id,
        **body.model_dump(),
    )
    db.add(allergy)
    await db.flush()
    await db.refresh(allergy)
    return allergy


async def update_allergy(
    db: AsyncSession,
    principal: ClinicPrincipal,
    allergy_id: uuid.UUID,
    body: schemas.AllergyUpdate,
    req: RequestInfo,
) -> Allergy:
    allergy = await db.get(Allergy, allergy_id)
    if allergy is None:
        raise ProblemError(404, "allergy-not-found")
    await _record(
        db, principal, allergy.patient_id, R.ALLERGIES, A.EDIT, req, allergy_id=allergy.id
    )
    allergy.is_active = body.is_active
    await db.flush()
    return allergy


async def add_condition(
    db: AsyncSession,
    principal: ClinicPrincipal,
    patient_id: uuid.UUID,
    body: schemas.ConditionIn,
    req: RequestInfo,
) -> Condition:
    # The problem list is part of the clinical record and is logged under notes (ADR 0005).
    await _record(db, principal, patient_id, R.NOTES, A.CREATE, req, op="condition.add")
    condition = Condition(
        clinic_id=principal.clinic_id,
        patient_id=patient_id,
        recorded_by=principal.user_id,
        **body.model_dump(),
    )
    db.add(condition)
    await db.flush()
    await db.refresh(condition)
    return condition


async def update_condition(
    db: AsyncSession,
    principal: ClinicPrincipal,
    condition_id: uuid.UUID,
    body: schemas.ConditionUpdate,
    req: RequestInfo,
) -> Condition:
    condition = await db.get(Condition, condition_id)
    if condition is None:
        raise ProblemError(404, "condition-not-found")
    await _record(
        db, principal, condition.patient_id, R.NOTES, A.EDIT, req, condition_id=condition.id
    )
    condition.status = body.status
    await db.flush()
    return condition


# Notes --------------------------------------------------------------------------------------------


def note_out(note: ClinicalNote) -> NoteOut:
    return NoteOut(
        id=note.id,
        version=note.version,
        parent_id=note.parent_id,
        author_user_id=note.author_user_id,
        created_at=note.created_at,
        signed_at=note.signed_at,
        body=decrypt_body(note.id, note.body_enc),
    )


async def create_note(
    db: AsyncSession,
    principal: ClinicPrincipal,
    patient_id: uuid.UUID,
    body: schemas.NoteIn,
    req: RequestInfo,
) -> ClinicalNote:
    await _record(db, principal, patient_id, R.NOTES, A.CREATE, req, op="note.create")
    encounter = await _encounter_for(db, body.encounter_id, patient_id)
    assert encounter is not None
    note_id = uuid7()
    note = ClinicalNote(
        id=note_id,
        clinic_id=principal.clinic_id,
        patient_id=patient_id,
        encounter_id=encounter.id,
        author_user_id=principal.user_id,
        version=1,
        body_enc=encrypt_body(note_id, body.body),
    )
    db.add(note)
    await db.flush()
    await db.refresh(note)
    return note


async def _own_note(
    db: AsyncSession, principal: ClinicPrincipal, note_id: uuid.UUID
) -> ClinicalNote:
    note = await db.get(ClinicalNote, note_id)
    if note is None:
        raise ProblemError(404, "note-not-found")
    return note


async def edit_note(
    db: AsyncSession,
    principal: ClinicPrincipal,
    note_id: uuid.UUID,
    body: schemas.NoteEdit,
    req: RequestInfo,
) -> ClinicalNote:
    note = await _own_note(db, principal, note_id)
    await _record(db, principal, note.patient_id, R.NOTES, A.EDIT, req, note_id=note.id)
    if note.signed_at is not None:
        raise ProblemError(409, "note-signed", "A signed note cannot change. Add a new version.")
    if note.author_user_id != principal.user_id:
        raise ProblemError(403, "not-the-author", "Only the author can edit a draft note.")
    note.body_enc = encrypt_body(note.id, body.body)
    await db.flush()
    return note


async def sign_note(
    db: AsyncSession, principal: ClinicPrincipal, note_id: uuid.UUID, req: RequestInfo
) -> ClinicalNote:
    note = await _own_note(db, principal, note_id)
    await _record(
        db, principal, note.patient_id, R.NOTES, A.EDIT, req, op="note.sign", note_id=note.id
    )
    if note.signed_at is not None:
        raise ProblemError(409, "note-signed", "This note is already signed.")
    if note.author_user_id != principal.user_id:
        raise ProblemError(403, "not-the-author", "Only the author can sign a note.")
    note.signed_at = await db_now(db)
    await db.flush()
    return note


async def new_note_version(
    db: AsyncSession,
    principal: ClinicPrincipal,
    note_id: uuid.UUID,
    body: schemas.NoteEdit,
    req: RequestInfo,
) -> ClinicalNote:
    parent = await _own_note(db, principal, note_id)
    await _record(
        db, principal, parent.patient_id, R.NOTES, A.EDIT, req, op="note.version", parent=parent.id
    )
    if parent.signed_at is None:
        raise ProblemError(409, "note-not-signed", "Edit the draft instead of adding a version.")
    newer = await db.scalar(select(ClinicalNote.id).where(ClinicalNote.parent_id == parent.id))
    if newer is not None:
        raise ProblemError(409, "not-latest-version", "Add the new version to the latest one.")
    new_id = uuid7()
    note = ClinicalNote(
        id=new_id,
        clinic_id=principal.clinic_id,
        patient_id=parent.patient_id,
        encounter_id=parent.encounter_id,
        author_user_id=principal.user_id,
        version=parent.version + 1,
        parent_id=parent.id,
        body_enc=encrypt_body(new_id, body.body),
    )
    db.add(note)
    await db.flush()
    await db.refresh(note)
    return note


# Prescriptions ------------------------------------------------------------------------------------


def _items(items: Sequence[schemas.PrescriptionItem]) -> list[dict[str, Any]]:
    return [i.model_dump(exclude_none=True) for i in items]


async def create_prescription(
    db: AsyncSession,
    principal: ClinicPrincipal,
    patient_id: uuid.UUID,
    body: schemas.PrescriptionIn,
    req: RequestInfo,
) -> Prescription:
    await _record(db, principal, patient_id, R.PRESCRIPTIONS, A.CREATE, req, op="rx.create")
    encounter = await _encounter_for(db, body.encounter_id, patient_id)
    assert encounter is not None
    rx = Prescription(
        clinic_id=principal.clinic_id,
        patient_id=patient_id,
        encounter_id=encounter.id,
        author_user_id=principal.user_id,
        items=_items(body.items),
        advice_en=body.advice_en,
        advice_hi=body.advice_hi,
        version=1,
    )
    db.add(rx)
    await db.flush()
    await db.refresh(rx)
    return rx


async def _prescription(db: AsyncSession, rx_id: uuid.UUID) -> Prescription:
    rx = await db.get(Prescription, rx_id)
    if rx is None:
        raise ProblemError(404, "prescription-not-found")
    return rx


async def edit_prescription(
    db: AsyncSession,
    principal: ClinicPrincipal,
    rx_id: uuid.UUID,
    body: schemas.PrescriptionEdit,
    req: RequestInfo,
) -> Prescription:
    rx = await _prescription(db, rx_id)
    await _record(db, principal, rx.patient_id, R.PRESCRIPTIONS, A.EDIT, req, rx_id=rx.id)
    if rx.signed_at is not None:
        raise ProblemError(409, "prescription-signed", "A signed prescription cannot change.")
    if rx.author_user_id != principal.user_id:
        raise ProblemError(403, "not-the-author")
    rx.items = _items(body.items)
    rx.advice_en = body.advice_en
    rx.advice_hi = body.advice_hi
    await db.flush()
    return rx


async def sign_prescription(
    db: AsyncSession, principal: ClinicPrincipal, rx_id: uuid.UUID, req: RequestInfo
) -> Prescription:
    rx = await _prescription(db, rx_id)
    await _record(
        db, principal, rx.patient_id, R.PRESCRIPTIONS, A.EDIT, req, op="rx.sign", rx_id=rx.id
    )
    if rx.signed_at is not None:
        raise ProblemError(409, "prescription-signed", "This prescription is already signed.")
    if rx.author_user_id != principal.user_id:
        raise ProblemError(403, "not-the-author")
    rx.signed_at = await db_now(db)
    await db.flush()
    return rx


async def new_prescription_version(
    db: AsyncSession,
    principal: ClinicPrincipal,
    rx_id: uuid.UUID,
    body: schemas.PrescriptionEdit,
    req: RequestInfo,
) -> Prescription:
    parent = await _prescription(db, rx_id)
    await _record(
        db,
        principal,
        parent.patient_id,
        R.PRESCRIPTIONS,
        A.EDIT,
        req,
        op="rx.version",
        parent=parent.id,
    )
    if parent.signed_at is None:
        raise ProblemError(409, "prescription-not-signed", "Edit the draft instead.")
    newer = await db.scalar(select(Prescription.id).where(Prescription.parent_id == parent.id))
    if newer is not None:
        raise ProblemError(409, "not-latest-version")
    rx = Prescription(
        clinic_id=principal.clinic_id,
        patient_id=parent.patient_id,
        encounter_id=parent.encounter_id,
        author_user_id=principal.user_id,
        items=_items(body.items),
        advice_en=body.advice_en,
        advice_hi=body.advice_hi,
        version=parent.version + 1,
        parent_id=parent.id,
    )
    db.add(rx)
    await db.flush()
    await db.refresh(rx)
    return rx


async def get_prescription_for_print(
    db: AsyncSession, principal: ClinicPrincipal, rx_id: uuid.UUID, req: RequestInfo
) -> Prescription:
    rx = await _prescription(db, rx_id)
    await _record(db, principal, rx.patient_id, R.PRESCRIPTIONS, A.PRINT, req, rx_id=rx.id)
    if rx.signed_at is None:
        raise ProblemError(409, "prescription-not-signed", "Sign the prescription before printing.")
    return rx


# Lab ----------------------------------------------------------------------------------------------


def lab_order_out(order: LabOrder, result: LabResult | None) -> LabOrderOut:
    return LabOrderOut(
        id=order.id,
        tests=order.tests,
        status=order.status,
        ordered_by=order.ordered_by,
        created_at=order.created_at,
        result=LabResultOut.model_validate(result) if result else None,
    )


async def _result_of(db: AsyncSession, order: LabOrder) -> LabResult | None:
    return await db.scalar(select(LabResult).where(LabResult.lab_order_id == order.id))


async def order_lab(
    db: AsyncSession,
    principal: ClinicPrincipal,
    patient_id: uuid.UUID,
    body: schemas.LabOrderIn,
    req: RequestInfo,
) -> LabOrder:
    await _record(db, principal, patient_id, R.LAB, A.CREATE, req, op="lab.order")
    await _encounter_for(db, body.encounter_id, patient_id)
    order = LabOrder(
        clinic_id=principal.clinic_id,
        patient_id=patient_id,
        encounter_id=body.encounter_id,
        ordered_by=principal.user_id,
        tests=[t.model_dump() for t in body.tests],
    )
    db.add(order)
    await db.flush()
    await db.refresh(order)
    await notify(db, principal.clinic_id, "lab.changed", lab_order_id=order.id)
    return order


LAB_TRANSITIONS: dict[Role, dict[LabOrderStatus, frozenset[LabOrderStatus]]] = {
    Role.LAB_TECH: {LabOrderStatus.ORDERED: frozenset({LabOrderStatus.COLLECTED})},
    Role.DOCTOR: {
        LabOrderStatus.ORDERED: frozenset({LabOrderStatus.CANCELLED}),
        LabOrderStatus.COLLECTED: frozenset({LabOrderStatus.CANCELLED}),
    },
}


async def update_lab_order(
    db: AsyncSession,
    principal: ClinicPrincipal,
    order_id: uuid.UUID,
    body: schemas.LabOrderUpdate,
    req: RequestInfo,
) -> LabOrder:
    order = await db.get(LabOrder, order_id)
    if order is None:
        raise ProblemError(404, "lab-order-not-found")
    await _record(
        db, principal, order.patient_id, R.LAB, A.EDIT, req, op=f"lab.{body.status.value}"
    )
    allowed = LAB_TRANSITIONS.get(principal.role, {}).get(order.status, frozenset())
    if body.status not in allowed:
        raise ProblemError(
            409,
            "invalid-transition",
            f"Cannot change this order from {order.status.value} to {body.status.value}.",
        )
    order.status = body.status
    await db.flush()
    await notify(db, principal.clinic_id, "lab.changed", lab_order_id=order.id)
    return order


async def worklist(
    db: AsyncSession, principal: ClinicPrincipal, req: RequestInfo
) -> list[schemas.WorklistItem]:
    rows = (
        await db.execute(
            select(LabOrder, Patient)
            .join(
                Patient,
                and_(Patient.id == LabOrder.patient_id, Patient.clinic_id == LabOrder.clinic_id),
            )
            .where(LabOrder.status.in_([LabOrderStatus.ORDERED, LabOrderStatus.COLLECTED]))
            .order_by(LabOrder.created_at)
        )
    ).all()
    await record_many(db, principal, [p.id for _, p in rows], R.LAB, A.VIEW, req)
    return [
        schemas.WorklistItem(
            order=lab_order_out(o, None), patient_id=p.id, patient_name=p.name, patient_mrn=p.mrn
        )
        for o, p in rows
    ]


async def record_result(
    db: AsyncSession,
    principal: ClinicPrincipal,
    order_id: uuid.UUID,
    values: dict[str, Any],
    upload: tuple[bytes, str] | None,
    req: RequestInfo,
) -> tuple[LabOrder, LabResult]:
    order = await db.get(LabOrder, order_id)
    if order is None:
        raise ProblemError(404, "lab-order-not-found")
    await _record(
        db, principal, order.patient_id, R.LAB, A.CREATE, req, op="lab.result", order=order.id
    )
    if order.status != LabOrderStatus.COLLECTED:
        raise ProblemError(409, "invalid-transition", "Mark the sample collected first.")

    document_id = None
    if upload is not None:
        data, content_type = upload
        if content_type not in ALLOWED_UPLOADS:
            raise ProblemError(415, "unsupported-file", "Upload a PDF, PNG or JPEG file.")
        if len(data) > get_settings().max_upload_bytes:
            raise ProblemError(413, "file-too-large")
        await _record(
            db, principal, order.patient_id, R.DOCUMENTS, A.CREATE, req, op="document.upload"
        )
        document_id = uuid7()
        digest = sha256_hex(data)
        extension = ALLOWED_UPLOADS[content_type]
        path = f"{principal.clinic_id}/{order.patient_id}/{document_id}.{extension}"
        await get_storage().put(path, data, content_type)
        db.add(
            Document(
                id=document_id,
                clinic_id=principal.clinic_id,
                patient_id=order.patient_id,
                kind="lab_report",
                blob_path=path,
                sha256=digest,
                uploaded_by=principal.user_id,
            )
        )
        await db.flush()

    result = LabResult(
        clinic_id=principal.clinic_id,
        lab_order_id=order.id,
        patient_id=order.patient_id,
        values=values,
        document_id=document_id,
        resulted_by=principal.user_id,
        resulted_at=await db_now(db),
    )
    db.add(result)
    order.status = LabOrderStatus.RESULTED
    await db.flush()
    await db.refresh(result)
    await notify(db, principal.clinic_id, "lab.changed", lab_order_id=order.id)
    return order, result


async def release_result(
    db: AsyncSession, principal: ClinicPrincipal, order_id: uuid.UUID, req: RequestInfo
) -> tuple[LabOrder, LabResult]:
    order = await db.get(LabOrder, order_id)
    if order is None:
        raise ProblemError(404, "lab-order-not-found")
    await _record(
        db, principal, order.patient_id, R.LAB, A.EDIT, req, op="lab.release", order=order.id
    )
    result = await _result_of(db, order)
    if result is None:
        raise ProblemError(409, "no-result", "There is no result to release yet.")
    if result.released_at is None:
        result.released_at = await db_now(db)
        await db.flush()
    return order, result


async def download_document(
    db: AsyncSession, principal: ClinicPrincipal, document_id: uuid.UUID, req: RequestInfo
) -> tuple[Document, bytes]:
    document = await db.get(Document, document_id)
    if document is None:
        raise ProblemError(404, "document-not-found")
    await _record(
        db, principal, document.patient_id, R.DOCUMENTS, A.VIEW, req, document_id=document.id
    )
    data = await get_storage().get(document.blob_path)
    if sha256_hex(data) != document.sha256:
        raise ProblemError(500, "document-integrity", "The stored file does not match its hash.")
    return document, data


# Referrals and follow-ups -------------------------------------------------------------------------


async def refer(
    db: AsyncSession,
    principal: ClinicPrincipal,
    patient_id: uuid.UUID,
    body: schemas.ReferralIn,
    req: RequestInfo,
) -> Referral:
    await _record(db, principal, patient_id, R.NOTES, A.CREATE, req, op="referral.create")
    if body.to_user_id == principal.user_id:
        raise ProblemError(422, "self-referral", "Refer the patient to another doctor.")
    await clinic_service.require_member(
        db, principal.clinic_id, body.to_user_id, frozenset({Role.DOCTOR})
    )
    referral = Referral(
        clinic_id=principal.clinic_id,
        patient_id=patient_id,
        from_user_id=principal.user_id,
        to_user_id=body.to_user_id,
        reason=body.reason,
        valid_until=await db_now(db) + timedelta(days=body.valid_days),
        created_by=principal.user_id,
    )
    db.add(referral)
    await db.flush()
    await db.refresh(referral)
    return referral


async def update_referral(
    db: AsyncSession,
    principal: ClinicPrincipal,
    referral_id: uuid.UUID,
    body: schemas.ReferralUpdate,
    req: RequestInfo,
) -> Referral:
    referral = await db.get(Referral, referral_id)
    if referral is None:
        raise ProblemError(404, "referral-not-found")
    await _record(db, principal, referral.patient_id, R.NOTES, A.EDIT, req, referral_id=referral.id)
    if principal.user_id not in (referral.from_user_id, referral.to_user_id):
        raise ProblemError(403, "not-your-referral")
    if referral.status != ReferralStatus.ACTIVE:
        raise ProblemError(409, "invalid-transition", "This referral is already closed.")
    referral.status = body.status
    await db.flush()
    return referral


async def book_follow_up(
    db: AsyncSession,
    principal: ClinicPrincipal,
    patient_id: uuid.UUID,
    body: schemas.FollowUpIn,
    req: RequestInfo,
) -> Appointment:
    return await clinic_service.create_appointment(
        db,
        principal,
        clinic_schemas.AppointmentCreate(
            patient_id=patient_id,
            doctor_user_id=principal.user_id,
            slot_start=body.slot_start,
            kind=AppointmentKind.FOLLOWUP,
        ),
        req,
    )


async def note_history(
    db: AsyncSession, principal: ClinicPrincipal, note_id: uuid.UUID, req: RequestInfo
) -> list[NoteOut]:
    note = await _own_note(db, principal, note_id)
    await _record(db, principal, note.patient_id, R.NOTES, A.VIEW, req, note_id=note.id)
    chain = [note]
    while chain[-1].parent_id is not None:
        parent = await db.get(ClinicalNote, chain[-1].parent_id)
        assert parent is not None
        chain.append(parent)
    return [note_out(n) for n in chain]
