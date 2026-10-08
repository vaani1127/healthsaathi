import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db import models as m
from app.db.enums import Role
from app.identity import tokens
from app.notify.email import outbox
from tests.helpers import Actor, make_actor, make_clinic, make_patient


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
async def patient_actor(admin_engine: AsyncEngine, clinic: uuid.UUID) -> Actor:
    return await make_actor(admin_engine, clinic, Role.PATIENT)


@pytest.fixture
async def patient(
    admin_engine: AsyncEngine, clinic: uuid.UUID, staff: dict[Role, Actor], patient_actor: Actor
) -> uuid.UUID:
    return await make_patient(
        admin_engine, clinic, staff[Role.RECEPTION].user_id, patient_actor.user_id, name="Asha Demo"
    )


async def visit_with_token(
    client: AsyncClient, staff: dict[Role, Actor], patient: uuid.UUID
) -> int:
    appt = await client.post(
        "/api/v1/appointments",
        json={
            "patient_id": str(patient),
            "doctor_user_id": str(staff[Role.DOCTOR].user_id),
            "slot_start": datetime.now(UTC).isoformat(),
        },
        headers=staff[Role.RECEPTION].headers,
    )
    token = await client.post(
        "/api/v1/queue/tokens",
        json={"appointment_id": appt.json()["id"]},
        headers=staff[Role.RECEPTION].headers,
    )
    return int(token.json()["token_no"])


async def test_patient_sees_own_record_only_signed_and_released(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    patient_actor: Actor,
    patient: uuid.UUID,
) -> None:
    doctor = staff[Role.DOCTOR].user_id
    async with AsyncSession(admin_engine) as s, s.begin():
        enc = m.Encounter(
            clinic_id=clinic,
            patient_id=patient,
            doctor_user_id=doctor,
            started_at=datetime.now(UTC),
            created_by=doctor,
        )
        s.add(enc)
        await s.flush()
        s.add_all(
            [
                m.Prescription(
                    clinic_id=clinic,
                    patient_id=patient,
                    encounter_id=enc.id,
                    author_user_id=doctor,
                    items=[{"drug": "Signed"}],
                    signed_at=datetime.now(UTC),
                ),
                m.Prescription(
                    clinic_id=clinic,
                    patient_id=patient,
                    encounter_id=enc.id,
                    author_user_id=doctor,
                    items=[{"drug": "Draft"}],
                ),
                m.LabOrder(
                    clinic_id=clinic, patient_id=patient, ordered_by=doctor, tests=[{"code": "CBC"}]
                ),
            ]
        )

    me = await client.get("/api/v1/me/patient", headers=patient_actor.headers)
    assert me.json()["id"] == str(patient)
    chart = await client.get(f"/api/v1/patients/{patient}/chart", headers=patient_actor.headers)
    assert chart.status_code == 200
    body = chart.json()
    assert body["view"] == "patient"
    assert [p["items"][0]["drug"] for p in body["prescriptions"]] == ["Signed"]
    assert body["lab_orders"] == []

    other = await make_patient(admin_engine, clinic, staff[Role.RECEPTION].user_id)
    assert (
        await client.get(f"/api/v1/patients/{other}/chart", headers=patient_actor.headers)
    ).status_code == 403


async def test_me_patient_without_a_record(
    client: AsyncClient, admin_engine: AsyncEngine, clinic: uuid.UUID
) -> None:
    lonely = await make_actor(admin_engine, clinic, Role.PATIENT)
    resp = await client.get("/api/v1/me/patient", headers=lonely.headers)
    assert resp.status_code == 404


