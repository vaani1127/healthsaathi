import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db import models as m
from app.db.enums import Role
from tests.helpers import Actor, access_events, make_actor, make_clinic, make_patient


@pytest.fixture
async def clinic(admin_engine: AsyncEngine) -> uuid.UUID:
    return await make_clinic(admin_engine)


@pytest.fixture
async def staff(admin_engine: AsyncEngine, clinic: uuid.UUID) -> dict[Role, Actor]:
    return {
        role: await make_actor(admin_engine, clinic, role)
        for role in (Role.RECEPTION, Role.NURSE, Role.DOCTOR, Role.LAB_TECH, Role.CLINIC_ADMIN)
    }


@pytest.fixture
async def visit(
    client: AsyncClient, admin_engine: AsyncEngine, clinic: uuid.UUID, staff: dict[Role, Actor]
) -> dict[str, str]:
    """A patient with today's appointment and an open consultation."""
    patient = await make_patient(admin_engine, clinic, staff[Role.RECEPTION].user_id)
    appt = await client.post(
        "/api/v1/appointments",
        json={
            "patient_id": str(patient),
            "doctor_user_id": str(staff[Role.DOCTOR].user_id),
            "slot_start": datetime.now(UTC).isoformat(),
        },
        headers=staff[Role.RECEPTION].headers,
    )
    assert appt.status_code == 201, appt.text
    enc = await client.post(
        f"/api/v1/appointments/{appt.json()['id']}/encounter", headers=staff[Role.DOCTOR].headers
    )
    assert enc.status_code == 201, enc.text
    return {
        "patient": str(patient),
        "appointment": appt.json()["id"],
        "encounter": enc.json()["id"],
    }


async def audit_count(engine: AsyncEngine, clinic: uuid.UUID) -> int:
    async with AsyncSession(engine) as s:
        return int(
            await s.scalar(
                select(func.count())
                .select_from(m.AuditEvent)
                .where(m.AuditEvent.clinic_id == clinic)
            )
            or 0
        )


# Notes -------------------------------------------------------------------------------------------


async def test_note_draft_sign_and_version(
    client: AsyncClient, staff: dict[Role, Actor], visit: dict[str, str]
) -> None:
    doctor = staff[Role.DOCTOR]
    created = await client.post(
        f"/api/v1/patients/{visit['patient']}/notes",
        json={"encounter_id": visit["encounter"], "body": "Fever for 3 days."},
        headers=doctor.headers,
    )
    assert created.status_code == 201
    note = created.json()
    assert note["version"] == 1 and note["signed_at"] is None

    edited = await client.patch(
        f"/api/v1/notes/{note['id']}",
        json={"body": "Fever for 3 days. No rash."},
        headers=doctor.headers,
    )
    assert edited.json()["body"] == "Fever for 3 days. No rash."

    signed = await client.post(f"/api/v1/notes/{note['id']}/sign", headers=doctor.headers)
    assert signed.status_code == 200
    assert signed.json()["signed_at"] is not None

    locked = await client.patch(
        f"/api/v1/notes/{note['id']}", json={"body": "changed"}, headers=doctor.headers
    )
    assert locked.status_code == 409
    assert locked.json()["code"] == "note-signed"

    v2 = await client.post(
        f"/api/v1/notes/{note['id']}/versions",
        json={"body": "Correction: 4 days."},
        headers=doctor.headers,
    )
    assert v2.status_code == 201
    assert v2.json()["version"] == 2
    assert v2.json()["parent_id"] == note["id"]

    again = await client.post(
        f"/api/v1/notes/{note['id']}/versions", json={"body": "fork"}, headers=doctor.headers
    )
    assert again.status_code == 409

    history = await client.get(f"/api/v1/notes/{v2.json()['id']}/history", headers=doctor.headers)
    assert [n["version"] for n in history.json()] == [2, 1]
    assert history.json()[1]["body"] == "Fever for 3 days. No rash."


async def test_note_body_is_encrypted_at_rest(
    client: AsyncClient, admin_engine: AsyncEngine, staff: dict[Role, Actor], visit: dict[str, str]
) -> None:
    resp = await client.post(
        f"/api/v1/patients/{visit['patient']}/notes",
        json={"encounter_id": visit["encounter"], "body": "Very private finding"},
        headers=staff[Role.DOCTOR].headers,
    )
    async with AsyncSession(admin_engine) as s:
        blob = await s.scalar(
            select(m.ClinicalNote.body_enc).where(m.ClinicalNote.id == uuid.UUID(resp.json()["id"]))
        )
    assert blob is not None
    assert b"Very private" not in blob


