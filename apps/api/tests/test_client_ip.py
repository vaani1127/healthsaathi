"""The client address: X-Forwarded-For is believed only from the trusted proxy (Caddy).

A client that reaches the API some other way cannot pick a new address for each request by
sending its own X-Forwarded-For, so the per-IP limits hold.
"""

from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.access.service import _ip_hash
from app.core.config import Settings, get_settings
from app.core.ratelimit import client_ip, limiter
from app.db import models as m
from app.db.enums import Role
from app.main import create_app
from tests.helpers import make_actor, make_clinic, make_patient, within_one_window

PROXY = "10.250.0.2"
OTHER_CONTAINER = "10.250.0.130"


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    monkeypatch.setenv("TRUSTED_PROXY_IPS", PROXY)
    get_settings.cache_clear()
    yield create_app()
    monkeypatch.delenv("TRUSTED_PROXY_IPS")
    get_settings.cache_clear()


def client_from(app: FastAPI, peer: str) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app, client=(peer, 40000)), base_url="http://t")


@pytest.fixture
async def via_proxy(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with client_from(app, PROXY) as c:
        yield c


@pytest.fixture
async def direct(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with client_from(app, OTHER_CONTAINER) as c:
        yield c


async def logins(client: AsyncClient, count: int, spoof: bool) -> list[int]:
    statuses = []
    for i in range(count):
        headers = {"x-forwarded-for": f"203.0.113.{i + 1}"} if spoof else {}
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": f"ip{i}@client-ip.test", "password": "x" * 12},
            headers=headers,
        )
        statuses.append(resp.status_code)
    return statuses


async def test_spoofed_forwarded_for_from_another_peer_is_ignored(direct: AsyncClient) -> None:
    await within_one_window()
    # A new X-Forwarded-For on every request does not give a new address: the 21st is limited.
    statuses = await logins(direct, 21, spoof=True)
    assert statuses == [401] * 20 + [429]


async def test_forwarded_for_from_the_proxy_names_the_client(via_proxy: AsyncClient) -> None:
    # Through Caddy, 21 different clients each get their own limit.
    statuses = await logins(via_proxy, 21, spoof=True)
    assert statuses == [401] * 21


async def test_in_memory_route_limit_keys_on_the_forwarded_address(app: FastAPI) -> None:
    await within_one_window()

    # slowapi limits run inside the app, after the address is set, so an in-memory limit keyed
    # by IP counts the client, not Caddy. A test-only route with such a limit shows it.
    @app.get("/test-only/limited")
    @limiter.limit("2/minute", key_func=client_ip)
    async def limited(request: Request) -> dict[str, str]:
        return {"ip": request.client.host if request.client else ""}

    async with client_from(app, PROXY) as c:
        first = {"x-forwarded-for": "198.51.100.1"}
        statuses = [
            (await c.get("/test-only/limited", headers=first)).status_code for _ in range(3)
        ]
        assert statuses == [200, 200, 429]
        other = await c.get("/test-only/limited", headers={"x-forwarded-for": "198.51.100.2"})
        assert other.status_code == 200
        assert other.json() == {"ip": "198.51.100.2"}


@pytest.mark.parametrize(
    ("peer", "recorded"), [(PROXY, "198.51.100.9"), (OTHER_CONTAINER, OTHER_CONTAINER)]
)
async def test_access_event_records_the_client_address(
    app: FastAPI, admin_engine: AsyncEngine, peer: str, recorded: str
) -> None:
    clinic = await make_clinic(admin_engine)
    reception = await make_actor(admin_engine, clinic, Role.RECEPTION)
    patient = await make_patient(admin_engine, clinic, reception.user_id)
    async with client_from(app, peer) as c:
        resp = await c.get(
            f"/api/v1/patients/{patient}",
            headers={**reception.headers, "x-forwarded-for": "198.51.100.9"},
        )
    assert resp.status_code == 200, resp.text
    async with AsyncSession(admin_engine) as s:
        hashes = (
            await s.scalars(select(m.AccessEvent.ip_hash).where(m.AccessEvent.clinic_id == clinic))
        ).all()
    assert hashes == [_ip_hash(recorded)]


def test_wildcard_proxy_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "*")
    with pytest.raises(ValidationError):
        Settings()


def test_no_proxy_is_trusted_by_default() -> None:
    assert get_settings().trusted_proxies == []