async def test_access_log_explains_with_token_and_time(
    client: AsyncClient,
    staff: dict[Role, Actor],
    patient_actor: Actor,
    patient: uuid.UUID,
) -> None:
    token_no = await visit_with_token(client, staff, patient)
    await client.get(f"/api/v1/patients/{patient}/chart", headers=staff[Role.DOCTOR].headers)
    # The patient's own views never show up in their log.
    await client.get(f"/api/v1/patients/{patient}/chart", headers=patient_actor.headers)

    log = await client.get(
        f"/api/v1/patients/{patient}/access-log?limit=100", headers=patient_actor.headers
    )
    assert log.status_code == 200
    items = log.json()["items"]
    assert all(i["role"] != "patient" for i in items)
    notes = next(i for i in items if i["resource"] == "notes")
    assert notes["user_name"] == "doctor actor"
    assert notes["because"]["template"] == "T_APPT"
    assert notes["because"]["kind"] == "appointment"
    assert notes["because"]["token_no"] == token_no
    assert notes["because"]["slot_start"] is not None
    assert notes["query_status"] is None
    front = next(i for i in items if i["role"] == "reception")
    assert front["because"]["template"] == "T_FRONTDESK"


async def test_access_log_pages(
    client: AsyncClient, staff: dict[Role, Actor], patient_actor: Actor, patient: uuid.UUID
) -> None:
    await visit_with_token(client, staff, patient)
    await client.get(f"/api/v1/patients/{patient}/chart", headers=staff[Role.DOCTOR].headers)
    first = (
        await client.get(
            f"/api/v1/patients/{patient}/access-log?limit=3", headers=patient_actor.headers
        )
    ).json()
    second = (
        await client.get(
            f"/api/v1/patients/{patient}/access-log?limit=3&cursor={first['next_cursor']}",
            headers=patient_actor.headers,
        )
    ).json()
    ids = [i["id"] for i in first["items"] + second["items"]]
    assert len(ids) == len(set(ids)) == 6


async def test_patient_reports_an_access_and_admin_closes_it(
    client: AsyncClient, staff: dict[Role, Actor], patient_actor: Actor, patient: uuid.UUID
) -> None:
    await client.get(f"/api/v1/patients/{patient}", headers=staff[Role.NURSE].headers)
    log = (
        await client.get(f"/api/v1/patients/{patient}/access-log", headers=patient_actor.headers)
    ).json()
    event = log["items"][0]
    assert event["because"]["template"] is None

    report = await client.post(
        f"/api/v1/access-events/{event['id']}/query",
        json={"message": "I do not know this nurse"},
        headers=patient_actor.headers,
    )
    assert report.status_code == 201
    again = await client.post(
        f"/api/v1/access-events/{event['id']}/query",
        json={"message": "again"},
        headers=patient_actor.headers,
    )
    assert again.status_code == 409

    log = (
        await client.get(f"/api/v1/patients/{patient}/access-log", headers=patient_actor.headers)
    ).json()
    assert next(i for i in log["items"] if i["id"] == event["id"])["query_status"] == "open"

    admin = staff[Role.CLINIC_ADMIN]
    queue = (await client.get("/api/v1/access-queries", headers=admin.headers)).json()
    assert [q["access_event_id"] for q in queue] == [event["id"]]
    closed = await client.patch(
        f"/api/v1/access-queries/{queue[0]['id']}", json={"status": "closed"}, headers=admin.headers
    )
    assert closed.json()["status"] == "closed"
    assert (await client.get("/api/v1/access-queries", headers=admin.headers)).json() == []


async def test_patient_cannot_report_someone_elses_access(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    patient_actor: Actor,
) -> None:
    other = await make_patient(admin_engine, clinic, staff[Role.RECEPTION].user_id)
    await client.get(f"/api/v1/patients/{other}", headers=staff[Role.NURSE].headers)
    admin_log = (
        await client.get(
            f"/api/v1/patients/{other}/access-log", headers=staff[Role.CLINIC_ADMIN].headers
        )
    ).json()
    resp = await client.post(
        f"/api/v1/access-events/{admin_log['items'][0]['id']}/query",
        json={"message": "not mine"},
        headers=patient_actor.headers,
    )
    assert resp.status_code == 403
    assert (
        await client.get(f"/api/v1/patients/{other}/access-log", headers=patient_actor.headers)
    ).status_code == 403