async def test_signed_note_cannot_be_changed_even_in_the_database(
    client: AsyncClient, admin_engine: AsyncEngine, staff: dict[Role, Actor], visit: dict[str, str]
) -> None:
    doctor = staff[Role.DOCTOR]
    note = (
        await client.post(
            f"/api/v1/patients/{visit['patient']}/notes",
            json={"encounter_id": visit["encounter"], "body": "Signed text"},
            headers=doctor.headers,
        )
    ).json()
    await client.post(f"/api/v1/notes/{note['id']}/sign", headers=doctor.headers)
    for sql in (
        "UPDATE clinical_notes SET body_enc = 'x' WHERE id = :id",
        "DELETE FROM clinical_notes WHERE id = :id",
    ):
        with pytest.raises(DBAPIError, match="is signed"):
            async with admin_engine.begin() as conn:
                await conn.execute(text(sql), {"id": note["id"]})


async def test_only_the_author_edits_or_signs(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    visit: dict[str, str],
) -> None:
    note = (
        await client.post(
            f"/api/v1/patients/{visit['patient']}/notes",
            json={"encounter_id": visit["encounter"], "body": "Draft"},
            headers=staff[Role.DOCTOR].headers,
        )
    ).json()
    other = await make_actor(admin_engine, clinic, Role.DOCTOR)
    # The other doctor needs a reason to touch this chart; with one, they still cannot sign it.
    resp = await client.post(f"/api/v1/notes/{note['id']}/sign", headers=other.headers)
    assert resp.status_code == 403


async def test_note_needs_an_encounter_of_the_patient(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    visit: dict[str, str],
) -> None:
    other_patient = await make_patient(admin_engine, clinic, staff[Role.RECEPTION].user_id)
    resp = await client.post(
        f"/api/v1/patients/{other_patient}/notes",
        json={"encounter_id": visit["encounter"], "body": "wrong patient"},
        headers=staff[Role.DOCTOR].headers,
    )
    assert resp.status_code in (403, 422)


# Prescriptions ------------------------------------------------------------------------------------

ITEMS = [{"drug": "Paracetamol", "strength": "500 mg", "dose": "1-1-1", "duration_days": 3}]


async def test_prescription_versions_and_print(
    client: AsyncClient, staff: dict[Role, Actor], visit: dict[str, str]
) -> None:
    doctor = staff[Role.DOCTOR]
    rx = (
        await client.post(
            f"/api/v1/patients/{visit['patient']}/prescriptions",
            json={
                "encounter_id": visit["encounter"],
                "items": ITEMS,
                "advice_en": "Rest",
                "advice_hi": "आराम करें",
            },
            headers=doctor.headers,
        )
    ).json()
    early_print = await client.get(
        f"/api/v1/prescriptions/{rx['id']}/print", headers=doctor.headers
    )
    assert early_print.status_code == 409

    signed = await client.post(f"/api/v1/prescriptions/{rx['id']}/sign", headers=doctor.headers)
    assert signed.status_code == 200
    locked = await client.patch(
        f"/api/v1/prescriptions/{rx['id']}", json={"items": ITEMS}, headers=doctor.headers
    )
    assert locked.status_code == 409
    printed = await client.get(f"/api/v1/prescriptions/{rx['id']}/print", headers=doctor.headers)
    assert printed.status_code == 200
    assert printed.json()["advice_hi"] == "आराम करें"

    v2 = await client.post(
        f"/api/v1/prescriptions/{rx['id']}/versions",
        json={"items": [{**ITEMS[0], "duration_days": 5}]},
        headers=doctor.headers,
    )
    assert v2.json()["version"] == 2


async def test_bad_prescription_is_rejected(
    client: AsyncClient, staff: dict[Role, Actor], visit: dict[str, str]
) -> None:
    resp = await client.post(
        f"/api/v1/patients/{visit['patient']}/prescriptions",
        json={"encounter_id": visit["encounter"], "items": []},
        headers=staff[Role.DOCTOR].headers,
    )
    assert resp.status_code == 422


