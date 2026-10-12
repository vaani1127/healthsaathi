import uuid
from typing import Any

import pytest
import requests
from httpx import AsyncClient
from pydantic import SecretStr
from pywebpush import WebPushException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import get_settings
from app.db import models as m
from app.db.enums import Role
from app.notify import push
from tests.helpers import Actor, make_actor, make_clinic, make_patient

ENDPOINT = "https://push.example.test/send/"


@pytest.fixture
def push_on(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    public, private = push.generate_vapid_keys()
    settings = get_settings()
    monkeypatch.setattr(settings, "vapid_public_key", public)
    monkeypatch.setattr(settings, "vapid_private_key", SecretStr(private))
    sent: list[dict[str, Any]] = []

    def fake_send(sub: m.PushSubscription, data: str) -> None:
        sent.append({"endpoint": sub.endpoint, "data": data})

    monkeypatch.setattr(push, "_send", fake_send)
    return sent


async def jobs_for(engine: AsyncEngine, user_id: uuid.UUID) -> list[m.Job]:
    async with AsyncSession(engine) as s:
        rows = await s.scalars(select(m.Job).where(m.Job.payload["user_id"].astext == str(user_id)))
        return list(rows.all())


async def setup_patient(engine: AsyncEngine) -> tuple[Actor, Actor, uuid.UUID]:
    clinic = await make_clinic(engine)
    reception = await make_actor(engine, clinic, Role.RECEPTION)
    patient_user = await make_actor(engine, clinic, Role.PATIENT)
    patient_id = await make_patient(engine, clinic, reception.user_id, patient_user.user_id)
    return reception, patient_user, patient_id


async def subscribe(client: AsyncClient, actor: Actor, endpoint: str) -> int:
    body = {"endpoint": endpoint, "keys": {"p256dh": "B" * 87, "auth": "A" * 22}}
    r = await client.post("/api/v1/push/subscriptions", json=body, headers=actor.headers)
    return r.status_code


async def test_key_is_null_when_push_is_off(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/push/key")).json() == {"public_key": None}


async def test_staff_access_queues_and_sends_a_push(
    client: AsyncClient, admin_engine: AsyncEngine, push_on: list[dict[str, Any]]
) -> None:
    key = (await client.get("/api/v1/push/key")).json()["public_key"]
    assert key and len(key) == 87, "uncompressed P-256 point, base64url"
    reception, patient_user, patient_id = await setup_patient(admin_engine)
    endpoint = ENDPOINT + uuid.uuid4().hex
    assert await subscribe(client, patient_user, "http://insecure.test/x") == 422
    assert await subscribe(client, patient_user, endpoint) == 204

    # The patient reading their own record does not notify them.
    await client.get(f"/api/v1/patients/{patient_id}/chart", headers=patient_user.headers)
    assert await jobs_for(admin_engine, patient_user.user_id) == []

    r = await client.get(f"/api/v1/patients/{patient_id}", headers=reception.headers)
    assert r.status_code == 200
    queued = await jobs_for(admin_engine, patient_user.user_id)
    assert len(queued) == 1 and queued[0].kind == push.ACCESS_JOB

    assert await push.send_pending() >= 1
    mine = [s for s in push_on if s["endpoint"] == endpoint]
    assert len(mine) == 1
    assert "opened your record" in mine[0]["data"]
    assert str(patient_id) not in mine[0]["data"], "no patient data in the message"
    done = await jobs_for(admin_engine, patient_user.user_id)
    assert done[0].done_at is not None


async def test_no_job_without_a_subscription(
    client: AsyncClient, admin_engine: AsyncEngine, push_on: list[dict[str, Any]]
) -> None:
    reception, patient_user, patient_id = await setup_patient(admin_engine)
    await client.get(f"/api/v1/patients/{patient_id}", headers=reception.headers)
    assert await jobs_for(admin_engine, patient_user.user_id) == []


async def test_gone_subscription_is_removed(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    push_on: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reception, patient_user, patient_id = await setup_patient(admin_engine)
    endpoint = ENDPOINT + uuid.uuid4().hex
    assert await subscribe(client, patient_user, endpoint) == 204

    def gone(sub: m.PushSubscription, data: str) -> None:
        response = requests.Response()
        response.status_code = 410
        raise WebPushException("gone", response=response)

    monkeypatch.setattr(push, "_send", gone)
    await client.get(f"/api/v1/patients/{patient_id}", headers=reception.headers)
    await push.send_pending()
    async with AsyncSession(admin_engine) as s:
        left = await s.scalar(
            select(func.count())
            .select_from(m.PushSubscription)
            .where(m.PushSubscription.endpoint == endpoint)
        )
    assert left == 0


async def test_unsubscribe_only_removes_your_own(
    client: AsyncClient, admin_engine: AsyncEngine, push_on: list[dict[str, Any]]
) -> None:
    _, patient_user, _ = await setup_patient(admin_engine)
    _, someone_else, _ = await setup_patient(admin_engine)
    endpoint = ENDPOINT + uuid.uuid4().hex
    assert await subscribe(client, patient_user, endpoint) == 204

    async def count() -> int:
        async with AsyncSession(admin_engine) as s:
            n = await s.scalar(
                select(func.count())
                .select_from(m.PushSubscription)
                .where(m.PushSubscription.endpoint == endpoint)
            )
        return int(n or 0)

    url = "/api/v1/push/subscriptions/remove"
    await client.post(url, json={"endpoint": endpoint}, headers=someone_else.headers)
    assert await count() == 1
    await client.post(url, json={"endpoint": endpoint}, headers=patient_user.headers)
    assert await count() == 0
    assert (await client.post(url, json={"endpoint": endpoint})).status_code == 401