async def test_export_contains_record_consents_and_log(
    client: AsyncClient, staff: dict[Role, Actor], patient_actor: Actor, patient: uuid.UUID
) -> None:
    await client.get(f"/api/v1/patients/{patient}", headers=staff[Role.NURSE].headers)
    resp = await client.get(f"/api/v1/patients/{patient}/export", headers=patient_actor.headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["record"]["patient"]["id"] == str(patient)
    assert body["consents"] == []
    assert any(e["role"] == "nurse" for e in body["access_log"])


async def test_reception_gives_portal_access_and_patient_signs_in(
    client: AsyncClient, admin_engine: AsyncEngine, clinic: uuid.UUID, staff: dict[Role, Actor]
) -> None:
    patient = await make_patient(
        admin_engine, clinic, staff[Role.RECEPTION].user_id, name="Ravi Demo"
    )
    email = f"ravi.{uuid.uuid4().hex[:8]}@portal.test"
    resp = await client.post(
        f"/api/v1/patients/{patient}/portal-access",
        json={"email": email},
        headers=staff[Role.RECEPTION].headers,
    )
    assert resp.status_code == 200
    assert outbox.last_to(email) is not None

    await client.post("/api/v1/auth/otp/request", json={"email": email})
    sent = outbox.last_to(email)
    assert sent is not None
    code = next(w for w in sent.text.replace(".", " ").split() if w.isdigit() and len(w) == 6)
    session = await client.post("/api/v1/auth/otp/verify", json={"email": email, "code": code})
    assert session.status_code == 200
    scoped = await client.post(
        "/api/v1/auth/select-clinic",
        json={"clinic_id": str(clinic), "role": "patient"},
        headers={"authorization": f"Bearer {session.json()['access_token']}"},
    )
    me = await client.get(
        "/api/v1/me/patient", headers={"authorization": f"Bearer {scoped.json()['access_token']}"}
    )
    assert me.json()["id"] == str(patient)

    other = await make_patient(admin_engine, clinic, staff[Role.RECEPTION].user_id)
    clash = await client.post(
        f"/api/v1/patients/{other}/portal-access",
        json={"email": email},
        headers=staff[Role.RECEPTION].headers,
    )
    assert clash.status_code == 409


# Staff management ---------------------------------------------------------------------------------


async def test_admin_lists_and_deactivates_staff(
    client: AsyncClient, staff: dict[Role, Actor]
) -> None:
    admin, nurse = staff[Role.CLINIC_ADMIN], staff[Role.NURSE]
    members = (await client.get("/api/v1/staff/members", headers=admin.headers)).json()
    assert {m["role"] for m in members} == {
        "reception",
        "nurse",
        "doctor",
        "lab_tech",
        "clinic_admin",
    }
    nurse_row = next(m for m in members if m["user_id"] == str(nurse.user_id))

    resp = await client.patch(
        f"/api/v1/staff/members/{nurse_row['membership_id']}",
        json={"is_active": False},
        headers=admin.headers,
    )
    assert resp.status_code == 204
    assert (await client.get("/api/v1/queue", headers=nurse.headers)).status_code == 401

    me_row = next(m for m in members if m["user_id"] == str(admin.user_id))
    self_off = await client.patch(
        f"/api/v1/staff/members/{me_row['membership_id']}",
        json={"is_active": False},
        headers=admin.headers,
    )
    assert self_off.status_code == 409
    assert (await client.get("/api/v1/staff/members", headers=nurse.headers)).status_code in (
        401,
        403,
    )


async def test_deactivated_member_cannot_select_clinic(
    client: AsyncClient, staff: dict[Role, Actor], clinic: uuid.UUID
) -> None:
    admin, doctor = staff[Role.CLINIC_ADMIN], staff[Role.DOCTOR]
    members = (await client.get("/api/v1/staff/members", headers=admin.headers)).json()
    row = next(m for m in members if m["user_id"] == str(doctor.user_id))
    await client.patch(
        f"/api/v1/staff/members/{row['membership_id']}",
        json={"is_active": False},
        headers=admin.headers,
    )
    from app.identity import service

    issued = await service.create_session(doctor.user_id, "mfa", service.DeviceInfo(None, None))
    token, _ = tokens.access_token(doctor.user_id, issued.session_id, ["pwd", "otp"])
    resp = await client.post(
        "/api/v1/auth/select-clinic",
        json={"clinic_id": str(clinic), "role": "doctor"},
        headers={"authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