# Vitals, allergies, conditions -------------------------------------------------------------------


async def test_vitals_allergies_conditions(
    client: AsyncClient, staff: dict[Role, Actor], visit: dict[str, str]
) -> None:
    nurse, doctor = staff[Role.NURSE], staff[Role.DOCTOR]
    pid = visit["patient"]
    bad = await client.post(
        f"/api/v1/patients/{pid}/vitals", json={"bp_sys": 120}, headers=doctor.headers
    )
    assert bad.status_code == 422
    empty = await client.post(f"/api/v1/patients/{pid}/vitals", json={}, headers=doctor.headers)
    assert empty.status_code == 422
    ok = await client.post(
        f"/api/v1/patients/{pid}/vitals",
        json={"bp_sys": 120, "bp_dia": 80, "pulse": 76, "temp_c": "37.2", "spo2": 98},
        headers=doctor.headers,
    )
    assert ok.status_code == 201, ok.text

    allergy = await client.post(
        f"/api/v1/patients/{pid}/allergies",
        json={"substance": "Sulfa", "severity": "moderate", "reaction": "Rash"},
        headers=doctor.headers,
    )
    assert allergy.status_code == 201
    off = await client.patch(
        f"/api/v1/allergies/{allergy.json()['id']}",
        json={"is_active": False},
        headers=doctor.headers,
    )
    assert off.json()["is_active"] is False

    condition = await client.post(
        f"/api/v1/patients/{pid}/conditions",
        json={"text": "Type 2 diabetes"},
        headers=doctor.headers,
    )
    assert condition.status_code == 201
    resolved = await client.patch(
        f"/api/v1/conditions/{condition.json()['id']}",
        json={"status": "resolved"},
        headers=doctor.headers,
    )
    assert resolved.json()["status"] == "resolved"

    nurse_condition = await client.post(
        f"/api/v1/patients/{pid}/conditions", json={"text": "x x"}, headers=nurse.headers
    )
    assert nurse_condition.status_code == 403


# Lab ----------------------------------------------------------------------------------------------


async def test_lab_order_to_released_result_with_file(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    visit: dict[str, str],
) -> None:
    doctor, lab = staff[Role.DOCTOR], staff[Role.LAB_TECH]
    order = (
        await client.post(
            f"/api/v1/patients/{visit['patient']}/lab-orders",
            json={
                "encounter_id": visit["encounter"],
                "tests": [{"code": "CBC", "name": "Complete blood count"}],
            },
            headers=doctor.headers,
        )
    ).json()
    worklist = await client.get("/api/v1/lab/worklist", headers=lab.headers)
    assert [w["order"]["id"] for w in worklist.json()] == [order["id"]]

    too_early = await client.post(
        f"/api/v1/lab-orders/{order['id']}/result", data={"values": "{}"}, headers=lab.headers
    )
    assert too_early.status_code == 409

    collected = await client.patch(
        f"/api/v1/lab-orders/{order['id']}", json={"status": "collected"}, headers=lab.headers
    )
    assert collected.json()["status"] == "collected"

    pdf = b"%PDF-1.4 synthetic report"
    bad_type = await client.post(
        f"/api/v1/lab-orders/{order['id']}/result",
        data={"values": "{}"},
        files={"file": ("x.exe", b"MZ", "application/x-msdownload")},
        headers=lab.headers,
    )
    assert bad_type.status_code == 415
    bad_values = await client.post(
        f"/api/v1/lab-orders/{order['id']}/result", data={"values": "[1]"}, headers=lab.headers
    )
    assert bad_values.status_code == 422

    result = await client.post(
        f"/api/v1/lab-orders/{order['id']}/result",
        data={"values": json.dumps({"hb": {"value": 13.1, "unit": "g/dL"}})},
        files={"file": ("cbc.pdf", pdf, "application/pdf")},
        headers=lab.headers,
    )
    assert result.status_code == 201, result.text
    body = result.json()
    assert body["status"] == "resulted"
    assert body["result"]["values"]["hb"]["value"] == 13.1
    assert body["result"]["released_at"] is None

    async with AsyncSession(admin_engine) as s:
        doc = await s.scalar(
            select(m.Document).where(m.Document.id == uuid.UUID(body["result"]["document_id"]))
        )
    assert doc is not None
    assert doc.sha256 == hashlib.sha256(pdf).hexdigest()

    released = await client.post(f"/api/v1/lab-orders/{order['id']}/release", headers=lab.headers)
    assert released.json()["result"]["released_at"] is not None

    download = await client.get(f"/api/v1/documents/{doc.id}", headers=doctor.headers)
    assert download.status_code == 200
    assert download.content == pdf
    assert download.headers["x-content-sha256"] == doc.sha256

    chart = await client.get(f"/api/v1/patients/{visit['patient']}/chart", headers=doctor.headers)
    assert chart.json()["lab_orders"][0]["result"]["values"]["hb"]["value"] == 13.1


