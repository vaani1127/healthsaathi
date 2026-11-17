"""Sign-in limits counted in the database: per IP, per email, per user, and the lockout."""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core import counters, ratelimit
from app.core.errors import ProblemError
from app.db import models as m
from app.db.enums import Role
from app.identity import lockout, service
from app.notify.email import outbox
from tests.factories import ClinicGraph, build_clinic_graph
from tests.identity.test_auth import PASSWORD, make_patient, make_user


@pytest.fixture(scope="module")
async def clinic(admin_engine: AsyncEngine) -> ClinicGraph:
    return await build_clinic_graph(admin_engine, "db-limits")


async def lockout_rows(engine: AsyncEngine, email: str) -> list[int]:
    key = counters.counter_key(lockout.SCOPE, email.lower())
    async with AsyncSession(engine) as s:
        return list(
            (await s.scalars(select(m.RateCounter.count).where(m.RateCounter.key == key))).all()
        )


async def wrong_password(email: str) -> str:
    try:
        await service.login_with_password(email, "wrong-password-123")
    except ProblemError as exc:
        return exc.code
    return "signed-in"


# Limits shared by workers ---------------------------------------------------------------------


async def test_exactly_the_limit_passes_across_sessions_and_the_next_is_refused() -> None:
    scope = f"limit-{uuid.uuid4().hex[:6]}"
    # Six hits at once, each on its own database session, as several workers would send them.
    results = await asyncio.gather(
        *(ratelimit.hit("5/minute", scope, "k") for _ in range(6)), return_exceptions=True
    )
    refused = [r for r in results if isinstance(r, ProblemError)]
    assert len([r for r in results if r is None]) == 5
    assert len(refused) == 1
    assert refused[0].status == 429


async def test_login_per_ip_is_counted_in_the_database(
    client: AsyncClient, admin_engine: AsyncEngine
) -> None:
    for i in range(5):
        resp = await client.post(
            "/api/v1/auth/login", json={"email": f"n{i}@db-limits.test", "password": "x" * 12}
        )
        assert resp.status_code == 401
    # The in-memory limiter is emptied (as a restart would); the database still has the count.
    ratelimit.reset()
    sixth = await client.post(
        "/api/v1/auth/login", json={"email": "n6@db-limits.test", "password": "x" * 12}
    )
    assert sixth.status_code == 429


# Email codes ----------------------------------------------------------------------------------


async def test_email_code_requests_are_limited_per_ip(
    client: AsyncClient, clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    statuses = []
    for _ in range(6):
        email = await make_patient(admin_engine, clinic)
        resp = await client.post("/api/v1/auth/otp/request", json={"email": email})
        statuses.append(resp.status_code)
    assert statuses == [202] * 5 + [429]
    assert (
        await client.post("/api/v1/auth/otp/request", json={"email": "x@db-limits.test"})
    ).json()["code"] == "rate-limited"


async def test_per_email_limit_answers_like_any_other_request(
    client: AsyncClient, clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    known = await make_patient(admin_engine, clinic)
    for _ in range(3):
        await service.request_email_code(known)
    sent = len(outbox.sent)
    # Over the per-email limit: the same 202 as for an unknown address, and no email.
    over = await client.post("/api/v1/auth/otp/request", json={"email": known})
    unknown = await client.post("/api/v1/auth/otp/request", json={"email": "nobody@db-limits.test"})
    assert over.status_code == unknown.status_code == 202
    assert over.json() == unknown.json() == {"status": "sent_if_registered"}
    assert len(outbox.sent) == sent


# Authenticator attempts -----------------------------------------------------------------------


async def test_authenticator_attempts_are_limited_per_user(
    clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    from datetime import timedelta

    from app.identity import tokens

    user = await make_user(admin_engine, clinic.clinic_id, Role.DOCTOR)
    challenge, _ = tokens.encode_token("mfa_challenge", user.user_id, timedelta(minutes=5))
    device = service.DeviceInfo(None, "db-limits")
    otp = pyotp.TOTP(user.totp_secret)
    now = int(time.time())
    valid = {otp.at(now + 30 * d) for d in range(-3, 4)}
    wrong = next(c for c in ("000000", "111111", "222222", "333333") if c not in valid)
    codes = []
    for _ in range(6):
        with pytest.raises(ProblemError) as exc:
            await service.verify_totp(challenge, wrong, device)
        codes.append(exc.value.code)
    assert codes == ["invalid-code"] * 5 + ["rate-limited"]
    # The 6th is refused even with the right code.
    with pytest.raises(ProblemError) as right:
        await service.verify_totp(challenge, otp.now(), device)
    assert right.value.code == "rate-limited"


# Password lockout -----------------------------------------------------------------------------


async def test_parallel_wrong_passwords_at_the_edge_check_only_one(
    clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    user = await make_user(admin_engine, clinic.clinic_id, Role.NURSE)
    for _ in range(4):
        assert await wrong_password(user.email) == "invalid-credentials"
    # Six wrong attempts at once at count 4: one more is checked, the rest are locked.
    results = await asyncio.gather(*(wrong_password(user.email) for _ in range(6)))
    assert sorted(results) == ["account-locked"] * 5 + ["invalid-credentials"]
    assert await lockout_rows(admin_engine, user.email) == [10]


async def test_correct_password_at_the_edge_signs_in_and_clears(
    clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    user = await make_user(admin_engine, clinic.clinic_id, Role.NURSE)
    for _ in range(4):
        await wrong_password(user.email)
    challenge = await service.login_with_password(user.email, PASSWORD)
    assert challenge.status == "mfa_required"
    assert await lockout_rows(admin_engine, user.email) == []
    # A fresh start: five more wrong passwords are checked before the lock.
    results = [await wrong_password(user.email) for _ in range(6)]
    assert results == ["invalid-credentials"] * 5 + ["account-locked"]


async def test_lockout_holds_after_a_restart(
    clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    user = await make_user(admin_engine, clinic.clinic_id, Role.NURSE)
    for _ in range(5):
        await wrong_password(user.email)
    ratelimit.reset()
    with pytest.raises(ProblemError) as locked:
        await service.login_with_password(user.email, PASSWORD)
    assert locked.value.code == "account-locked"


# Switch and failure ---------------------------------------------------------------------------


async def test_switched_off_counts_nothing(
    monkeypatch: pytest.MonkeyPatch, clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    monkeypatch.setattr(ratelimit.limiter, "enabled", False)
    user = await make_user(admin_engine, clinic.clinic_id, Role.NURSE)
    results = [await wrong_password(user.email) for _ in range(8)]
    assert results == ["invalid-credentials"] * 8
    assert await ratelimit.allow("1/minute", "off", "k")
    assert await ratelimit.allow("1/minute", "off", "k")
    async with AsyncSession(admin_engine) as s:
        assert (await s.scalars(select(m.RateCounter))).all() == []


async def test_unreachable_database_refuses_login(
    monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    @asynccontextmanager
    async def broken(_ctx: object) -> AsyncIterator[None]:
        down = True
        if down:
            raise OperationalError("INSERT", {}, ConnectionRefusedError("database is down"))
        yield

    monkeypatch.setattr(counters, "tenant_session", broken)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "down@db-limits.test", "password": "x" * 12}
    )
    assert resp.status_code == 503
    assert resp.json()["code"] == "rate-limit-unavailable"
