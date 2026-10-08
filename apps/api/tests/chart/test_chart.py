import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.clinical.notes_crypto import encrypt_body
from app.db import models as m
from app.db.enums import AccessDecision, ResourceType, Role
from e2d_core.ids import uuid7
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
async def patient(
    admin_engine: AsyncEngine, clinic: uuid.UUID, staff: dict[Role, Actor]
) -> uuid.UUID:
    """A patient with one of everything: allergy, condition, note, prescription, lab order."""
    pid = await make_patient(admin_engine, clinic, staff[Role.RECEPTION].user_id)
    doctor = staff[Role.DOCTOR].user_id
    now = datetime.now(UTC)
    async with AsyncSession(admin_engine) as s, s.begin():
        encounter = m.Encounter(
            clinic_id=clinic,
            patient_id=pid,
            doctor_user_id=doctor,
            started_at=now,
            created_by=doctor,
        )
        s.add(encounter)
        await s.flush()
        note_id = uuid7()
        s.add_all(
            [
                m.Allergy(
                    clinic_id=clinic,
                    patient_id=pid,
                    substance="Penicillin",
                    severity="severe",
                    recorded_by=doctor,
                ),
                m.Allergy(
                    clinic_id=clinic,
                    patient_id=pid,
                    substance="Dust",
                    severity="mild",
                    is_active=False,
                    recorded_by=doctor,
                ),
                m.Condition(
                    clinic_id=clinic, patient_id=pid, text="Hypertension", recorded_by=doctor
                ),
                m.Condition(
                    clinic_id=clinic,
                    patient_id=pid,
                    text="Old fracture",
                    status="resolved",
                    recorded_by=doctor,
                ),
                m.ClinicalNote(
                    id=note_id,
                    clinic_id=clinic,
                    patient_id=pid,
                    encounter_id=encounter.id,
                    author_user_id=doctor,
                    body_enc=encrypt_body(note_id, "Chest clear. Review in two weeks."),
                    signed_at=now,
                ),
                m.Prescription(
                    clinic_id=clinic,
                    patient_id=pid,
                    encounter_id=encounter.id,
                    author_user_id=doctor,
                    items=[{"drug": "Amlodipine 5 mg", "dose": "1-0-0", "days": 30}],
                    signed_at=now,
                ),
                m.LabOrder(
                    clinic_id=clinic,
                    patient_id=pid,
                    encounter_id=encounter.id,
                    ordered_by=doctor,
                    tests=[{"code": "CBC"}],
                ),
                m.Vital(
                    clinic_id=clinic,
                    patient_id=pid,
                    recorded_by=staff[Role.NURSE].user_id,
                    pulse=72,
                ),
            ]
        )
    return pid


async def book(client: AsyncClient, staff: dict[Role, Actor], patient: uuid.UUID) -> None:
    resp = await client.post(
        "/api/v1/appointments",
        json={
            "patient_id": str(patient),
            "doctor_user_id": str(staff[Role.DOCTOR].user_id),
            "slot_start": datetime.now(UTC).isoformat(),
        },
        headers=staff[Role.RECEPTION].headers,
    )
    assert resp.status_code == 201


