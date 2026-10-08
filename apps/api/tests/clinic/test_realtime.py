import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from httpx import AsyncClient
from httpx_ws import AsyncWebSocketSession, WebSocketDisconnect, aconnect_ws
from httpx_ws.transport import ASGIWebSocketTransport
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.enums import Role
from app.identity import tokens
from app.main import create_app
from app.realtime.hub import hub
from tests.helpers import make_actor, make_clinic, make_patient


@asynccontextmanager
async def socket(client: AsyncClient) -> AsyncIterator[AsyncWebSocketSession]:
    ws: AsyncWebSocketSession
    async with aconnect_ws("/api/v1/ws", client) as ws:
        yield ws


@asynccontextmanager
async def ws_app() -> AsyncIterator[AsyncClient]:
    # Opened inside each test: the transport's task group must start and stop in the same task.
    app = create_app()
    async with AsyncClient(transport=ASGIWebSocketTransport(app), base_url="http://test") as client:
        yield client
    await hub.close()


async def test_queue_update_reaches_subscribed_clients(
    admin_engine: AsyncEngine,
) -> None:
    clinic = await make_clinic(admin_engine)
    nurse = await make_actor(admin_engine, clinic, Role.NURSE)
    reception = await make_actor(admin_engine, clinic, Role.RECEPTION)
    doctor = await make_actor(admin_engine, clinic, Role.DOCTOR)
    patient_id = await make_patient(admin_engine, clinic, reception.user_id)

    async with ws_app() as ws_client, socket(ws_client) as ws:
        await ws.send_text(json.dumps({"type": "auth", "token": nurse.token}))
        assert json.loads(await ws.receive_text()) == {"type": "ready"}

        await ws.send_text("ping")
        assert await ws.receive_text() == "pong"

        resp = await ws_client.post(
            "/api/v1/walkins",
            json={"patient_id": str(patient_id), "doctor_user_id": str(doctor.user_id)},
            headers=reception.headers,
        )
        assert resp.status_code == 201

        seen = set()
        while "queue.changed" not in seen:
            message = json.loads(await asyncio.wait_for(ws.receive_text(), 5))
            seen.add(message["type"])
            # Messages never carry patient data.
            assert "name" not in json.dumps(message)


async def test_other_clinics_events_are_not_delivered(
    admin_engine: AsyncEngine,
) -> None:
    clinic_a, clinic_b = await make_clinic(admin_engine), await make_clinic(admin_engine)
    watcher = await make_actor(admin_engine, clinic_a, Role.NURSE)
    reception_b = await make_actor(admin_engine, clinic_b, Role.RECEPTION)
    doctor_b = await make_actor(admin_engine, clinic_b, Role.DOCTOR)
    patient_b = await make_patient(admin_engine, clinic_b, reception_b.user_id)

    async with ws_app() as ws_client, socket(ws_client) as ws:
        await ws.send_text(json.dumps({"type": "auth", "token": watcher.token}))
        assert json.loads(await ws.receive_text())["type"] == "ready"
        await ws_client.post(
            "/api/v1/walkins",
            json={"patient_id": str(patient_b), "doctor_user_id": str(doctor_b.user_id)},
            headers=reception_b.headers,
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(ws.receive_text(), 1)


@pytest.mark.parametrize(
    "bad",
    ["not json", json.dumps({"type": "hello"}), json.dumps({"type": "auth", "token": "junk"})],
)
async def test_bad_first_message_closes_the_socket(bad: str) -> None:
    async with ws_app() as ws_client, socket(ws_client) as ws:
        await ws.send_text(bad)
        with pytest.raises(WebSocketDisconnect) as exc:
            await ws.receive_text()
    assert exc.value.code == 1008


async def test_token_without_clinic_or_mfa_is_refused(
    admin_engine: AsyncEngine,
) -> None:
    clinic = await make_clinic(admin_engine)
    nurse = await make_actor(admin_engine, clinic, Role.NURSE)
    no_clinic, _ = tokens.access_token(nurse.user_id, nurse.session_id, ["pwd", "otp"])
    no_mfa, _ = tokens.access_token(nurse.user_id, nurse.session_id, ["pwd"], clinic, "nurse")
    ended, _ = tokens.access_token(nurse.user_id, uuid.uuid4(), ["pwd", "otp"], clinic, "nurse")
    for token in (no_clinic, no_mfa, ended):
        async with ws_app() as ws_client, socket(ws_client) as ws:
            await ws.send_text(json.dumps({"type": "auth", "token": token}))
            with pytest.raises(WebSocketDisconnect):
                await ws.receive_text()
