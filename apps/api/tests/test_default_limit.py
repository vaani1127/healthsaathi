"""The SPEC 4 default limit: 120 requests a minute per user (valid access token) or per IP."""

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.types import Receive, Scope, Send

from app.core import ratelimit
from app.core.ratelimit import DefaultRateLimitMiddleware
from app.db.enums import Role
from app.identity import tokens
from app.main import create_app
from tests.helpers import make_actor, make_clinic, within_one_window

# Unknown paths are counted like any other request and answer fast (404, no database).
NOWHERE = "/api/v1/not-a-route"


@pytest.fixture
def app() -> FastAPI:
    return create_app()


def client_from(app: FastAPI, peer: str = "198.51.100.10") -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app, client=(peer, 40000)), base_url="http://t")


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with client_from(app) as c:
        yield c


def bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def access_for(user: uuid.UUID) -> str:
    token, _ = tokens.access_token(user, uuid.uuid4(), ["pwd", "otp"])
    return token


async def statuses(
    client: AsyncClient, count: int, path: str = NOWHERE, **kwargs: object
) -> list[int]:
    return [(await client.get(path, **kwargs)).status_code for _ in range(count)]  # type: ignore[arg-type]


async def test_the_121st_request_from_one_user_is_refused(client: AsyncClient) -> None:
    await within_one_window()
    user = bearer(access_for(uuid.uuid4()))
    assert set(await statuses(client, 120, headers=user)) == {404}
    refused = await client.get(NOWHERE, headers=user)
    assert refused.status_code == 429
    assert refused.headers["content-type"].startswith("application/problem+json")
    assert refused.json()["code"] == "rate-limited"
    assert 1 <= int(refused.headers["retry-after"]) <= 60


async def test_a_second_user_on_the_same_ip_is_not_affected(client: AsyncClient) -> None:
    await within_one_window()
    first, second = bearer(access_for(uuid.uuid4())), bearer(access_for(uuid.uuid4()))
    await statuses(client, 121, headers=first)
    assert (await client.get(NOWHERE, headers=first)).status_code == 429
    assert set(await statuses(client, 120, headers=second)) == {404}


async def test_without_a_token_the_client_ip_is_counted(app: FastAPI) -> None:
    await within_one_window()
    async with client_from(app, "203.0.113.1") as one, client_from(app, "203.0.113.2") as two:
        assert set(await statuses(one, 120)) == {404}
        assert (await one.get(NOWHERE)).status_code == 429
        # An invalid token is no token: still the same IP's count.
        assert (await one.get(NOWHERE, headers=bearer("not-a-token"))).status_code == 429
        assert (await two.get(NOWHERE)).status_code == 404


async def test_a_login_challenge_token_counts_per_ip_not_per_user(app: FastAPI) -> None:
    await within_one_window()
    user = uuid.uuid4()
    challenge, _ = tokens.encode_token("mfa_challenge", user, timedelta(minutes=5))
    enrol, _ = tokens.encode_token("mfa_enroll", user, timedelta(minutes=10))
    async with client_from(app, "203.0.113.3") as c:
        assert set(await statuses(c, 60, headers=bearer(challenge))) == {404}
        assert set(await statuses(c, 60, headers=bearer(enrol))) == {404}
        # 120 counted on the IP: a request with no token is now refused.
        assert (await c.get(NOWHERE)).status_code == 429
        # The same user with an access token has a count of their own.
        assert (await c.get(NOWHERE, headers=bearer(access_for(user)))).status_code == 404


async def test_health_and_ready_are_never_limited(client: AsyncClient) -> None:
    await within_one_window()
    assert set(await statuses(client, 130, "/api/v1/health")) == {200}
    assert set(await statuses(client, 125, "/api/v1/ready")) == {200}
    assert set(await statuses(client, 120)) == {404}


async def test_options_requests_are_not_counted(client: AsyncClient) -> None:
    await within_one_window()
    # A CORS preflight (answered by the CORS middleware) and a plain OPTIONS (which reaches the
    # limiter, which skips it).
    preflight = {"origin": "http://localhost:5173", "access-control-request-method": "GET"}
    for _ in range(65):
        assert (await client.options("/api/v1/patients", headers=preflight)).status_code == 200
        assert (await client.options(NOWHERE)).status_code != 429
    assert set(await statuses(client, 120)) == {404}


async def test_websocket_connections_are_skipped() -> None:
    reached: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        reached.append(scope["type"])

    middleware = DefaultRateLimitMiddleware(inner)
    scope: Scope = {
        "type": "websocket",
        "path": "/api/v1/ws",
        "headers": [],
        "client": ("192.0.2.9", 1),
    }
    for _ in range(130):
        await middleware(scope, None, None)  # type: ignore[arg-type]
    assert reached == ["websocket"] * 130


async def test_a_refused_request_carries_cors_headers(client: AsyncClient) -> None:
    await within_one_window()
    origin = {"origin": "http://localhost:5173"}
    await statuses(client, 120, headers=origin)
    refused = await client.get(NOWHERE, headers=origin)
    assert refused.status_code == 429
    assert refused.headers["access-control-allow-origin"] == "http://localhost:5173"


async def test_switched_off_limits_nothing(
    monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    monkeypatch.setattr(ratelimit.limiter, "enabled", False)
    assert set(await statuses(client, 130)) == {404}


async def test_search_limit_still_works_with_retry_after(
    client: AsyncClient, admin_engine: AsyncEngine
) -> None:
    await within_one_window()
    clinic = await make_clinic(admin_engine)
    reception = await make_actor(admin_engine, clinic, Role.RECEPTION)
    codes = await statuses(client, 30, "/api/v1/patients", headers=reception.headers)
    assert set(codes) == {200}
    refused = await client.get("/api/v1/patients", headers=reception.headers)
    assert refused.status_code == 429
    assert refused.json()["code"] == "rate-limited"
    assert 1 <= int(refused.headers["retry-after"]) <= 60


async def test_sign_in_limits_send_retry_after(client: AsyncClient) -> None:
    await within_one_window()
    for i in range(20):
        await client.post(
            "/api/v1/auth/login", json={"email": f"ra{i}@default.test", "password": "x" * 12}
        )
    refused = await client.post(
        "/api/v1/auth/login", json={"email": "ra@default.test", "password": "x" * 12}
    )
    assert refused.status_code == 429
    assert 1 <= int(refused.headers["retry-after"]) <= 60
