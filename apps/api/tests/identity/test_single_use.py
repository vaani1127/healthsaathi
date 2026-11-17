"""Single-use rules hold across API workers and restarts.

Each concurrency test runs two attempts at the same time on separate database sessions, as two
API workers would. To make sure they really overlap, a third connection holds a lock on the row
both need; the test waits until Postgres shows both attempts blocked on that lock, then lets go.
Exactly one attempt may succeed.
"""

import asyncio
import importlib
import time
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pyotp
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.errors import ProblemError
from app.db import models as m
from app.db.enums import Role
from app.identity import service, tokens, totp
from app.main import create_app
from tests.factories import ClinicGraph, build_clinic_graph
from tests.identity.test_auth import PASSWORD, StaffUser, make_user

DEVICE = service.DeviceInfo(fingerprint=None, user_agent="single-use test")


@pytest.fixture(scope="module")
async def clinic(admin_engine: AsyncEngine) -> ClinicGraph:
    return await build_clinic_graph(admin_engine, "single-use")


@asynccontextmanager
async def row_locked(engine: AsyncEngine, sql: str, **params: Any) -> AsyncIterator[None]:
    """Hold `SELECT ... FOR UPDATE` on a row until the block ends."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        found = await conn.execute(text(sql), params)
        assert found.first() is not None, "the row to lock is missing"
        try:
            yield
        finally:
            await trans.rollback()


async def wait_for_lock_waiters(engine: AsyncEngine, count: int) -> None:
    deadline = time.monotonic() + 10
    while True:
        async with engine.connect() as conn:
            waiting = await conn.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
        if waiting is not None and waiting >= count:
            return
        assert time.monotonic() < deadline, f"only {waiting} of {count} attempts reached the lock"
        await asyncio.sleep(0.05)


async def race(
    engine: AsyncEngine,
    lock_sql: str,
    attempt: Callable[[], Coroutine[Any, Any, Any]],
    **params: Any,
) -> list[Any]:
    """Run two attempts at once, both blocked on the same row until both are waiting."""
    async with row_locked(engine, lock_sql, **params):
        tasks: list[asyncio.Task[Any]] = [asyncio.create_task(attempt()) for _ in range(2)]
        try:
            await wait_for_lock_waiters(engine, 2)
        except AssertionError:
            for task in tasks:
                task.cancel()
            raise
    return list(await asyncio.gather(*tasks, return_exceptions=True))


def split(results: list[Any]) -> tuple[list[Any], list[BaseException]]:
    ok = [r for r in results if not isinstance(r, BaseException)]
    failed = [r for r in results if isinstance(r, BaseException)]
    return ok, failed


def challenge(user: StaffUser) -> str:
    token, _ = tokens.encode_token("mfa_challenge", user.user_id, timedelta(minutes=5))
    return token


async def last_step(engine: AsyncEngine, user_id: uuid.UUID) -> int | None:
    async with AsyncSession(engine) as s:
        value: int | None = await s.scalar(
            select(m.MfaSecret.last_step).where(m.MfaSecret.user_id == user_id)
        )
        return value


# Authenticator codes ------------------------------------------------------------------------------


async def test_same_code_twice_at_once_is_accepted_once(
    clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    user = await make_user(admin_engine, clinic.clinic_id, Role.DOCTOR)
    code = pyotp.TOTP(user.totp_secret).now()

    results = await race(
        admin_engine,
        "SELECT 1 FROM mfa_secrets WHERE user_id = :user FOR UPDATE",
        lambda: service.verify_totp(challenge(user), code, DEVICE),
        user=user.user_id,
    )

    ok, failed = split(results)
    assert len(ok) == 1, results
    assert isinstance(ok[0], service.IssuedTokens)
    assert len(failed) == 1
    assert isinstance(failed[0], ProblemError)
    assert failed[0].code == "invalid-code"
    assert await last_step(admin_engine, user.user_id) is not None


async def test_two_sessions_claiming_one_step_change_one_row(
    clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    """The claim itself: the second session waits for the first one's row lock, then finds the
    step already taken."""
    from app.core.db import ANONYMOUS, tenant_session

    user = await make_user(admin_engine, clinic.clinic_id, Role.DOCTOR)
    step = int(time.time() // totp.STEP_SECONDS)

    async def claim() -> bool:
        async with tenant_session(ANONYMOUS) as db:
            return await service._claim_step(db, user.user_id, step)

    async with tenant_session(ANONYMOUS) as first:
        assert await service._claim_step(first, user.user_id, step)
        second = asyncio.create_task(claim())
        await wait_for_lock_waiters(admin_engine, 1)
        assert not second.done()
    assert await second is False
    assert await last_step(admin_engine, user.user_id) == step


async def test_code_is_refused_after_a_restart(
    clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    user = await make_user(admin_engine, clinic.clinic_id, Role.DOCTOR)
    code = pyotp.TOTP(user.totp_secret).now()
    await service.verify_totp(challenge(user), code, DEVICE)

    # A restarted worker: the TOTP module is loaded again and a new app is built. Nothing kept
    # in process memory survives; the database still knows the step was used.
    importlib.reload(totp)
    assert not hasattr(totp, "_last_step")
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as c:
        login = await c.post("/api/v1/auth/login", json={"email": user.email, "password": PASSWORD})
        assert login.status_code == 200, login.text
        again = await c.post(
            "/api/v1/auth/totp/verify",
            json={"challenge_token": login.json()["challenge_token"], "code": code},
        )
    assert again.status_code == 401
    assert again.json()["code"] == "invalid-code"


async def test_older_step_after_a_newer_one_is_refused(
    clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    user = await make_user(admin_engine, clinic.clinic_id, Role.DOCTOR)
    current = int(time.time() // totp.STEP_SECONDS)
    otp = pyotp.TOTP(user.totp_secret)
    newer = otp.at((current + 1) * totp.STEP_SECONDS)
    older = otp.at(current * totp.STEP_SECONDS)

    await service.verify_totp(challenge(user), newer, DEVICE)
    with pytest.raises(ProblemError) as refused:
        await service.verify_totp(challenge(user), older, DEVICE)
    assert refused.value.code == "invalid-code"
    assert await last_step(admin_engine, user.user_id) == current + 1


async def test_wrong_code_claims_nothing(clinic: ClinicGraph, admin_engine: AsyncEngine) -> None:
    user = await make_user(admin_engine, clinic.clinic_id, Role.DOCTOR)
    current = int(time.time() // totp.STEP_SECONDS)
    otp = pyotp.TOTP(user.totp_secret)
    valid = {otp.at((current + d) * totp.STEP_SECONDS) for d in (-2, -1, 0, 1, 2)}
    wrong = next(
        c for c in ("000000", "111111", "222222", "333333", "444444", "555555") if c not in valid
    )
    with pytest.raises(ProblemError):
        await service.verify_totp(challenge(user), wrong, DEVICE)
    assert await last_step(admin_engine, user.user_id) is None
    # The right code still works afterwards.
    await service.verify_totp(challenge(user), user.code(), DEVICE)


async def test_new_enrolment_secret_forgets_old_steps(
    clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    user = await make_user(admin_engine, clinic.clinic_id, Role.RECEPTION, with_totp=False)
    enroll, _ = tokens.encode_token("mfa_enroll", user.user_id, timedelta(minutes=10))
    await service.start_totp_enrolment(enroll)
    current = int(time.time() // totp.STEP_SECONDS)
    async with AsyncSession(admin_engine) as s, s.begin():
        row = await s.get(m.MfaSecret, user.user_id)
        assert row is not None
        row.last_step = current + 1
    # Starting again issues a new secret and clears the step kept for the old one.
    secret, _ = await service.start_totp_enrolment(enroll)
    assert await last_step(admin_engine, user.user_id) is None
    issued = await service.activate_totp(enroll, pyotp.TOTP(secret).now(), DEVICE)
    assert issued.refresh_token
    assert await last_step(admin_engine, user.user_id) is not None
