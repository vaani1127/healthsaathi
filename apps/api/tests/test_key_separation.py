"""The cutover to derived keys (ADR 0009): new values use the derived keys, old rows stay as they
are, and the values that must keep working across the deploy do."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.access.service import _ip_hash
from app.consent.service import commitment, verify_commitment
from app.core import keys
from app.core.errors import ProblemError
from app.db import models as m
from app.db.enums import AccessAction, AccessDecision, ResourceType, Role
from app.identity import service
from app.notify.email import outbox
from tests.factories import ClinicGraph, build_clinic_graph
from tests.helpers import make_actor, make_clinic, make_patient
from tests.identity.test_auth import make_patient as make_patient_login
from tests.identity.test_auth import make_user

DEVICE = service.DeviceInfo(None, "key-separation")


@pytest.fixture(scope="module")
async def clinic(admin_engine: AsyncEngine) -> ClinicGraph:
    return await build_clinic_graph(admin_engine, "key-separation")


def legacy(message: str) -> str:
    return keys.legacy_hmac_hex(message.encode())


async def add_otp(
    engine: AsyncEngine, email: str, purpose: str, code_hash: str, ttl: timedelta
) -> None:
    async with AsyncSession(engine) as s, s.begin():
        s.add(
            m.EmailOtp(
                email=email,
                code_hash=code_hash,
                purpose=purpose,
                expires_at=datetime.now(UTC) + ttl,
            )
        )


# Email codes ---------------------------------------------------------------------------------


async def test_email_codes_use_the_email_code_key(
    clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    email = await make_patient_login(admin_engine, clinic)
    await service.request_email_code(email)
    sent = outbox.last_to(email)
    assert sent is not None
    code = next(w for w in sent.text.replace(".", " ").split() if w.isdigit() and len(w) == 6)
    async with AsyncSession(admin_engine) as s:
        stored = await s.scalar(select(m.EmailOtp.code_hash).where(m.EmailOtp.email == email))
    assert stored == keys.hmac_hex(keys.EMAIL_CODE, f"{email}|login|{code}".encode())
    assert stored != legacy(f"{email}|login|{code}")


async def test_an_email_code_from_before_the_cutover_simply_fails(
    clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    email = await make_patient_login(admin_engine, clinic)
    await add_otp(
        admin_engine, email, "login", legacy(f"{email}|login|123456"), timedelta(minutes=10)
    )
    with pytest.raises(ProblemError) as refused:
        await service.verify_email_code(email, "123456", DEVICE)
    assert refused.value.code == "invalid-code"


# Invites -------------------------------------------------------------------------------------


async def invited_user(engine: AsyncEngine, clinic: ClinicGraph) -> str:
    user = await make_user(engine, clinic.clinic_id, Role.NURSE, with_totp=False, password=None)
    return user.email


async def password_set(engine: AsyncEngine, email: str) -> bool:
    async with AsyncSession(engine) as s:
        return (
            await s.scalar(select(m.User.password_hash).where(m.User.email == email))
        ) is not None


async def test_an_invite_made_with_the_old_key_still_works_once(
    clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    email = await invited_user(admin_engine, clinic)
    token = "old-key-invite-token"
    await add_otp(
        admin_engine, email, "invite", legacy(f"{email}|invite|{token}"), timedelta(hours=72)
    )
    await service.accept_invite(email, token, "a long enough pass phrase")
    assert await password_set(admin_engine, email)
    with pytest.raises(ProblemError) as again:
        await service.accept_invite(email, token, "a long enough pass phrase")
    assert again.value.code == "invalid-invite"


async def test_an_invite_made_with_the_new_key_works(
    clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    email = await invited_user(admin_engine, clinic)
    token = "new-key-invite-token"
    await add_otp(
        admin_engine,
        email,
        "invite",
        keys.hmac_hex(keys.INVITE, f"{email}|invite|{token}".encode()),
        timedelta(hours=72),
    )
    await service.accept_invite(email, token, "a long enough pass phrase")
    assert await password_set(admin_engine, email)


async def test_an_expired_old_key_invite_is_refused(
    clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    email = await invited_user(admin_engine, clinic)
    token = "expired-old-invite"
    await add_otp(
        admin_engine, email, "invite", legacy(f"{email}|invite|{token}"), timedelta(hours=-1)
    )
    with pytest.raises(ProblemError) as refused:
        await service.accept_invite(email, token, "a long enough pass phrase")
    assert refused.value.code == "invalid-invite"


async def test_an_old_key_email_code_hash_is_not_accepted_as_an_invite(
    clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    # The fallback is only for invites: the purpose still has to match.
    email = await invited_user(admin_engine, clinic)
    token = "wrong-purpose"
    await add_otp(
        admin_engine, email, "login", legacy(f"{email}|invite|{token}"), timedelta(hours=72)
    )
    with pytest.raises(ProblemError):
        await service.accept_invite(email, token, "a long enough pass phrase")


# ip_hash -------------------------------------------------------------------------------------


async def test_new_access_events_use_the_ip_hash_key_and_old_rows_stay(
    admin_engine: AsyncEngine,
) -> None:
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    clinic = await make_clinic(admin_engine)
    reception = await make_actor(admin_engine, clinic, Role.RECEPTION)
    patient = await make_patient(admin_engine, clinic, reception.user_id)
    old_hash = legacy("ip:198.51.100.77")
    old_id = uuid.uuid4()
    async with AsyncSession(admin_engine) as s, s.begin():
        s.add(
            m.AccessEvent(
                id=old_id,
                at=datetime.now(UTC) - timedelta(days=1),
                clinic_id=clinic,
                user_id=reception.user_id,
                role=Role.RECEPTION,
                patient_id=patient,
                resource_type=ResourceType.DEMOGRAPHICS,
                action=AccessAction.VIEW,
                ip_hash=old_hash,
                policy_version="old",
                decision=AccessDecision.ALLOW,
            )
        )
    transport = ASGITransport(app=create_app(), client=("198.51.100.77", 40000))
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.get(f"/api/v1/patients/{patient}", headers=reception.headers)
    assert resp.status_code == 200, resp.text
    async with AsyncSession(admin_engine) as s:
        rows = dict(
            (
                await s.execute(
                    select(m.AccessEvent.id, m.AccessEvent.ip_hash).where(
                        m.AccessEvent.clinic_id == clinic
                    )
                )
            ).all()
        )
    assert rows.pop(old_id) == old_hash
    assert list(rows.values()) == [keys.hmac_hex(keys.IP_HASH, b"ip:198.51.100.77")]
    assert _ip_hash("198.51.100.77") == keys.hmac_hex(keys.IP_HASH, b"ip:198.51.100.77")


# Consent commitments -------------------------------------------------------------------------


def test_new_commitments_use_their_key_and_both_kinds_verify() -> None:
    consent, patient, notice = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    message = f"consent|{consent}|{patient}|{notice}"
    new = commitment(consent, patient, notice)
    assert new == keys.hmac_hex(keys.CONSENT_COMMITMENT, message.encode())
    old = legacy(message)
    assert new != old
    assert verify_commitment(consent, patient, notice, new)
    assert verify_commitment(consent, patient, notice, old)
    assert not verify_commitment(consent, patient, uuid.uuid4(), new)
    assert not verify_commitment(consent, patient, notice, "0" * 64)