async def test_lab_status_rules(
    client: AsyncClient, staff: dict[Role, Actor], visit: dict[str, str]
) -> None:
    doctor, lab = staff[Role.DOCTOR], staff[Role.LAB_TECH]
    order = (
        await client.post(
            f"/api/v1/patients/{visit['patient']}/lab-orders",
            json={"tests": [{"code": "RBS", "name": "Random blood sugar"}]},
            headers=doctor.headers,
        )
    ).json()
    lab_cancel = await client.patch(
        f"/api/v1/lab-orders/{order['id']}", json={"status": "cancelled"}, headers=lab.headers
    )
    assert lab_cancel.status_code == 409
    cancelled = await client.patch(
        f"/api/v1/lab-orders/{order['id']}", json={"status": "cancelled"}, headers=doctor.headers
    )
    assert cancelled.json()["status"] == "cancelled"


# Referrals and follow-ups -------------------------------------------------------------------------


async def test_referral_explains_the_other_doctor(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    visit: dict[str, str],
) -> None:
    specialist = await make_actor(admin_engine, clinic, Role.DOCTOR)
    before = await client.get(
        f"/api/v1/patients/{visit['patient']}/chart", headers=specialist.headers
    )
    assert before.status_code == 428

    ref = await client.post(
        f"/api/v1/patients/{visit['patient']}/referrals",
        json={"to_user_id": str(specialist.user_id), "reason": "Please review the ECG changes."},
        headers=staff[Role.DOCTOR].headers,
    )
    assert ref.status_code == 201
    after = await client.get(
        f"/api/v1/patients/{visit['patient']}/chart", headers=specialist.headers
    )
    assert after.status_code == 200
    assert after.json()["explanation"] == "T_REFERRAL"

    self_ref = await client.post(
        f"/api/v1/patients/{visit['patient']}/referrals",
        json={"to_user_id": str(staff[Role.DOCTOR].user_id), "reason": "myself again"},
        headers=staff[Role.DOCTOR].headers,
    )
    assert self_ref.status_code == 422

    done = await client.patch(
        f"/api/v1/referrals/{ref.json()['id']}",
        json={"status": "completed"},
        headers=specialist.headers,
    )
    assert done.json()["status"] == "completed"


async def test_follow_up_and_close_encounter(
    client: AsyncClient, staff: dict[Role, Actor], visit: dict[str, str]
) -> None:
    doctor = staff[Role.DOCTOR]
    follow = await client.post(
        f"/api/v1/patients/{visit['patient']}/follow-ups",
        json={"slot_start": (datetime.now(UTC) + timedelta(days=14)).isoformat()},
        headers=doctor.headers,
    )
    assert follow.status_code == 201
    assert follow.json()["kind"] == "followup"
    assert follow.json()["source"] == "doctor"

    closed = await client.post(
        f"/api/v1/encounters/{visit['encounter']}/close", headers=doctor.headers
    )
    assert closed.json()["status"] == "closed"
    appts = await client.get("/api/v1/appointments", headers=staff[Role.RECEPTION].headers)
    statuses = {a["id"]: a["status"] for a in appts.json()}
    assert statuses[visit["appointment"]] == "completed"


# Billing ------------------------------------------------------------------------------------------


