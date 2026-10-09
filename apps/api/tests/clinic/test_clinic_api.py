import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.access.policy import get_policy
from app.access.service import access_session, record_and_explain
from app.core.errors import ProblemError
from app.db import models as m
from app.db.enums import AccessAction, AccessDecision, ResourceType, Role
from app.startup import ensure_partitions, register_policy_version
from tests.helpers import Actor, access_events, count_rows, make_actor, make_clinic, make_patient


@pytest.fixture
async def clinic(admin_engine: AsyncEngine) -> uuid.UUID:
    return await make_clinic(admin_engine)


@pytest.fixture
async def staff(admin_engine: AsyncEngine, clinic: uuid.UUID) -> dict[Role, Actor]:
    return {
        role: await make_actor(admin_engine, clinic, role)
        for role in (Role.RECEPTION, Role.NURSE, Role.DOCTOR, Role.LAB_TECH, Role.CLINIC_ADMIN)
    }


def soon(minutes: int = 0) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()


def later_today(minutes: int) -> str:
    """Like soon(), but never past the end of the clinic's local day (Asia/Kolkata)."""
    now = datetime.now(UTC)
    local = now.astimezone(ZoneInfo("Asia/Kolkata"))
    last = local.replace(hour=23, minute=59, second=0, microsecond=0).astimezone(UTC)
    return max(min(now + timedelta(minutes=minutes), last), now).isoformat()


