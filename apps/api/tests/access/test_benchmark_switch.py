"""The load-test switch (BENCHMARK_SKIP_RECORDING) never weakens authorisation and only exists in
APP_ENV=test."""

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings, get_settings
from app.db import models as m
from app.db.enums import Role
from tests.helpers import make_actor, make_clinic, make_patient

SECRETS = {
    "JWT_SECRET": "j" * 40,
    "OTP_HMAC_KEY": "o" * 40,
    "DATA_KEYS": "k1:" + "A" * 43 + "=",
    "LEDGER_SIGNING_SEED": "s" * 40,
}


@pytest.mark.parametrize("env", ["local", "staging", "prod"])
def test_the_switch_is_refused_outside_test(env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {**SECRETS, "APP_ENV": env, "BENCHMARK_SKIP_RECORDING": "true"}.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError, match="only allowed with APP_ENV=test"):
        Settings(_env_file=None)


async def _accesses(engine: AsyncEngine, patient: object) -> int:
    async with AsyncSession(engine) as s:
        return int(
            await s.scalar(
                select(func.count())
                .select_from(m.AccessEvent)
                .where(m.AccessEvent.patient_id == patient)
            )
            or 0
        )


async def test_with_the_switch_reads_are_authorised_but_not_recorded(
    admin_engine: AsyncEngine, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    clinic = await make_clinic(admin_engine)
    nurse = await make_actor(admin_engine, clinic, Role.NURSE)
    own = await make_actor(admin_engine, clinic, Role.PATIENT)
    patient = await make_patient(admin_engine, clinic, nurse.user_id)
    settings = get_settings()

    async def read(token: str) -> int:
        resp = await client.get(
            f"/api/v1/patients/{patient}/chart", headers={"Authorization": f"Bearer {token}"}
        )
        return resp.status_code

    assert await read(nurse.token) == 200
    recorded = await _accesses(admin_engine, patient)
    assert recorded >= 1

    monkeypatch.setattr(settings, "benchmark_skip_recording", True)
    assert await read(nurse.token) == 200
    assert await _accesses(admin_engine, patient) == recorded  # nothing written
    # Authorisation still runs: a patient account may not open someone else's chart.
    assert await read(own.token) == 403

    monkeypatch.setattr(settings, "benchmark_skip_recording", False)
    assert await read(nurse.token) == 200
    assert await _accesses(admin_engine, patient) > recorded