# Task views ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "view", "allowed_keys", "resources"),
    [
        (
            Role.RECEPTION,
            "reception",
            {"view", "patient", "appointments", "invoices", "consent"},
            {"demographics", "billing", "consent"},
        ),
        (
            Role.NURSE,
            "nurse",
            {"view", "patient", "today_appointment", "vitals", "allergies"},
            {"demographics", "vitals", "allergies"},
        ),
        (Role.LAB_TECH, "lab", {"view", "patient", "worklist"}, {"demographics", "lab"}),
        (Role.CLINIC_ADMIN, "admin", {"view", "patient"}, {"demographics"}),
    ],
)
async def test_each_role_gets_only_its_view(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    patient: uuid.UUID,
    role: Role,
    view: str,
    allowed_keys: set[str],
    resources: set[str],
) -> None:
    resp = await client.get(f"/api/v1/patients/{patient}/chart", headers=staff[role].headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["view"] == view
    assert set(body) == allowed_keys
    flat = resp.text
    for secret in ("Chest clear", "Amlodipine", "Hypertension"):
        assert secret not in flat, f"{role} must not see {secret}"

    events = [
        e for e, _ in await access_events(admin_engine, clinic) if e.user_id == staff[role].user_id
    ]
    assert {e.resource_type.value for e in events} == resources
    assert len(events) == len(resources)


async def test_nurse_summary_has_no_contact_details(
    client: AsyncClient, staff: dict[Role, Actor], patient: uuid.UUID
) -> None:
    body = (
        await client.get(f"/api/v1/patients/{patient}/chart", headers=staff[Role.NURSE].headers)
    ).json()
    assert set(body["patient"]) == {"id", "mrn", "name", "dob", "sex"}
    assert body["allergies"][0]["substance"] == "Penicillin"


async def test_lab_worklist_shows_open_orders(
    client: AsyncClient, staff: dict[Role, Actor], patient: uuid.UUID
) -> None:
    body = (
        await client.get(f"/api/v1/patients/{patient}/chart", headers=staff[Role.LAB_TECH].headers)
    ).json()
    assert [o["tests"] for o in body["worklist"]] == [[{"code": "CBC"}]]


# Doctor and the reason flow -----------------------------------------------------------------------


async def test_doctor_with_appointment_sees_full_chart(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    patient: uuid.UUID,
) -> None:
    await book(client, staff, patient)
    resp = await client.get(f"/api/v1/patients/{patient}/chart", headers=staff[Role.DOCTOR].headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["view"] == "doctor"
    assert body["explanation"] == "T_APPT"
    assert body["notes"][0]["body"] == "Chest clear. Review in two weeks."
    assert body["prescriptions"][0]["items"][0]["drug"] == "Amlodipine 5 mg"
    events = [
        e
        for e, _ in await access_events(admin_engine, clinic)
        if e.user_id == staff[Role.DOCTOR].user_id
    ]
    assert len(events) == 7


async def test_unexplained_doctor_must_give_a_reason(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    patient: uuid.UUID,
) -> None:
    doctor = staff[Role.DOCTOR]
    blocked = await client.get(f"/api/v1/patients/{patient}/chart", headers=doctor.headers)
    assert blocked.status_code == 428
    assert blocked.json()["code"] == "reason-required"
    assert "Chest clear" not in blocked.text

    events = [
        (e, x) for e, x in await access_events(admin_engine, clinic) if e.user_id == doctor.user_id
    ]
    assert [(e.resource_type, e.decision) for e, _ in events] == [
        (ResourceType.DEMOGRAPHICS, AccessDecision.DENY)
    ]

    short = await client.post(
        f"/api/v1/patients/{patient}/chart",
        json={"reason_code": "covering_doctor", "reason_text": "x"},
        headers=doctor.headers,
    )
    assert short.status_code == 422

    opened = await client.post(
        f"/api/v1/patients/{patient}/chart",
        json={"reason_code": "covering_doctor", "reason_text": "Covering for Dr Demo today"},
        headers=doctor.headers,
    )
    assert opened.status_code == 200
    assert opened.json()["explanation"] == "T_REASON"
    allowed = [
        (e, x)
        for e, x in await access_events(admin_engine, clinic)
        if e.user_id == doctor.user_id and e.decision == AccessDecision.ALLOW
    ]
    assert len(allowed) == 7
    for event, explanation in allowed:
        assert event.decision == AccessDecision.ALLOW
        assert explanation is not None
        assert explanation.template_code == "T_REASON"
        assert explanation.strength == pytest.approx(0.3)
        assert explanation.evidence["reason"] == {
            "code": "covering_doctor",
            "text": "Covering for Dr Demo today",
        }


async def test_reason_endpoint_is_for_doctors_only(
    client: AsyncClient, staff: dict[Role, Actor], patient: uuid.UUID
) -> None:
    resp = await client.post(
        f"/api/v1/patients/{patient}/chart",
        json={"reason_code": "other", "reason_text": "just curious here"},
        headers=staff[Role.NURSE].headers,
    )
    assert resp.status_code == 403


# Break-glass -------------------------------------------------------------------------------------


async def start(client: AsyncClient, actor: Actor, patient: uuid.UUID) -> dict[str, Any]:
    resp = await client.post(
        f"/api/v1/patients/{patient}/break-glass",
        json={
            "reason_code": "unconscious",
            "reason_text": "Collapsed at reception, not responding",
        },
        headers=actor.headers,
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def test_break_glass_opens_the_emergency_view(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    patient: uuid.UUID,
) -> None:
    nurse = staff[Role.NURSE]
    # Without break-glass a nurse cannot see medicines.
    view = await start(client, nurse, patient)
    assert view["view"] == "emergency"
    assert [a["substance"] for a in view["allergies"]] == ["Penicillin"]
    assert [c["text"] for c in view["conditions"]] == ["Hypertension"]
    assert view["current_medicines"] == [{"drug": "Amlodipine 5 mg", "dose": "1-0-0", "days": 30}]
    assert view["last_vitals"]["pulse"] == 72
    assert "Chest clear" not in str(view)

    events = [
        (e, x) for e, x in await access_events(admin_engine, clinic) if e.user_id == nurse.user_id
    ]
    assert len(events) == 5
    for event, explanation in events:
        assert str(event.break_glass_id) == view["break_glass_id"]
        assert explanation is not None
        assert explanation.template_code == "T_BREAKGLASS"

    again = await client.get(
        f"/api/v1/patients/{patient}/emergency?break_glass_id={view['break_glass_id']}",
        headers=nurse.headers,
    )
    assert again.status_code == 200


async def test_break_glass_cannot_be_borrowed_or_reused_late(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    staff: dict[Role, Actor],
    patient: uuid.UUID,
) -> None:
    view = await start(client, staff[Role.DOCTOR], patient)
    url = f"/api/v1/patients/{patient}/emergency?break_glass_id={view['break_glass_id']}"

    borrowed = await client.get(url, headers=staff[Role.NURSE].headers)
    assert borrowed.status_code == 403
    assert borrowed.json()["code"] == "break-glass-expired"

    async with AsyncSession(admin_engine) as s, s.begin():
        await s.execute(
            update(m.BreakGlassEvent)
            .where(m.BreakGlassEvent.id == uuid.UUID(str(view["break_glass_id"])))
            .values(at=datetime.now(UTC) - timedelta(hours=5))
        )
    late = await client.get(url, headers=staff[Role.DOCTOR].headers)
    assert late.status_code == 403


async def test_other_roles_cannot_break_glass(
    client: AsyncClient, staff: dict[Role, Actor], patient: uuid.UUID
) -> None:
    resp = await client.post(
        f"/api/v1/patients/{patient}/break-glass",
        json={"reason_code": "emergency", "reason_text": "front desk emergency"},
        headers=staff[Role.RECEPTION].headers,
    )
    assert resp.status_code == 403


async def test_admin_reviews_break_glass(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    patient: uuid.UUID,
) -> None:
    view = await start(client, staff[Role.DOCTOR], patient)
    admin = staff[Role.CLINIC_ADMIN]

    queue = await client.get("/api/v1/break-glass?pending=true", headers=admin.headers)
    assert queue.status_code == 200
    (item,) = queue.json()
    assert item["id"] == view["break_glass_id"]
    assert item["user_role"] == "doctor"
    assert item["reason_code"] == "unconscious"
    assert item["outcome"] is None

    not_admin = await client.get("/api/v1/break-glass", headers=staff[Role.NURSE].headers)
    assert not_admin.status_code == 403

    reviewed = await client.post(
        f"/api/v1/break-glass/{item['id']}/review",
        json={"outcome": "benign", "note": "Genuine emergency, patient admitted"},
        headers=admin.headers,
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["outcome"] == "benign"
    assert reviewed.json()["reviewed_by"] == str(admin.user_id)

    twice = await client.post(
        f"/api/v1/break-glass/{item['id']}/review",
        json={"outcome": "misuse"},
        headers=admin.headers,
    )
    assert twice.status_code == 409
    assert (
        await client.get("/api/v1/break-glass?pending=true", headers=admin.headers)
    ).json() == []
    assert len((await client.get("/api/v1/break-glass", headers=admin.headers)).json()) == 1


async def test_admin_cannot_review_own_break_glass(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    patient: uuid.UUID,
) -> None:
    both = await make_actor(
        admin_engine, clinic, Role.CLINIC_ADMIN, user_id=staff[Role.DOCTOR].user_id
    )
    view = await start(client, staff[Role.DOCTOR], patient)
    resp = await client.post(
        f"/api/v1/break-glass/{view['break_glass_id']}/review",
        json={"outcome": "benign"},
        headers=both.headers,
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "own-break-glass"