async def new_patient(client: AsyncClient, reception: Actor, name: str = "Meena Demo") -> str:
    resp = await client.post(
        "/api/v1/patients", json={"name": name, "sex": "female"}, headers=reception.headers
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def book(
    client: AsyncClient,
    actor: Actor,
    patient_id: str,
    doctor: Actor,
    minutes: int = 0,
    slot: str | None = None,
) -> dict[str, Any]:
    resp = await client.post(
        "/api/v1/appointments",
        json={
            "patient_id": patient_id,
            "doctor_user_id": str(doctor.user_id),
            "slot_start": slot or soon(minutes),
        },
        headers=actor.headers,
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


# Explanations through the API ---------------------------------------------------------------------


async def test_registration_is_recorded_and_explained(
    client: AsyncClient, admin_engine: AsyncEngine, clinic: uuid.UUID, staff: dict[Role, Actor]
) -> None:
    patient_id = await new_patient(client, staff[Role.RECEPTION])
    events = await access_events(admin_engine, clinic)
    assert len(events) == 1
    event, explanation = events[0]
    assert str(event.patient_id) == patient_id
    assert (event.resource_type, event.action, event.decision) == (
        ResourceType.DEMOGRAPHICS,
        AccessAction.CREATE,
        AccessDecision.ALLOW,
    )
    assert event.policy_version == get_policy().sha256
    assert event.session_id is not None
    assert event.device_id is not None
    assert explanation is not None
    assert explanation.template_code == "T_FRONTDESK"
    assert explanation.evidence["refs"][0]["kind"] == "registration"
    assert await count_rows(admin_engine, m.AuditEvent, clinic) == 1


async def test_nurse_without_a_token_is_recorded_as_unexplained(
    client: AsyncClient, admin_engine: AsyncEngine, clinic: uuid.UUID, staff: dict[Role, Actor]
) -> None:
    patient_id = await make_patient(admin_engine, clinic, staff[Role.RECEPTION].user_id)
    resp = await client.get(f"/api/v1/patients/{patient_id}", headers=staff[Role.NURSE].headers)
    assert resp.status_code == 200
    ((event, explanation),) = await access_events(admin_engine, clinic)
    assert event.decision == AccessDecision.ALLOW
    assert explanation is not None
    assert explanation.template_code is None
    assert explanation.strength == 0


async def test_opd_flow_explains_each_role(
    client: AsyncClient, admin_engine: AsyncEngine, clinic: uuid.UUID, staff: dict[Role, Actor]
) -> None:
    reception, nurse, doctor = staff[Role.RECEPTION], staff[Role.NURSE], staff[Role.DOCTOR]
    patient_id = await new_patient(client, reception)
    appointment = await book(client, reception, patient_id, doctor)

    token = await client.post(
        "/api/v1/queue/tokens",
        json={"appointment_id": appointment["id"]},
        headers=reception.headers,
    )
    assert token.status_code == 201
    assert token.json()["token_no"] == 1

    assigned = await client.patch(
        f"/api/v1/queue/tokens/{token.json()['id']}",
        json={"assigned_nurse_user_id": str(nurse.user_id), "status": "with_nurse"},
        headers=reception.headers,
    )
    assert assigned.status_code == 200

    queue = await client.get("/api/v1/queue", headers=nurse.headers)
    assert [q["patient"]["id"] for q in queue.json()] == [patient_id]

    chart = await client.get(f"/api/v1/patients/{patient_id}", headers=doctor.headers)
    assert chart.status_code == 200

    by_user: dict[uuid.UUID, list[str | None]] = {}
    for event, explanation in await access_events(admin_engine, clinic):
        assert explanation is not None
        by_user.setdefault(event.user_id, []).append(explanation.template_code)
    assert set(by_user[reception.user_id]) == {"T_FRONTDESK"}
    assert by_user[nurse.user_id] == ["T_QUEUE"]
    assert by_user[doctor.user_id] == ["T_APPT"]


async def test_patient_reads_only_own_record(
    client: AsyncClient, admin_engine: AsyncEngine, clinic: uuid.UUID, staff: dict[Role, Actor]
) -> None:
    patient_actor = await make_actor(admin_engine, clinic, Role.PATIENT)
    own = await make_patient(
        admin_engine, clinic, staff[Role.RECEPTION].user_id, patient_actor.user_id
    )
    other = await make_patient(admin_engine, clinic, staff[Role.RECEPTION].user_id)

    assert (
        await client.get(f"/api/v1/patients/{own}", headers=patient_actor.headers)
    ).status_code == 200
    denied = await client.get(f"/api/v1/patients/{other}", headers=patient_actor.headers)
    assert denied.status_code == 403

    events = await access_events(admin_engine, clinic)
    decisions = {(e.patient_id, e.decision): x.template_code if x else None for e, x in events}
    assert decisions[(own, AccessDecision.ALLOW)] == "T_SELF"
    assert (other, AccessDecision.DENY) in decisions


# Denials ------------------------------------------------------------------------------------------


async def test_reception_reading_notes_is_denied_and_logged(
    admin_engine: AsyncEngine, clinic: uuid.UUID, staff: dict[Role, Actor]
) -> None:
    reception = staff[Role.RECEPTION]
    patient_id = await make_patient(admin_engine, clinic, reception.user_id)
    with pytest.raises(ProblemError) as exc:
        async with access_session(reception.principal) as db:
            await record_and_explain(
                db, reception.principal, patient_id, ResourceType.NOTES, AccessAction.VIEW
            )
    assert exc.value.status == 403

    ((event, explanation),) = await access_events(admin_engine, clinic)
    assert event.decision == AccessDecision.DENY
    assert event.resource_type == ResourceType.NOTES
    assert explanation is not None
    assert explanation.template_code is None
    assert await count_rows(admin_engine, m.AuditEvent, clinic) == 1


async def test_denial_after_an_allowed_access_does_not_deadlock(
    admin_engine: AsyncEngine, clinic: uuid.UUID, staff: dict[Role, Actor]
) -> None:
    """The first access takes the audit lock; the denial is written after that rollback."""
    nurse = staff[Role.NURSE]
    patient_id = await make_patient(admin_engine, clinic, staff[Role.RECEPTION].user_id)
    with pytest.raises(ProblemError):
        async with access_session(nurse.principal) as db:
            await record_and_explain(
                db, nurse.principal, patient_id, ResourceType.VITALS, AccessAction.VIEW
            )
            await record_and_explain(
                db, nurse.principal, patient_id, ResourceType.NOTES, AccessAction.VIEW
            )

    events = await access_events(admin_engine, clinic)
    # The allowed read rolled back with the failed request; only the denial remains.
    assert [(e.resource_type, e.decision) for e, _ in events] == [
        (ResourceType.NOTES, AccessDecision.DENY)
    ]


async def test_break_glass_only_needs_break_glass(
    admin_engine: AsyncEngine, clinic: uuid.UUID, staff: dict[Role, Actor]
) -> None:
    nurse = staff[Role.NURSE]
    assert (
        get_policy().decide(Role.NURSE, ResourceType.PRESCRIPTIONS, AccessAction.VIEW).allowed
        is False
    )
    assert (
        get_policy()
        .decide(Role.NURSE, ResourceType.PRESCRIPTIONS, AccessAction.VIEW, break_glass=True)
        .allowed
    )
    patient_id = await make_patient(admin_engine, clinic, staff[Role.RECEPTION].user_id)
    with pytest.raises(ProblemError):
        async with access_session(nurse.principal) as db:
            await record_and_explain(
                db, nurse.principal, patient_id, ResourceType.PRESCRIPTIONS, AccessAction.VIEW
            )


async def test_unknown_patient_is_404_and_not_logged(
    client: AsyncClient, admin_engine: AsyncEngine, clinic: uuid.UUID, staff: dict[Role, Actor]
) -> None:
    resp = await client.get(f"/api/v1/patients/{uuid.uuid4()}", headers=staff[Role.DOCTOR].headers)
    assert resp.status_code == 404
    assert await access_events(admin_engine, clinic) == []


async def test_other_clinics_patient_is_not_found(
    client: AsyncClient, admin_engine: AsyncEngine, clinic: uuid.UUID, staff: dict[Role, Actor]
) -> None:
    other_clinic = await make_clinic(admin_engine)
    other_reception = await make_actor(admin_engine, other_clinic, Role.RECEPTION)
    foreign = await make_patient(admin_engine, other_clinic, other_reception.user_id)
    resp = await client.get(f"/api/v1/patients/{foreign}", headers=staff[Role.DOCTOR].headers)
    assert resp.status_code == 404
    assert await access_events(admin_engine, other_clinic) == []


# Exactly one access event per patient per call ----------------------------------------------------

Call = Callable[[AsyncClient, dict[Role, Actor], dict[str, Any]], Awaitable[Any]]


async def _setup_flow(client: AsyncClient, staff: dict[Role, Actor]) -> dict[str, Any]:
    reception, doctor = staff[Role.RECEPTION], staff[Role.DOCTOR]
    p1 = await new_patient(client, reception, "Asha Demo")
    p2 = await new_patient(client, reception, "Asha Demo Two")
    # On the clinic's local day, so "today's appointments" lists it whatever the time of day.
    appt = await book(client, reception, p1, doctor, slot=later_today(60))
    token = await client.post(
        "/api/v1/queue/tokens", json={"appointment_id": appt["id"]}, headers=reception.headers
    )
    walkin = await client.post(
        "/api/v1/walkins",
        json={"patient_id": p2, "doctor_user_id": str(doctor.user_id)},
        headers=reception.headers,
    )
    care = await client.post(
        "/api/v1/care-team",
        json={"patient_id": p1, "user_id": str(staff[Role.NURSE].user_id), "role": "nurse"},
        headers=staff[Role.CLINIC_ADMIN].headers,
    )
    return {
        "p1": p1,
        "p2": p2,
        "appt": appt,
        "token": token.json(),
        "walkin": walkin.json(),
        "care": care.json(),
    }


CASES: list[tuple[str, Call, int]] = [
    (
        "create patient",
        lambda c, s, d: c.post(
            "/api/v1/patients",
            json={"name": "Ravi Demo", "sex": "male"},
            headers=s[Role.RECEPTION].headers,
        ),
        1,
    ),
    (
        "get patient",
        lambda c, s, d: c.get(f"/api/v1/patients/{d['p1']}", headers=s[Role.DOCTOR].headers),
        1,
    ),
    (
        "update patient",
        lambda c, s, d: c.patch(
            f"/api/v1/patients/{d['p1']}",
            json={"phone": "+91-00000-11111"},
            headers=s[Role.RECEPTION].headers,
        ),
        1,
    ),
    (
        "search patients",
        lambda c, s, d: c.get("/api/v1/patients?q=Asha", headers=s[Role.RECEPTION].headers),
        2,
    ),
    (
        "book appointment",
        lambda c, s, d: c.post(
            "/api/v1/appointments",
            json={
                "patient_id": d["p2"],
                "doctor_user_id": str(s[Role.DOCTOR].user_id),
                "slot_start": soon(180),
            },
            headers=s[Role.RECEPTION].headers,
        ),
        1,
    ),
    (
        "list appointments",
        lambda c, s, d: c.get("/api/v1/appointments", headers=s[Role.DOCTOR].headers),
        2,
    ),
    (
        "update appointment",
        lambda c, s, d: c.patch(
            f"/api/v1/appointments/{d['appt']['id']}",
            json={"status": "cancelled"},
            headers=s[Role.RECEPTION].headers,
        ),
        1,
    ),
    ("queue", lambda c, s, d: c.get("/api/v1/queue", headers=s[Role.NURSE].headers), 2),
    (
        "update token",
        lambda c, s, d: c.patch(
            f"/api/v1/queue/tokens/{d['token']['id']}",
            json={"status": "with_nurse"},
            headers=s[Role.NURSE].headers,
        ),
        1,
    ),
    (
        "care team list",
        lambda c, s, d: c.get(
            f"/api/v1/patients/{d['p1']}/care-team", headers=s[Role.DOCTOR].headers
        ),
        1,
    ),
    (
        "care team end",
        lambda c, s, d: c.post(
            f"/api/v1/care-team/{d['care']['id']}/end", headers=s[Role.CLINIC_ADMIN].headers
        ),
        1,
    ),
    ("staff directory", lambda c, s, d: c.get("/api/v1/staff", headers=s[Role.NURSE].headers), 0),
    ("schedules", lambda c, s, d: c.get("/api/v1/schedules", headers=s[Role.NURSE].headers), 0),
]


@pytest.mark.parametrize(("name", "call", "expected"), CASES, ids=[c[0] for c in CASES])
async def test_each_endpoint_records_one_event_per_patient(
    name: str,
    call: Call,
    expected: int,
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
) -> None:
    data = await _setup_flow(client, staff)
    before_events = await count_rows(admin_engine, m.AccessEvent, clinic)
    before_audit = await count_rows(admin_engine, m.AuditEvent, clinic)

    resp = await call(client, staff, data)
    assert resp.status_code < 300, resp.text

    events = await access_events(admin_engine, clinic)
    new = events[before_events:]
    assert len(new) == expected
    assert all(x is not None for _, x in new), "every access event has an explanation"
    assert len({e.patient_id for e, _ in new}) == expected, "one event per patient"
    assert await count_rows(admin_engine, m.AuditEvent, clinic) == before_audit + expected


# Validation and roles -----------------------------------------------------------------------------


async def test_roles_are_enforced(client: AsyncClient, staff: dict[Role, Actor]) -> None:
    nurse_create = await client.post(
        "/api/v1/patients",
        json={"name": "No Way", "sex": "male"},
        headers=staff[Role.NURSE].headers,
    )
    assert nurse_create.status_code == 403
    schedule = await client.post(
        "/api/v1/schedules",
        json={
            "doctor_user_id": str(staff[Role.DOCTOR].user_id),
            "weekday": 0,
            "start_time": "09:00",
            "end_time": "13:00",
        },
        headers=staff[Role.RECEPTION].headers,
    )
    assert schedule.status_code == 403


async def test_schedules_shifts_and_staff(client: AsyncClient, staff: dict[Role, Actor]) -> None:
    admin, doctor, nurse = staff[Role.CLINIC_ADMIN], staff[Role.DOCTOR], staff[Role.NURSE]
    created = await client.post(
        "/api/v1/schedules",
        json={
            "doctor_user_id": str(doctor.user_id),
            "weekday": 2,
            "start_time": "09:00",
            "end_time": "13:00",
            "slot_minutes": 20,
        },
        headers=admin.headers,
    )
    assert created.status_code == 201
    bad = await client.post(
        "/api/v1/schedules",
        json={
            "doctor_user_id": str(nurse.user_id),
            "weekday": 2,
            "start_time": "09:00",
            "end_time": "13:00",
        },
        headers=admin.headers,
    )
    assert bad.status_code == 422
    paused = await client.patch(
        f"/api/v1/schedules/{created.json()['id']}",
        json={"status": "inactive"},
        headers=admin.headers,
    )
    assert paused.json()["status"] == "inactive"
    listed = await client.get(
        f"/api/v1/schedules?doctor_user_id={doctor.user_id}", headers=nurse.headers
    )
    assert len(listed.json()) == 1

    start = datetime.now(UTC)
    shift = await client.post(
        "/api/v1/shifts",
        json={
            "user_id": str(nurse.user_id),
            "role": "nurse",
            "starts_at": start.isoformat(),
            "ends_at": (start + timedelta(hours=8)).isoformat(),
        },
        headers=admin.headers,
    )
    assert shift.status_code == 201
    listed_shifts = await client.get(
        "/api/v1/shifts",
        params={"start": (start - timedelta(hours=1)).isoformat()},
        headers=nurse.headers,
    )
    assert [s["id"] for s in listed_shifts.json()] == [shift.json()["id"]]
    cancelled = await client.post(
        f"/api/v1/shifts/{shift.json()['id']}/cancel", headers=admin.headers
    )
    assert cancelled.json()["status"] == "cancelled"

    directory = await client.get("/api/v1/staff", headers=nurse.headers)
    assert {s["role"] for s in directory.json()} >= {"doctor", "nurse", "reception"}


async def test_appointment_rules(client: AsyncClient, staff: dict[Role, Actor]) -> None:
    reception, doctor = staff[Role.RECEPTION], staff[Role.DOCTOR]
    patient_id = await new_patient(client, reception)
    first = await book(client, reception, patient_id, doctor, minutes=300)
    assert first["source"] == "reception"
    clash = await client.post(
        "/api/v1/appointments",
        json={
            "patient_id": patient_id,
            "doctor_user_id": str(doctor.user_id),
            "slot_start": first["slot_start"],
        },
        headers=reception.headers,
    )
    assert clash.status_code == 409

    not_doctor = await client.post(
        "/api/v1/appointments",
        json={
            "patient_id": patient_id,
            "doctor_user_id": str(staff[Role.NURSE].user_id),
            "slot_start": soon(400),
        },
        headers=reception.headers,
    )
    assert not_doctor.status_code == 422

    by_doctor = await book(client, doctor, patient_id, doctor, minutes=600)
    assert by_doctor["source"] == "doctor"

    done = await client.patch(
        f"/api/v1/appointments/{first['id']}",
        json={"status": "completed"},
        headers=reception.headers,
    )
    assert done.status_code == 409
    assert done.json()["code"] == "invalid-transition"


async def test_queue_numbers_and_transitions(client: AsyncClient, staff: dict[Role, Actor]) -> None:
    reception, doctor, nurse = staff[Role.RECEPTION], staff[Role.DOCTOR], staff[Role.NURSE]
    tokens = []
    for i in range(3):
        patient_id = await new_patient(client, reception, f"Queue Demo {i}")
        walkin = await client.post(
            "/api/v1/walkins",
            json={"patient_id": patient_id, "doctor_user_id": str(doctor.user_id)},
            headers=reception.headers,
        )
        assert walkin.status_code == 201
        tokens.append(walkin.json()["token"])
    assert [t["token_no"] for t in tokens] == [1, 2, 3]

    t = tokens[0]["id"]
    bad = await client.patch(
        f"/api/v1/queue/tokens/{t}", json={"status": "done"}, headers=nurse.headers
    )
    assert bad.status_code == 409
    for status in ("with_nurse", "with_doctor", "done"):
        ok = await client.patch(
            f"/api/v1/queue/tokens/{t}", json={"status": status}, headers=doctor.headers
        )
        assert ok.status_code == 200, ok.text
    appointments = await client.get("/api/v1/appointments", headers=reception.headers)
    statuses = {a["id"]: a["status"] for a in appointments.json()}
    assert statuses[tokens[0]["appointment_id"]] == "completed"

    again = await client.post(
        "/api/v1/queue/tokens",
        json={"appointment_id": tokens[1]["appointment_id"]},
        headers=reception.headers,
    )
    assert again.status_code == 409


async def test_search_pages_with_cursor(client: AsyncClient, staff: dict[Role, Actor]) -> None:
    reception = staff[Role.RECEPTION]
    for i in range(5):
        await new_patient(client, reception, f"Cursor Demo {i}")
    first = await client.get("/api/v1/patients?q=Cursor&limit=3", headers=reception.headers)
    body = first.json()
    assert len(body["items"]) == 3
    assert body["next_cursor"]
    second = await client.get(
        f"/api/v1/patients?q=Cursor&limit=3&cursor={body['next_cursor']}", headers=reception.headers
    )
    names = [p["name"] for p in body["items"] + second.json()["items"]]
    assert names == sorted(names)
    assert len(set(names)) == 5
    assert second.json()["next_cursor"] is None
    bad = await client.get("/api/v1/patients?cursor=nonsense", headers=reception.headers)
    assert bad.status_code == 400


async def test_mrn_is_sequential_per_clinic(client: AsyncClient, staff: dict[Role, Actor]) -> None:
    reception = staff[Role.RECEPTION]
    ids = [await new_patient(client, reception, f"Mrn Demo {i}") for i in range(2)]
    mrns = [
        (await client.get(f"/api/v1/patients/{i}", headers=reception.headers)).json()["mrn"]
        for i in ids
    ]
    assert mrns == ["P000001", "P000002"]


# Policy and startup -------------------------------------------------------------------------------


async def test_policy_version_is_registered(admin_engine: AsyncEngine) -> None:
    sha = await register_policy_version()
    await register_policy_version()
    async with AsyncSession(admin_engine) as s:
        rows = (await s.scalars(select(m.PolicyVersion).where(m.PolicyVersion.sha256 == sha))).all()
    assert len(rows) == 1
    assert rows[0].yaml == get_policy().yaml_text
    assert await ensure_partitions() == 0


def test_policy_defaults_to_deny() -> None:
    policy = get_policy()
    assert policy.decide(Role.RECEPTION, ResourceType.NOTES, AccessAction.VIEW).allowed is False
    assert policy.decide(Role.LAB_TECH, ResourceType.BILLING, AccessAction.VIEW).allowed is False
    assert policy.decide(Role.DOCTOR, ResourceType.NOTES, AccessAction.VIEW).allowed
    assert len(policy.sha256) == 64


async def test_current_clinic(
    client: AsyncClient, staff: dict[Role, Actor], clinic: uuid.UUID
) -> None:
    resp = await client.get("/api/v1/clinic", headers=staff[Role.NURSE].headers)
    assert resp.json()["id"] == str(clinic)
    assert resp.json()["timezone"] == "Asia/Kolkata"


async def test_e2e_fixture_script(tmp_path: Any) -> None:
    from app.scripts.e2e_fixture import build

    fixture = await build(tmp_path / "f.json")
    users = fixture["users"]
    assert isinstance(users, list)
    assert {u["role"] for u in users} == {
        "clinic_admin",
        "reception",
        "nurse",
        "doctor",
        "lab_tech",
    }
    from app.identity import service

    reception = next(u for u in users if u["role"] == "reception")
    challenge = await service.login_with_password(reception["email"], str(fixture["password"]))
    assert challenge.status == "mfa_required"
