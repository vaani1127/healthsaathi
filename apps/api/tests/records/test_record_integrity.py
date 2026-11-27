"""Record integrity (ADR 0011): versions written with each clinical write, the integrity endpoint
and its verdicts, the admin records line, and the backfill."""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.clinical.notes_crypto import encrypt_body
from app.core.db import tenant_session
from app.db import models as m
from app.db.enums import AnchorBackend, AnchorStatus, RecordType, Role
from app.ledger.checkpoint import build_checkpoint
from app.ledger.records import commitment, write_version
from app.scripts.backfill_record_versions import backfill_clinic, count_pending
from e2d_core.ledger import TreeHead, leaf_hash, payload_hash, verify, verify_inclusion
from tests.helpers import Actor, make_actor, make_clinic, make_patient

ITEMS = [{"drug": "Paracetamol", "strength": "500 mg", "dose": "1-1-1", "duration_days": 3}]
PDF = b"%PDF-1.4 synthetic report"


@pytest.fixture
async def clinic(admin_engine: AsyncEngine) -> uuid.UUID:
    return await make_clinic(admin_engine)


@pytest.fixture
async def staff(admin_engine: AsyncEngine, clinic: uuid.UUID) -> dict[Role, Actor]:
    return {
        role: await make_actor(admin_engine, clinic, role)
        for role in (
            Role.RECEPTION,
            Role.NURSE,
            Role.DOCTOR,
            Role.LAB_TECH,
            Role.CLINIC_ADMIN,
            Role.PATIENT,
        )
    }


@pytest.fixture
async def visit(
    client: AsyncClient, admin_engine: AsyncEngine, clinic: uuid.UUID, staff: dict[Role, Actor]
) -> dict[str, str]:
    """A patient (with a portal account) with today's appointment and an open consultation."""
    patient = await make_patient(
        admin_engine, clinic, staff[Role.RECEPTION].user_id, staff[Role.PATIENT].user_id
    )
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
    return {"patient": str(patient), "encounter": enc.json()["id"]}


async def versions(engine: AsyncEngine, clinic: uuid.UUID) -> list[m.RecordVersion]:
    async with AsyncSession(engine) as s:
        rows = await s.scalars(
            select(m.RecordVersion)
            .where(m.RecordVersion.clinic_id == clinic)
            .order_by(m.RecordVersion.created_at, m.RecordVersion.version)
        )
        return list(rows.all())


async def leaves(engine: AsyncEngine, clinic: uuid.UUID) -> dict[int, dict[str, Any]]:
    async with AsyncSession(engine) as s:
        rows = await s.execute(
            select(m.AuditEvent.seq, m.AuditEvent.payload).where(
                m.AuditEvent.clinic_id == clinic, m.AuditEvent.kind == "record_version"
            )
        )
        return {int(seq): payload for seq, payload in rows.all()}


async def audit_total(engine: AsyncEngine, clinic: uuid.UUID) -> int:
    async with AsyncSession(engine) as s:
        return int(
            await s.scalar(
                select(func.count())
                .select_from(m.AuditEvent)
                .where(m.AuditEvent.clinic_id == clinic)
            )
            or 0
        )


COUNT_KEYS = ("records", "match", "pending", "mismatch", "missing_version", "missing_row")
COUNT_KEYS += ("backfilled",)