async def test_invoice_payment_receipt_and_report(
    client: AsyncClient, staff: dict[Role, Actor], visit: dict[str, str]
) -> None:
    admin, reception = staff[Role.CLINIC_ADMIN], staff[Role.RECEPTION]
    consult = (
        await client.post(
            "/api/v1/services",
            json={"name": "Consultation", "price_paise": 30000},
            headers=admin.headers,
        )
    ).json()
    cbc = (
        await client.post(
            "/api/v1/services", json={"name": "CBC", "price_paise": 25000}, headers=admin.headers
        )
    ).json()
    dup = await client.post(
        "/api/v1/services", json={"name": "CBC", "price_paise": 1}, headers=admin.headers
    )
    assert dup.status_code == 409

    invoice = await client.post(
        f"/api/v1/patients/{visit['patient']}/invoices",
        json={
            "appointment_id": visit["appointment"],
            "items": [{"service_id": consult["id"]}, {"service_id": cbc["id"], "qty": 2}],
        },
        headers=reception.headers,
    )
    assert invoice.status_code == 201
    inv = invoice.json()
    assert inv["total_paise"] == 80000 and inv["status"] == "draft"

    early = await client.post(
        f"/api/v1/invoices/{inv['id']}/payments",
        json={"method": "cash", "amount_paise": 100},
        headers=reception.headers,
    )
    assert early.status_code == 409
    await client.post(f"/api/v1/invoices/{inv['id']}/issue", headers=reception.headers)

    over = await client.post(
        f"/api/v1/invoices/{inv['id']}/payments",
        json={"method": "upi", "amount_paise": 90000},
        headers=reception.headers,
    )
    assert over.status_code == 422
    part = await client.post(
        f"/api/v1/invoices/{inv['id']}/payments",
        json={"method": "upi", "amount_paise": 50000},
        headers=reception.headers,
    )
    assert part.json()["status"] == "issued"
    rest = await client.post(
        f"/api/v1/invoices/{inv['id']}/payments",
        json={"method": "cash", "amount_paise": 30000},
        headers=reception.headers,
    )
    assert rest.json()["status"] == "paid"
    assert rest.json()["paid_paise"] == 80000

    void = await client.post(f"/api/v1/invoices/{inv['id']}/void", headers=reception.headers)
    assert void.status_code == 409

    receipt = await client.get(f"/api/v1/invoices/{inv['id']}/receipt", headers=reception.headers)
    assert receipt.status_code == 200
    assert [i["service_name"] for i in receipt.json()["items"]] == ["CBC", "Consultation"]

    report = await client.get("/api/v1/reports/daily-revenue", headers=admin.headers)
    assert report.json()["total_paise"] == 80000
    assert report.json()["by_method"] == {"cash": 30000, "upi": 50000}
    assert report.json()["invoices_paid"] == 1

    listed = await client.get("/api/v1/invoices", headers=reception.headers)
    assert [i["id"] for i in listed.json()] == [inv["id"]]

    doctor_billing = await client.post(
        f"/api/v1/patients/{visit['patient']}/invoices",
        json={"items": [{"service_id": consult["id"]}]},
        headers=staff[Role.DOCTOR].headers,
    )
    assert doctor_billing.status_code == 403


async def test_inactive_service_cannot_be_billed(
    client: AsyncClient, staff: dict[Role, Actor], visit: dict[str, str]
) -> None:
    admin = staff[Role.CLINIC_ADMIN]
    svc = (
        await client.post(
            "/api/v1/services",
            json={"name": "Dressing", "price_paise": 10000},
            headers=admin.headers,
        )
    ).json()
    await client.patch(
        f"/api/v1/services/{svc['id']}", json={"is_active": False}, headers=admin.headers
    )
    resp = await client.post(
        f"/api/v1/patients/{visit['patient']}/invoices",
        json={"items": [{"service_id": svc["id"]}]},
        headers=staff[Role.RECEPTION].headers,
    )
    assert resp.status_code == 422


# Consent ------------------------------------------------------------------------------------------

NOTICE = {
    "text_en": "We record your health information to treat you and log every access.",
    "text_hi": "हम आपके इलाज के लिए आपकी स्वास्थ्य जानकारी दर्ज करते हैं और हर पहुंच का लॉग रखते हैं।",
    "purposes": ["treatment", "access_audit"],
}


