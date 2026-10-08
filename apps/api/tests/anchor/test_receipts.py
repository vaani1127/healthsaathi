import json
import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from app.anchor import job
from app.anchor.receipts import Receipt
from app.core.config import get_settings
from app.db.enums import Role
from app.ledger.checkpoint import build_checkpoint
from e2d_core.ledger import TreeHead, leaf_hash, payload_hash, verify, verify_inclusion
from tests.helpers import Actor, make_actor, make_clinic, make_patient


@pytest.fixture
async def clinic(admin_engine: AsyncEngine) -> uuid.UUID:
    return await make_clinic(admin_engine)


@pytest.fixture
async def reception(admin_engine: AsyncEngine, clinic: uuid.UUID) -> Actor:
    return await make_actor(admin_engine, clinic, Role.RECEPTION)


@pytest.fixture
async def admin(admin_engine: AsyncEngine, clinic: uuid.UUID) -> Actor:
    return await make_actor(admin_engine, clinic, Role.CLINIC_ADMIN)


@pytest.fixture
async def patient_actor(admin_engine: AsyncEngine, clinic: uuid.UUID) -> Actor:
    return await make_actor(admin_engine, clinic, Role.PATIENT)


@pytest.fixture
async def patient(
    admin_engine: AsyncEngine, clinic: uuid.UUID, reception: Actor, patient_actor: Actor
) -> uuid.UUID:
    return await make_patient(admin_engine, clinic, reception.user_id, patient_actor.user_id)


async def staff_read(
    client: AsyncClient, reception: Actor, patient_actor: Actor, patient: uuid.UUID
) -> str:
    """Reception opens the record; returns the id of the access event that was written."""
    r = await client.get(f"/api/v1/patients/{patient}", headers=reception.headers)
    assert r.status_code == 200
    log = await client.get(f"/api/v1/patients/{patient}/access-log", headers=patient_actor.headers)
    return str(log.json()["items"][0]["id"])


async def test_receipt_verifies_against_the_signed_tree_head(
    client: AsyncClient,
    clinic: uuid.UUID,
    reception: Actor,
    patient_actor: Actor,
    patient: uuid.UUID,
) -> None:
    event_id = await staff_read(client, reception, patient_actor, patient)
    url = f"/api/v1/access-events/{event_id}/receipt"

    early = await client.get(url, headers=patient_actor.headers)
    assert early.status_code == 409
    assert early.json()["type"].endswith("receipt-not-ready")

    await build_checkpoint(clinic)
    r = await client.get(url, headers=patient_actor.headers)
    assert r.status_code == 200
    body = r.json()

    assert body["payload"]["access_event_id"] == event_id
    assert body["payload"]["patient_id"] == str(patient)
    assert payload_hash(body["payload"]).hex() == body["payload_hash"]
    leaf = leaf_hash(bytes.fromhex(body["payload_hash"]))
    assert leaf.hex() == body["leaf_hash"]

    head = TreeHead.from_json(body["sth"])
    assert verify_inclusion(
        leaf,
        body["leaf_index"],
        head.tree_size,
        [bytes.fromhex(p) for p in body["proof"]],
        bytes.fromhex(head.root_hex),
    )
    assert verify(head, bytes.fromhex(body["signature"]), bytes.fromhex(body["public_key"]))
    assert body["clinic_id_bytes32"] == "0x" + uuid.UUID(str(clinic)).bytes.hex() + "00" * 16
    assert body["anchors"] == []


async def test_receipt_is_only_for_the_patient_it_is_about(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    reception: Actor,
    patient_actor: Actor,
    patient: uuid.UUID,
) -> None:
    event_id = await staff_read(client, reception, patient_actor, patient)
    await build_checkpoint(clinic)
    url = f"/api/v1/access-events/{event_id}/receipt"

    other = await make_actor(admin_engine, clinic, Role.PATIENT)
    await make_patient(admin_engine, clinic, reception.user_id, other.user_id)
    assert (await client.get(url, headers=other.headers)).status_code == 403

    assert (await client.get(url, headers=reception.headers)).status_code == 403

    # A patient of clinic B cannot even see that the event exists (RLS).
    clinic_b = await make_clinic(admin_engine)
    outsider = await make_actor(admin_engine, clinic_b, Role.PATIENT)
    staff_b = await make_actor(admin_engine, clinic_b, Role.RECEPTION)
    await make_patient(admin_engine, clinic_b, staff_b.user_id, outsider.user_id)
    assert (await client.get(url, headers=outsider.headers)).status_code == 404


async def test_receipt_for_unknown_event_is_404(client: AsyncClient, patient_actor: Actor) -> None:
    r = await client.get(
        f"/api/v1/access-events/{uuid.uuid4()}/receipt", headers=patient_actor.headers
    )
    assert r.status_code == 404


async def test_audit_status_reports_consistency(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    reception: Actor,
    admin: Actor,
    patient_actor: Actor,
    patient: uuid.UUID,
) -> None:
    empty = (await client.get("/api/v1/audit/status", headers=admin.headers)).json()
    assert empty["sth"] is None and empty["consistency_ok"] is None and empty["chain_ok"]

    await staff_read(client, reception, patient_actor, patient)
    await build_checkpoint(clinic)
    await staff_read(client, reception, patient_actor, patient)
    await build_checkpoint(clinic)

    r = await client.get("/api/v1/audit/status", headers=admin.headers)
    assert r.status_code == 200
    body = r.json()
    assert body["chain_ok"] is True
    assert body["consistency_ok"] is True
    assert body["problem"] is None
    assert body["sth"]["tree_size"] <= body["events"]

    # Other roles and other clinics cannot read it.
    assert (await client.get("/api/v1/audit/status", headers=reception.headers)).status_code == 403
    clinic_b = await make_clinic(admin_engine)
    admin_b = await make_actor(admin_engine, clinic_b, Role.CLINIC_ADMIN)
    other = (await client.get("/api/v1/audit/status", headers=admin_b.headers)).json()
    assert other["sth"] is None and other["events"] == 0


async def test_anchor_trigger_needs_the_token(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "/api/v1/internal/anchor/run"
    settings = get_settings()
    monkeypatch.setattr(settings, "anchor_trigger_token", None)
    assert (await client.post(url, headers={"Authorization": "Bearer x"})).status_code == 401

    monkeypatch.setattr(settings, "anchor_trigger_token", SecretStr("s3cret-trigger"))
    monkeypatch.setattr(job, "backends_from_settings", lambda: job.Backends())
    assert (await client.post(url, headers={"Authorization": "Bearer wrong"})).status_code == 401
    odd = {b"Authorization": "Bearer café".encode()}
    assert (await client.post(url, headers=odd)).status_code == 401
    r = await client.post(url, headers={"Authorization": "Bearer s3cret-trigger"})
    assert r.status_code == 200
    assert r.json()["status"] in ("done", "already_running")


def test_shared_vector_has_the_api_receipt_shape() -> None:
    """receipt-verify is tested against this vector, so it must match what the API returns."""
    path = Path(__file__).parents[4] / "packages/receipt-verify/test/vectors/ledger.json"
    vector = json.loads(path.read_text(encoding="utf-8"))["api_receipt"]
    vector.pop("sth_digest")
    assert Receipt.model_validate(vector).model_dump() == vector
