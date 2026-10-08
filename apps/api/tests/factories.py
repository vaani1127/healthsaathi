"""Builds one row in every clinic scoped table for a clinic, as the database owner."""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db import TenantContext
from app.db import models as m
from app.db.enums import (
    AccessAction,
    AccessDecision,
    AnchorBackend,
    AppointmentKind,
    AppointmentSource,
    ConsentChannel,
    ConsentEventKind,
    PaymentMethod,
    ResourceType,
    ReviewOutcome,
    Role,
    Sex,
)
from e2d_core.ids import uuid7


@dataclass(frozen=True)
class ClinicGraph:
    clinic_id: uuid.UUID
    admin_id: uuid.UUID
    doctor_id: uuid.UUID
    patient_id: uuid.UUID

    def ctx(self, role: Role = Role.DOCTOR) -> TenantContext:
        user = self.admin_id if role == Role.CLINIC_ADMIN else self.doctor_id
        return TenantContext(clinic_id=self.clinic_id, user_id=user, role=role)


def _user(label: str, role: str) -> m.User:
    return m.User(email=f"{role}.{label}.{uuid7().hex[:8]}@example.test", name=f"{role} {label}")


async def build_clinic_graph(engine: AsyncEngine, label: str) -> ClinicGraph:
    now = datetime.now(UTC)
    digest = hashlib.sha256(label.encode()).digest()
    async with AsyncSession(engine, expire_on_commit=False) as s, s.begin():
        clinic = m.Clinic(name=f"Clinic {label}", city="Test City", state="Test State")
        admin, doctor, nurse, reception, lab = (
            _user(label, r) for r in ("admin", "doctor", "nurse", "reception", "lab")
        )
        s.add_all([clinic, admin, doctor, nurse, reception, lab])
        await s.flush()
        cid = clinic.id

        s.add_all(
            [
                m.Membership(clinic_id=cid, user_id=admin.id, role=Role.CLINIC_ADMIN),
                m.Membership(clinic_id=cid, user_id=doctor.id, role=Role.DOCTOR),
                m.Membership(clinic_id=cid, user_id=nurse.id, role=Role.NURSE),
                m.Membership(clinic_id=cid, user_id=reception.id, role=Role.RECEPTION),
                m.Membership(clinic_id=cid, user_id=lab.id, role=Role.LAB_TECH),
                m.StaffProfile(clinic_id=cid, user_id=doctor.id, registration_no="T-1"),
            ]
        )
        patient = m.Patient(
            clinic_id=cid,
            mrn=f"T-{label}",
            name=f"Patient {label}",
            dob=date(1990, 1, 1),
            sex=Sex.FEMALE,
            created_by=reception.id,
        )
        s.add(patient)
        await s.flush()
        pid = patient.id

        s.add_all(
            [
                m.Schedule(
                    clinic_id=cid,
                    doctor_user_id=doctor.id,
                    weekday=0,
                    start_time=time(9),
                    end_time=time(13),
                    slot_minutes=15,
                    created_by=admin.id,
                ),
                m.Shift(
                    clinic_id=cid,
                    user_id=nurse.id,
                    starts_at=now,
                    ends_at=now + timedelta(hours=8),
                    role=Role.NURSE,
                    created_by=admin.id,
                ),
            ]
        )
        appt = m.Appointment(
            clinic_id=cid,
            patient_id=pid,
            doctor_user_id=doctor.id,
            slot_start=now,
            slot_end=now + timedelta(minutes=15),
            kind=AppointmentKind.NEW,
            source=AppointmentSource.RECEPTION,
            created_by=reception.id,
        )
        s.add(appt)
        await s.flush()
        enc = m.Encounter(
            clinic_id=cid,
            appointment_id=appt.id,
            patient_id=pid,
            doctor_user_id=doctor.id,
            started_at=now,
            created_by=doctor.id,
        )
        s.add_all(
            [
                m.QueueToken(
                    clinic_id=cid,
                    appointment_id=appt.id,
                    token_no=1,
                    day=now.date(),
                    created_by=reception.id,
                ),
                enc,
                m.Referral(
                    clinic_id=cid,
                    patient_id=pid,
                    from_user_id=doctor.id,
                    to_user_id=doctor.id,
                    reason="test",
                    valid_until=now + timedelta(days=7),
                    created_by=doctor.id,
                ),
                m.CareTeamAssignment(
                    clinic_id=cid,
                    patient_id=pid,
                    user_id=nurse.id,
                    role=Role.NURSE,
                    starts_at=now,
                    created_by=admin.id,
                ),
            ]
        )
        await s.flush()

        order = m.LabOrder(
            clinic_id=cid, patient_id=pid, encounter_id=enc.id, ordered_by=doctor.id, tests=[]
        )
        doc = m.Document(
            clinic_id=cid,
            patient_id=pid,
            kind="lab_report",
            blob_path="test/doc.pdf",
            sha256=digest.hex(),
            uploaded_by=lab.id,
        )
        note = m.ClinicalNote(
            clinic_id=cid,
            patient_id=pid,
            encounter_id=enc.id,
            author_user_id=doctor.id,
            body_enc=b"encrypted",
        )
        s.add_all(
            [
                m.Vital(clinic_id=cid, patient_id=pid, encounter_id=enc.id, recorded_by=nurse.id),
                m.Allergy(
                    clinic_id=cid,
                    patient_id=pid,
                    substance="Penicillin",
                    severity="severe",
                    recorded_by=nurse.id,
                ),
                m.Condition(clinic_id=cid, patient_id=pid, text="Fever", recorded_by=doctor.id),
                note,
                m.Prescription(
                    clinic_id=cid,
                    patient_id=pid,
                    encounter_id=enc.id,
                    author_user_id=doctor.id,
                    items=[],
                ),
                order,
                doc,
            ]
        )
        await s.flush()
        s.add(
            m.LabResult(
                clinic_id=cid,
                lab_order_id=order.id,
                patient_id=pid,
                values={},
                document_id=doc.id,
                resulted_by=lab.id,
            )
        )

        service = m.Service(clinic_id=cid, name="Consultation", price_paise=30000)
        invoice = m.Invoice(
            clinic_id=cid, patient_id=pid, appointment_id=appt.id, created_by=reception.id
        )
        notice = m.ConsentNotice(
            clinic_id=cid, version=1, text_en="en", text_hi="hi", purposes=["treatment"]
        )
        s.add_all([service, invoice, notice])
        await s.flush()
        consent = m.Consent(
            clinic_id=cid,
            patient_id=pid,
            notice_id=notice.id,
            channel=ConsentChannel.RECEPTION,
            salt_hash=digest.hex(),
        )
        s.add_all(
            [
                m.InvoiceItem(
                    clinic_id=cid, invoice_id=invoice.id, service_id=service.id, price_paise=30000
                ),
                m.Payment(
                    clinic_id=cid,
                    invoice_id=invoice.id,
                    method=PaymentMethod.CASH,
                    amount_paise=30000,
                    received_by=reception.id,
                ),
                consent,
            ]
        )
        await s.flush()
        s.add(
            m.ConsentEvent(
                clinic_id=cid,
                consent_id=consent.id,
                kind=ConsentEventKind.GRANTED,
                by_user_id=reception.id,
            )
        )

        bg = m.BreakGlassEvent(
            clinic_id=cid,
            user_id=doctor.id,
            patient_id=pid,
            reason_code="emergency",
            reason_text="t",
        )
        s.add(bg)
        await s.flush()
        event = m.AccessEvent(
            clinic_id=cid,
            user_id=doctor.id,
            role=Role.DOCTOR,
            patient_id=pid,
            resource_type=ResourceType.NOTES,
            action=AccessAction.VIEW,
            at=now,
            policy_version="0" * 64,
            decision=AccessDecision.ALLOW,
            break_glass_id=bg.id,
        )
        s.add(event)
        await s.flush()
        alert = m.Alert(
            clinic_id=cid,
            access_event_id=event.id,
            day=now.date(),
            score=0.5,
            rank_in_day=1,
            features={},
            model_version="m",
            policy_version="p",
        )
        s.add_all(
            [
                m.AccessExplanation(
                    access_event_id=event.id,
                    clinic_id=cid,
                    template_code="T_BREAKGLASS",
                    evidence={},
                    strength=0.2,
                    forgery_flags={},
                ),
                alert,
                m.PatientAccessQuery(
                    clinic_id=cid, patient_id=pid, access_event_id=event.id, message="who?"
                ),
                m.AuditEvent(
                    clinic_id=cid,
                    kind="test",
                    payload={},
                    payload_hash=digest,
                    prev_hash=bytes(32),
                    chain_hash=digest,
                ),
            ]
        )
        await s.flush()
        checkpoint = m.MerkleCheckpoint(
            clinic_id=cid, tree_size=1, root=digest, signature=b"sig", key_id="k"
        )
        s.add_all(
            [
                m.AlertReview(
                    clinic_id=cid,
                    alert_id=alert.id,
                    reviewer_user_id=admin.id,
                    outcome=ReviewOutcome.BENIGN,
                ),
                checkpoint,
            ]
        )
        await s.flush()
        s.add(
            m.AnchorReceipt(clinic_id=cid, checkpoint_id=checkpoint.id, backend=AnchorBackend.AMOY)
        )

    return ClinicGraph(clinic_id=cid, admin_id=admin.id, doctor_id=doctor.id, patient_id=pid)