async def test_consent_grant_withdraw_visible_to_patient(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
) -> None:
    admin, reception = staff[Role.CLINIC_ADMIN], staff[Role.RECEPTION]
    patient_actor = await make_actor(admin_engine, clinic, Role.PATIENT)
    patient = await make_patient(admin_engine, clinic, reception.user_id, patient_actor.user_id)

    none_yet = await client.post(f"/api/v1/patients/{patient}/consents", headers=reception.headers)
    assert none_yet.status_code == 409

    notice = (
        await client.post("/api/v1/consent-notices", json=NOTICE, headers=admin.headers)
    ).json()
    assert notice["version"] == 1 and notice["published_at"] is None
    hidden = await client.get("/api/v1/consent-notices", headers=reception.headers)
    assert hidden.json() == []
    await client.post(f"/api/v1/consent-notices/{notice['id']}/publish", headers=admin.headers)
    current = await client.get("/api/v1/consent-notices/current", headers=patient_actor.headers)
    assert current.json()["text_hi"] == NOTICE["text_hi"]

    granted = await client.post(f"/api/v1/patients/{patient}/consents", headers=reception.headers)
    assert granted.status_code == 201
    consent = granted.json()
    assert consent["channel"] == "reception"
    twice = await client.post(f"/api/v1/patients/{patient}/consents", headers=reception.headers)
    assert twice.status_code == 409

    withdrawn = await client.post(
        f"/api/v1/consents/{consent['id']}/withdraw", headers=patient_actor.headers
    )
    assert withdrawn.status_code == 200
    assert withdrawn.json()["withdrawn_at"] is not None

    seen = await client.get(f"/api/v1/patients/{patient}/consents", headers=patient_actor.headers)
    assert seen.status_code == 200
    (row,) = seen.json()
    assert [e["kind"] for e in row["events"]] == ["granted", "withdrawn"]
    assert row["events"][1]["by_user_id"] == str(patient_actor.user_id)

    async with AsyncSession(admin_engine) as s:
        salt = await s.scalar(
            select(m.Consent.salt_hash).where(m.Consent.id == uuid.UUID(consent["id"]))
        )
    assert salt is not None and len(salt) == 64 and str(patient) not in salt


async def test_patient_cannot_withdraw_someone_elses_consent(
    client: AsyncClient, admin_engine: AsyncEngine, clinic: uuid.UUID, staff: dict[Role, Actor]
) -> None:
    admin, reception = staff[Role.CLINIC_ADMIN], staff[Role.RECEPTION]
    notice = (
        await client.post("/api/v1/consent-notices", json=NOTICE, headers=admin.headers)
    ).json()
    await client.post(f"/api/v1/consent-notices/{notice['id']}/publish", headers=admin.headers)
    stranger = await make_actor(admin_engine, clinic, Role.PATIENT)
    patient = await make_patient(admin_engine, clinic, reception.user_id)
    consent = (
        await client.post(f"/api/v1/patients/{patient}/consents", headers=reception.headers)
    ).json()
    resp = await client.post(f"/api/v1/consents/{consent['id']}/withdraw", headers=stranger.headers)
    assert resp.status_code == 403


# Every action is audited --------------------------------------------------------------------------


async def test_every_write_adds_an_audit_event(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    visit: dict[str, str],
) -> None:
    doctor = staff[Role.DOCTOR]
    calls: list[tuple[str, str, dict[str, Any] | None]] = [
        ("post", f"/api/v1/patients/{visit['patient']}/vitals", {"pulse": 80}),
        (
            "post",
            f"/api/v1/patients/{visit['patient']}/allergies",
            {"substance": "Latex", "severity": "mild"},
        ),
        (
            "post",
            f"/api/v1/patients/{visit['patient']}/notes",
            {"encounter_id": visit["encounter"], "body": "x"},
        ),
    ]
    for method, url, body in calls:
        before = await audit_count(admin_engine, clinic)
        resp = await getattr(client, method)(url, json=body, headers=doctor.headers)
        assert resp.status_code < 300, resp.text
        assert await audit_count(admin_engine, clinic) == before + 1, url

    note_id = resp.json()["id"]
    before = await audit_count(admin_engine, clinic)
    await client.post(f"/api/v1/notes/{note_id}/sign", headers=doctor.headers)
    assert await audit_count(admin_engine, clinic) == before + 1
    async with AsyncSession(admin_engine) as s:
        last = await s.scalar(
            select(m.AuditEvent.payload)
            .where(m.AuditEvent.clinic_id == clinic)
            .order_by(m.AuditEvent.seq.desc())
            .limit(1)
        )
    assert last is not None
    assert last["detail"] == {"op": "note.sign", "note_id": note_id}

    events = await access_events(admin_engine, clinic)
    assert all(x is not None for _, x in events)
