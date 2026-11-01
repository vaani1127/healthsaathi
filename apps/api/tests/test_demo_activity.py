import asyncio
import random
from datetime import datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db import models as m
from app.db.enums import Role
from app.scripts import demo_activity as bot
from tests.helpers import make_actor, make_clinic, make_patient

IST = bot.TZ


def test_opd_hours_in_clinic_time() -> None:
    monday = datetime(2026, 11, 2, tzinfo=IST)
    assert bot.is_open(monday.replace(hour=10))
    assert not bot.is_open(monday.replace(hour=14))
    assert bot.is_open(monday.replace(hour=19, minute=59))
    assert not bot.is_open(monday.replace(hour=20))
    sunday = datetime(2026, 11, 1, tzinfo=IST)
    assert bot.is_open(sunday.replace(hour=12))
    assert not bot.is_open(sunday.replace(hour=18))


def test_next_open_skips_to_the_next_session() -> None:
    after_lunch = datetime(2026, 11, 2, 14, 5, tzinfo=IST)
    assert bot.next_open(after_lunch) == datetime(2026, 11, 2, 17, 0, tzinfo=IST)
    sunday_night = datetime(2026, 11, 1, 21, 0, tzinfo=IST)
    assert bot.next_open(sunday_night) == datetime(2026, 11, 2, 9, 30, tzinfo=IST)


def test_vitals_are_valid_for_the_api() -> None:
    from app.clinical.schemas import VitalsIn

    rng = random.Random(1)
    for _ in range(50):
        VitalsIn(**bot.vitals(rng))


async def test_a_visit_goes_through_the_normal_recording(
    admin_engine: AsyncEngine, client: httpx.AsyncClient
) -> None:
    clinic = await make_clinic(admin_engine)
    actors = {
        role: await make_actor(admin_engine, clinic, role)
        for role in (Role.RECEPTION, Role.NURSE, Role.DOCTOR)
    }
    patient = await make_patient(admin_engine, clinic, actors[Role.RECEPTION].user_id)
    lock = asyncio.Lock()

    def session(role: Role) -> bot.Session:
        s = bot.Session(client, bot.Login("x@demo.test", "JBSWY3DPEHPK3PXP"), "pw", lock)
        s.token, s.signed_in_at = actors[role].token, datetime.now(IST)
        return s

    visit = bot.Clinic(session(Role.RECEPTION), session(Role.NURSE), session(Role.DOCTOR))
    assert await visit.visit(random.Random(0)) == str(patient)

    async with AsyncSession(admin_engine) as s:
        doctor_reads = (
            await s.execute(
                select(m.AccessEvent.id, m.AccessExplanation.template_code)
                .join(m.AccessExplanation, m.AccessExplanation.access_event_id == m.AccessEvent.id)
                .where(
                    m.AccessEvent.patient_id == patient,
                    m.AccessEvent.user_id == actors[Role.DOCTOR].user_id,
                )
            )
        ).all()
    assert doctor_reads, "the doctor's chart read was not recorded"
    # The walk-in token explains the doctor's access.
    assert all(code is not None for _, code in doctor_reads)


async def test_sign_in_answers_the_authenticator_challenge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bot, "LOGIN_SPACING_SECONDS", 0)
    seen: list[tuple[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode() or None
        seen.append((request.url.path, body))
        if request.url.path == "/api/v1/auth/login":
            return httpx.Response(200, json={"challenge_token": "c"})
        return httpx.Response(200, json={"access_token": "t", "clinic_id": "k", "role": "nurse"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://x") as c:
        s = bot.Session(c, bot.Login("n@demo.test", "JBSWY3DPEHPK3PXP"), "pw", asyncio.Lock())
        assert await s.headers() == {"Authorization": "Bearer t"}
    assert [p for p, _ in seen] == ["/api/v1/auth/login", "/api/v1/auth/totp/verify"]
    assert '"code"' in (seen[1][1] or "")