async def records_status(client: AsyncClient, admin: Actor) -> Any:
    resp = await client.get("/api/v1/audit/records-status", headers=admin.headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def check(client: AsyncClient, actor: Actor, record_type: str, record_id: str) -> Any:
    return await client.get(
        f"/api/v1/records/{record_type}/{record_id}/integrity", headers=actor.headers
    )


async def anchor(engine: AsyncEngine, clinic: uuid.UUID) -> None:
    """Checkpoint the log and mark the checkpoint confirmed on Amoy (no chain in unit tests)."""
    cp = await build_checkpoint(clinic)
    async with AsyncSession(engine) as s, s.begin():
        s.add(
            m.AnchorReceipt(
                clinic_id=clinic,
                checkpoint_id=cp.checkpoint.id,
                backend=AnchorBackend.AMOY,
                status=AnchorStatus.CONFIRMED,
                tx_ref="0x" + "cd" * 32,
                submitted_at=datetime.now(UTC),
            )
        )


async def add_vitals(client: AsyncClient, staff: dict[Role, Actor], patient: str) -> str:
    resp = await client.post(
        f"/api/v1/patients/{patient}/vitals",
        json={"pulse": 80, "temp_c": "37.2", "weight_kg": "61.5"},
        headers=staff[Role.NURSE].headers,
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def add_allergy(client: AsyncClient, staff: dict[Role, Actor], patient: str) -> str:
    resp = await client.post(
        f"/api/v1/patients/{patient}/allergies",
        json={"substance": "Penicillin", "severity": "severe", "reaction": "Rash"},
        headers=staff[Role.DOCTOR].headers,
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def signed_note(
    client: AsyncClient, staff: dict[Role, Actor], visit: dict[str, str], body: str = "Fever."
) -> str:
    doctor = staff[Role.DOCTOR]
    note = await client.post(
        f"/api/v1/patients/{visit['patient']}/notes",
        json={"encounter_id": visit["encounter"], "body": body},
        headers=doctor.headers,
    )
    assert note.status_code == 201, note.text
    signed = await client.post(f"/api/v1/notes/{note.json()['id']}/sign", headers=doctor.headers)
    assert signed.status_code == 200, signed.text
    return str(note.json()["id"])


# Writing -----------------------------------------------------------------------------------------


async def test_each_clinical_write_commits_one_version_and_one_leaf(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    visit: dict[str, str],
) -> None:
    doctor, lab = staff[Role.DOCTOR], staff[Role.LAB_TECH]
    patient = visit["patient"]
    vital = await add_vitals(client, staff, patient)
    allergy = await add_allergy(client, staff, patient)
    await client.patch(
        f"/api/v1/allergies/{allergy}", json={"is_active": False}, headers=doctor.headers
    )
    condition = (
        await client.post(
            f"/api/v1/patients/{patient}/conditions",
            json={"text": "Hypertension", "code": "I10", "onset": "2026-01-15"},
            headers=doctor.headers,
        )
    ).json()["id"]
    await client.patch(
        f"/api/v1/conditions/{condition}", json={"status": "resolved"}, headers=doctor.headers
    )
    draft = await client.post(
        f"/api/v1/patients/{patient}/notes",
        json={"encounter_id": visit["encounter"], "body": "Draft only"},
        headers=doctor.headers,
    )
    note = await signed_note(client, staff, visit)
    rx = (
        await client.post(
            f"/api/v1/patients/{patient}/prescriptions",
            json={"encounter_id": visit["encounter"], "items": ITEMS, "advice_en": "Rest"},
            headers=doctor.headers,
        )
    ).json()["id"]
    await client.post(f"/api/v1/prescriptions/{rx}/sign", headers=doctor.headers)
    order = (
        await client.post(
            f"/api/v1/patients/{patient}/lab-orders",
            json={"encounter_id": visit["encounter"], "tests": [{"code": "CBC", "name": "CBC"}]},
            headers=doctor.headers,
        )
    ).json()["id"]
    await client.patch(
        f"/api/v1/lab-orders/{order}", json={"status": "collected"}, headers=lab.headers
    )
    resulted = await client.post(
        f"/api/v1/lab-orders/{order}/result",
        data={"values": json.dumps({"hb": {"value": 13.1, "unit": "g/dL"}})},
        files={"file": ("cbc.pdf", PDF, "application/pdf")},
        headers=lab.headers,
    )
    assert resulted.status_code == 201, resulted.text
    result = resulted.json()["result"]
    await client.post(f"/api/v1/lab-orders/{order}/release", headers=lab.headers)
    await client.post(f"/api/v1/lab-orders/{order}/release", headers=lab.headers)  # no-op

    found = await versions(admin_engine, clinic)
    got = sorted((v.record_type.value, str(v.record_id), v.version) for v in found)
    assert got == sorted(
        [
            ("vital", vital, 1),
            ("allergy", allergy, 1),
            ("allergy", allergy, 2),
            ("condition", condition, 1),
            ("condition", condition, 2),
            ("note", note, 1),
            ("prescription", rx, 1),
            ("lab_order", order, 1),
            ("document", result["document_id"], 1),
            ("lab_result", result["id"], 1),
            ("lab_result", result["id"], 2),
        ]
    )
    assert draft.json()["id"] not in {str(v.record_id) for v in found}

    by_seq = await leaves(admin_engine, clinic)
    assert len(by_seq) == len(found)
    for v in found:
        leaf = by_seq[v.audit_seq]
        assert leaf["record_id"] == str(v.record_id)
        assert leaf["version"] == v.version
        assert leaf["commitment"] == v.commitment.hex()
        assert leaf["content_schema"] == v.content_schema
        assert "patient_id" not in leaf and "salt" not in leaf and "backfilled" not in leaf
        assert v.backfilled is False
    second = {(v.record_type, v.version): v for v in found}
    assert second[(RecordType.ALLERGY, 2)].prev_version_id == second[(RecordType.ALLERGY, 1)].id


async def test_a_status_set_to_its_current_value_adds_no_version(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    visit: dict[str, str],
) -> None:
    allergy = await add_allergy(client, staff, visit["patient"])
    same = await client.patch(
        f"/api/v1/allergies/{allergy}", json={"is_active": True}, headers=staff[Role.DOCTOR].headers
    )
    assert same.status_code == 200
    assert [v.version for v in await versions(admin_engine, clinic)] == [1]


async def test_a_new_note_version_links_to_the_parent_version_when_signed(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    visit: dict[str, str],
) -> None:
    doctor = staff[Role.DOCTOR]
    first = await signed_note(client, staff, visit, "Fever for 3 days.")
    second = await client.post(
        f"/api/v1/notes/{first}/versions",
        json={"body": "Correction: 4 days."},
        headers=doctor.headers,
    )
    assert len(await versions(admin_engine, clinic)) == 1  # the new draft is not committed
    await client.post(f"/api/v1/notes/{second.json()['id']}/sign", headers=doctor.headers)
    v1, v2 = await versions(admin_engine, clinic)
    assert (str(v1.record_id), str(v2.record_id)) == (first, second.json()["id"])
    assert v2.version == 1 and v2.prev_version_id == v1.id

    await anchor(admin_engine, clinic)
    body = (await check(client, doctor, "note", first)).json()
    assert body["verdict"] == "match"
    assert [(v["record_id"], v["status"]) for v in body["versions"]] == [
        (first, "match"),
        (second.json()["id"], "match"),
    ]
    assert body["versions"][1]["content"]["body"] == "Correction: 4 days."


async def test_version_and_leaf_roll_back_with_the_clinical_write(
    admin_engine: AsyncEngine, clinic: uuid.UUID, staff: dict[Role, Actor]
) -> None:
    patient = await make_patient(admin_engine, clinic, staff[Role.RECEPTION].user_id)
    nurse = staff[Role.NURSE]
    before = await audit_total(admin_engine, clinic)
    with pytest.raises(RuntimeError, match="after the commit step"):
        async with tenant_session(nurse.ctx) as db:
            vital = m.Vital(
                clinic_id=clinic, patient_id=patient, recorded_by=nurse.user_id, pulse=70
            )
            db.add(vital)
            await write_version(db, clinic, RecordType.VITAL, vital, nurse.user_id)
            raise RuntimeError("after the commit step")
    assert await versions(admin_engine, clinic) == []
    assert await audit_total(admin_engine, clinic) == before


# Endpoint verdicts -------------------------------------------------------------------------------


async def test_match_returns_proofs_that_verify_without_the_server(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    visit: dict[str, str],
) -> None:
    vital = await add_vitals(client, staff, visit["patient"])
    await anchor(admin_engine, clinic)
    resp = await check(client, staff[Role.DOCTOR], "vital", vital)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["verdict"] == "match" and body["reason"] is None
    assert body["backfilled"] is False
    (version,) = body["versions"]
    assert version["status"] == "match" and version["anchored"] is True
    assert version["content"]["temp_c"] == "37.2" and version["content"]["weight_kg"] == "61.50"
    assert [a["backend"] for a in version["anchors"]] == ["amoy"]

    recomputed = commitment(bytes.fromhex(version["salt"]), version["content"])
    assert recomputed.hex() == version["commitment"] == version["payload"]["commitment"]
    digest = payload_hash(version["payload"])
    assert digest.hex() == version["payload_hash"]
    leaf = leaf_hash(digest)
    assert leaf.hex() == version["leaf_hash"]
    head = TreeHead.from_json(version["sth"])
    assert verify_inclusion(
        leaf,
        version["leaf_index"],
        head.tree_size,
        [bytes.fromhex(p) for p in version["proof"]],
        bytes.fromhex(head.root_hex),
    )
    assert verify(head, bytes.fromhex(version["signature"]), bytes.fromhex(body["public_key"]))


async def test_pending_until_the_leaf_is_under_a_chain_anchored_root(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    visit: dict[str, str],
) -> None:
    vital = await add_vitals(client, staff, visit["patient"])
    body = (await check(client, staff[Role.DOCTOR], "vital", vital)).json()
    assert body["verdict"] == "pending"
    assert body["versions"][0]["proof"] is None

    await build_checkpoint(clinic)  # signed, but not confirmed on a chain
    body = (await check(client, staff[Role.DOCTOR], "vital", vital)).json()
    assert body["verdict"] == "pending"
    assert body["versions"][0]["proof"] is not None and body["versions"][0]["anchored"] is False


async def test_mismatch_when_a_row_is_changed_in_the_database(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    visit: dict[str, str],
) -> None:
    allergy = await add_allergy(client, staff, visit["patient"])
    await anchor(admin_engine, clinic)
    async with admin_engine.begin() as conn:
        await conn.execute(
            text("UPDATE allergies SET severity = 'mild' WHERE id = :id"), {"id": allergy}
        )
    body = (await check(client, staff[Role.DOCTOR], "allergy", allergy)).json()
    assert body["verdict"] == "mismatch"
    assert body["reason"] == "the record differs from its last committed version"
    version = body["versions"][0]
    assert version["status"] == "mismatch"
    recomputed = commitment(bytes.fromhex(version["salt"]), version["content"])
    assert recomputed.hex() != version["commitment"]

    status = await records_status(client, staff[Role.CLINIC_ADMIN])
    assert status["mismatch"] == 1


async def test_mismatch_when_an_append_only_row_is_changed_by_the_owner(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    visit: dict[str, str],
) -> None:
    vital = await add_vitals(client, staff, visit["patient"])
    async with admin_engine.begin() as conn:
        await conn.execute(text("ALTER TABLE vitals DISABLE TRIGGER vitals_append_only"))
        await conn.execute(text("UPDATE vitals SET pulse = 120 WHERE id = :id"), {"id": vital})
        await conn.execute(text("ALTER TABLE vitals ENABLE TRIGGER vitals_append_only"))
    body = (await check(client, staff[Role.DOCTOR], "vital", vital)).json()
    assert body["verdict"] == "mismatch"


async def test_mismatch_when_the_stored_commitment_is_rewritten(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    visit: dict[str, str],
) -> None:
    allergy = await add_allergy(client, staff, visit["patient"])
    async with admin_engine.begin() as conn:
        await conn.execute(
            text("UPDATE allergies SET severity = 'mild' WHERE id = :id"), {"id": allergy}
        )
        await conn.execute(
            text("ALTER TABLE record_versions DISABLE TRIGGER record_versions_append_only")
        )
        await conn.execute(
            text("UPDATE record_versions SET commitment = :c WHERE record_id = :id"),
            {"c": bytes(32), "id": allergy},
        )
        await conn.execute(
            text("ALTER TABLE record_versions ENABLE TRIGGER record_versions_append_only")
        )
    body = (await check(client, staff[Role.DOCTOR], "allergy", allergy)).json()
    assert body["verdict"] == "mismatch"
    assert body["reason"] == "the stored version does not match its audit leaf"


async def test_missing_version_when_a_row_is_written_outside_the_app(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    visit: dict[str, str],
) -> None:
    async with AsyncSession(admin_engine) as s, s.begin():
        vital = m.Vital(
            clinic_id=clinic,
            patient_id=uuid.UUID(visit["patient"]),
            recorded_by=staff[Role.NURSE].user_id,
            pulse=99,
        )
        s.add(vital)
        await s.flush()
        vital_id = str(vital.id)
    body = (await check(client, staff[Role.DOCTOR], "vital", vital_id)).json()
    assert body["verdict"] == "missing_version"
    assert body["versions"] == [] and body["covered_since"] is None


async def test_missing_row_when_a_committed_row_is_deleted(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    visit: dict[str, str],
) -> None:
    allergy = await add_allergy(client, staff, visit["patient"])
    async with admin_engine.begin() as conn:
        await conn.execute(text("DELETE FROM allergies WHERE id = :id"), {"id": allergy})
    body = (await check(client, staff[Role.DOCTOR], "allergy", allergy)).json()
    assert body["verdict"] == "missing_row"
    assert body["patient_id"] == visit["patient"]
    assert body["versions"][0]["status"] == "missing_row"
    assert body["versions"][0]["content"] is None

    status = await records_status(client, staff[Role.CLINIC_ADMIN])
    assert status["missing_row"] == 1


async def test_lab_result_release_is_a_second_version(
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
            json={"encounter_id": visit["encounter"], "tests": [{"code": "RBS", "name": "Sugar"}]},
            headers=doctor.headers,
        )
    ).json()["id"]
    await client.patch(
        f"/api/v1/lab-orders/{order}", json={"status": "collected"}, headers=lab.headers
    )
    result = (
        await client.post(
            f"/api/v1/lab-orders/{order}/result",
            data={"values": json.dumps({"rbs": {"value": 110, "unit": "mg/dL"}})},
            headers=lab.headers,
        )
    ).json()["result"]["id"]
    await client.post(f"/api/v1/lab-orders/{order}/release", headers=lab.headers)
    await anchor(admin_engine, clinic)
    body = (await check(client, doctor, "lab_result", result)).json()
    assert body["verdict"] == "match"
    assert [(v["version"], v["status"]) for v in body["versions"]] == [
        (1, "superseded"),
        (2, "match"),
    ]
    assert body["versions"][1]["content"]["released_at"] is not None
    assert body["versions"][0]["content"] is None
    order_check = (await check(client, doctor, "lab_order", order)).json()
    assert order_check["verdict"] == "match"  # status changes are not in the order's content


async def test_lab_values_that_cannot_be_committed_are_refused(
    client: AsyncClient, staff: dict[Role, Actor], visit: dict[str, str]
) -> None:
    doctor, lab = staff[Role.DOCTOR], staff[Role.LAB_TECH]
    order = (
        await client.post(
            f"/api/v1/patients/{visit['patient']}/lab-orders",
            json={"encounter_id": visit["encounter"], "tests": [{"code": "RBS", "name": "Sugar"}]},
            headers=doctor.headers,
        )
    ).json()["id"]
    await client.patch(
        f"/api/v1/lab-orders/{order}", json={"status": "collected"}, headers=lab.headers
    )
    resp = await client.post(
        f"/api/v1/lab-orders/{order}/result",
        data={"values": '{"rbs": NaN}'},
        headers=lab.headers,
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "bad-values"


async def test_the_check_is_recorded_as_an_access(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    visit: dict[str, str],
) -> None:
    vital = await add_vitals(client, staff, visit["patient"])
    await check(client, staff[Role.DOCTOR], "vital", vital)
    async with AsyncSession(admin_engine) as s:
        last = await s.scalar(
            select(m.AuditEvent.payload)
            .where(m.AuditEvent.clinic_id == clinic, m.AuditEvent.kind == "access")
            .order_by(m.AuditEvent.seq.desc())
            .limit(1)
        )
    assert last is not None
    assert last["resource"] == "vitals" and last["action"] == "view"
    assert last["detail"] == {"op": "record.integrity", "record_type": "vital", "record_id": vital}


# Who may check -----------------------------------------------------------------------------------


async def test_roles_follow_the_access_policy(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    visit: dict[str, str],
) -> None:
    note = await signed_note(client, staff, visit)
    assert (await check(client, staff[Role.DOCTOR], "note", note)).status_code == 200
    assert (await check(client, staff[Role.PATIENT], "note", note)).status_code == 200
    assert (await check(client, staff[Role.RECEPTION], "note", note)).status_code == 403
    assert (await check(client, staff[Role.NURSE], "note", note)).status_code == 403
    assert (await check(client, staff[Role.CLINIC_ADMIN], "note", note)).status_code == 403

    stranger = await make_actor(admin_engine, clinic, Role.PATIENT)
    await make_patient(admin_engine, clinic, staff[Role.RECEPTION].user_id, stranger.user_id)
    assert (await check(client, stranger, "note", note)).status_code == 403


async def test_clinic_b_cannot_check_clinic_a_records(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    staff: dict[Role, Actor],
    visit: dict[str, str],
) -> None:
    vital = await add_vitals(client, staff, visit["patient"])
    other_clinic = await make_clinic(admin_engine)
    other_doctor = await make_actor(admin_engine, other_clinic, Role.DOCTOR)
    resp = await check(client, other_doctor, "vital", vital)
    assert resp.status_code == 404
    assert resp.json()["code"] == "record-not-found"


async def test_unknown_record_is_404(client: AsyncClient, staff: dict[Role, Actor]) -> None:
    resp = await check(client, staff[Role.DOCTOR], "vital", str(uuid.uuid4()))
    assert resp.status_code == 404
    bad_type = await check(client, staff[Role.DOCTOR], "invoice", str(uuid.uuid4()))
    assert bad_type.status_code == 422


# Audit status and receipts -----------------------------------------------------------------------


async def test_records_status_counts_and_reuses_results(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    visit: dict[str, str],
) -> None:
    await add_vitals(client, staff, visit["patient"])
    await add_allergy(client, staff, visit["patient"])
    await client.post(
        f"/api/v1/patients/{visit['patient']}/notes",
        json={"encounter_id": visit["encounter"], "body": "draft"},
        headers=staff[Role.DOCTOR].headers,
    )
    admin = staff[Role.CLINIC_ADMIN]
    status = await records_status(client, admin)
    counts = {k: status[k] for k in COUNT_KEYS}
    assert counts == {
        "records": 2,
        "match": 0,
        "pending": 2,
        "mismatch": 0,
        "missing_version": 0,
        "missing_row": 0,
        "backfilled": 0,
    }
    assert status["cached"] is False and status["tree_size"] == 0
    again = await records_status(client, admin)
    assert again["cached"] is True and again["computed_at"] == status["computed_at"]

    await anchor(admin_engine, clinic)
    status = await records_status(client, admin)
    assert status["cached"] is False and status["anchored_size"] == status["tree_size"] > 0
    assert status["match"] == 2 and status["pending"] == 0


async def test_records_status_is_for_clinic_admins_only(
    client: AsyncClient, staff: dict[Role, Actor]
) -> None:
    for role in (Role.DOCTOR, Role.NURSE, Role.RECEPTION, Role.PATIENT):
        resp = await client.get("/api/v1/audit/records-status", headers=staff[role].headers)
        assert resp.status_code == 403, role


async def test_audit_status_no_longer_scans_records(
    client: AsyncClient, staff: dict[Role, Actor], visit: dict[str, str]
) -> None:
    await add_vitals(client, staff, visit["patient"])
    status = await client.get("/api/v1/audit/status", headers=staff[Role.CLINIC_ADMIN].headers)
    assert status.status_code == 200
    assert "records" not in status.json()


async def test_access_receipts_work_with_record_leaves_in_between(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    visit: dict[str, str],
) -> None:
    patient_actor = staff[Role.PATIENT]
    await add_vitals(client, staff, visit["patient"])
    await add_allergy(client, staff, visit["patient"])
    await client.get(f"/api/v1/patients/{visit['patient']}", headers=staff[Role.RECEPTION].headers)
    log = await client.get(
        f"/api/v1/patients/{visit['patient']}/access-log", headers=patient_actor.headers
    )
    event_id = log.json()["items"][0]["id"]
    await build_checkpoint(clinic)
    resp = await client.get(
        f"/api/v1/access-events/{event_id}/receipt", headers=patient_actor.headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    head = TreeHead.from_json(body["sth"])
    assert verify_inclusion(
        bytes.fromhex(body["leaf_hash"]),
        body["leaf_index"],
        head.tree_size,
        [bytes.fromhex(p) for p in body["proof"]],
        bytes.fromhex(head.root_hex),
    )
    assert len(await leaves(admin_engine, clinic)) == 2  # the vitals and the allergy
    assert body["payload"]["type"] == "access"


# Backfill ----------------------------------------------------------------------------------------


async def _legacy_rows(
    engine: AsyncEngine,
    clinic: uuid.UUID,
    patient: uuid.UUID,
    staff: dict[Role, Actor],
    encounter: str,
) -> dict[str, uuid.UUID]:
    """Rows written before record versions existed, inserted directly like the demo loader."""
    doctor = staff[Role.DOCTOR].user_id
    now = datetime.now(UTC)
    ids = {k: uuid.uuid4() for k in ("vital", "allergy", "note1", "note2", "draft", "rx")}
    async with AsyncSession(engine) as s, s.begin():
        s.add(
            m.Vital(
                id=ids["vital"], clinic_id=clinic, patient_id=patient, recorded_by=doctor, pulse=64
            )
        )
        s.add(
            m.Allergy(
                id=ids["allergy"],
                clinic_id=clinic,
                patient_id=patient,
                substance="Sulfa",
                severity="moderate",
                recorded_by=doctor,
            )
        )
        enc = uuid.UUID(encounter)
        for key, version, parent, signed in (
            ("note1", 1, None, now),
            ("note2", 2, ids["note1"], now),
            ("draft", 3, ids["note2"], None),
        ):
            s.add(
                m.ClinicalNote(
                    id=ids[key],
                    clinic_id=clinic,
                    patient_id=patient,
                    encounter_id=enc,
                    author_user_id=doctor,
                    version=version,
                    parent_id=parent,
                    body_enc=encrypt_body(ids[key], f"legacy {key}"),
                    signed_at=signed,
                )
            )
            await s.flush()
        s.add(
            m.Prescription(
                id=ids["rx"],
                clinic_id=clinic,
                patient_id=patient,
                encounter_id=enc,
                author_user_id=doctor,
                items=ITEMS,
                signed_at=now,
            )
        )
    return ids


async def test_backfill_marks_versions_and_running_it_again_adds_nothing(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    visit: dict[str, str],
) -> None:
    ids = await _legacy_rows(
        admin_engine, clinic, uuid.UUID(visit["patient"]), staff, visit["encounter"]
    )
    before = (await check(client, staff[Role.DOCTOR], "note", str(ids["note2"]))).json()
    assert before["verdict"] == "missing_version"

    assert await count_pending(clinic) == {
        "vital": 1,
        "allergy": 1,
        "condition": 0,
        "lab_order": 0,
        "document": 0,
        "lab_result": 0,
        "note": 2,
        "prescription": 1,
    }
    written = await backfill_clinic(clinic, batch=2)  # several batches
    assert dict(written) == {"vital": 1, "allergy": 1, "note": 2, "prescription": 1}

    found = {v.record_id: v for v in await versions(admin_engine, clinic)}
    assert set(found) == {ids["vital"], ids["allergy"], ids["note1"], ids["note2"], ids["rx"]}
    assert all(v.backfilled and v.version == 1 for v in found.values())
    assert found[ids["note2"]].prev_version_id == found[ids["note1"]].id
    by_seq = await leaves(admin_engine, clinic)
    assert all(by_seq[v.audit_seq]["backfilled"] is True for v in found.values())

    total = await audit_total(admin_engine, clinic)
    assert sum((await backfill_clinic(clinic)).values()) == 0
    assert await audit_total(admin_engine, clinic) == total
    assert sum((await count_pending(clinic)).values()) == 0

    await anchor(admin_engine, clinic)
    body = (await check(client, staff[Role.DOCTOR], "note", str(ids["note2"]))).json()
    assert body["verdict"] == "match"
    assert body["backfilled"] is True
    assert body["covered_since"] is not None
    assert all(v["backfilled"] for v in body["versions"])
    committed = datetime.fromisoformat(body["covered_since"])
    assert committed >= min(datetime.fromisoformat(v["committed_at"]) for v in body["versions"])

    status = await records_status(client, staff[Role.CLINIC_ADMIN])
    assert status["backfilled"] == 5
    assert status["missing_version"] == 0


async def test_versions_written_after_a_backfill_are_not_marked(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    visit: dict[str, str],
) -> None:
    ids = await _legacy_rows(
        admin_engine, clinic, uuid.UUID(visit["patient"]), staff, visit["encounter"]
    )
    await backfill_clinic(clinic)
    await client.patch(
        f"/api/v1/allergies/{ids['allergy']}",
        json={"is_active": False},
        headers=staff[Role.DOCTOR].headers,
    )
    body = (await check(client, staff[Role.DOCTOR], "allergy", str(ids["allergy"]))).json()
    assert [(v["version"], v["backfilled"]) for v in body["versions"]] == [(1, True), (2, False)]
    assert body["backfilled"] is True


# Careful rewrites (the record tamper demo's helpers) ---------------------------------------------


async def test_a_rewritten_log_is_a_mismatch_once_anchored(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    visit: dict[str, str],
) -> None:
    from app.scripts.record_tamper_demo import forge_version

    vital = await add_vitals(client, staff, visit["patient"])
    await anchor(admin_engine, clinic)
    await forge_version(
        clinic, RecordType.VITAL, uuid.UUID(vital), "UPDATE vitals SET pulse = 140 WHERE id = :id"
    )
    body = (await check(client, staff[Role.DOCTOR], "vital", vital)).json()
    assert body["verdict"] == "mismatch"
    assert body["reason"] == "the audit log no longer matches its signed checkpoint"
    version = body["versions"][0]
    head = TreeHead.from_json(version["sth"])
    assert not verify_inclusion(
        bytes.fromhex(version["leaf_hash"]),
        version["leaf_index"],
        head.tree_size,
        [bytes.fromhex(p) for p in version["proof"]],
        bytes.fromhex(head.root_hex),
    )


async def test_a_rewrite_before_the_first_anchor_is_not_caught(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    staff: dict[Role, Actor],
    visit: dict[str, str],
) -> None:
    """The stated limit (ADR 0011): nothing tells a change apart from the real record if it was
    made before any anchor covered the version."""
    from app.scripts.record_tamper_demo import forge_version

    vital = await add_vitals(client, staff, visit["patient"])
    await forge_version(
        clinic, RecordType.VITAL, uuid.UUID(vital), "UPDATE vitals SET pulse = 140 WHERE id = :id"
    )
    await anchor(admin_engine, clinic)
    body = (await check(client, staff[Role.DOCTOR], "vital", vital)).json()
    assert body["verdict"] == "match"
    assert body["versions"][0]["content"]["pulse"] == 140
