"""Staff password recovery (ADR 0010): the request answers the same for everyone and does its
work after the response; one valid link per person; limits; audit events."""

import uuid
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.counters import counter_key
from app.db import models as m
from app.db.enums import Role
from app.identity import recovery
from app.ledger.checkpoint import load_tree
from app.notify.email import outbox
from tests.helpers import make_clinic
from tests.identity.test_auth import StaffUser, make_user
from tests.identity.test_single_use import race

REQUEST = "/api/v1/auth/password-reset/request"
SAME_ANSWER = {"status": "sent_if_registered"}


@pytest.fixture
async def clinic(admin_engine: AsyncEngine) -> uuid.UUID:
    return await make_clinic(admin_engine)


@pytest.fixture
async def staff(admin_engine: AsyncEngine, clinic: uuid.UUID) -> StaffUser:
    return await make_user(admin_engine, clinic, Role.NURSE)


def reset_mails(email: str) -> list[str]:
    return [mail.text for mail in outbox.sent if mail.to == email]


def link_parts(text: str) -> tuple[str, dict[str, str]]:
    """The link in a reset email: (everything before '#', fragment fields)."""
    link = next(word for word in text.split() if "/reset-password" in word)
    base, _, fragment = link.partition("#")
    return base, {k: v[0] for k, v in parse_qs(fragment).items()}


def token_from(text: str) -> str:
    return link_parts(text)[1]["token"]


async def reset_rows(engine: AsyncEngine, email: str, *, open_only: bool = False) -> int:
    query = (
        select(func.count())
        .select_from(m.EmailOtp)
        .where(m.EmailOtp.email == email.lower(), m.EmailOtp.purpose == recovery.PURPOSE)
    )
    if open_only:
        query = query.where(m.EmailOtp.used_at.is_(None))
    async with AsyncSession(engine) as s:
        return int(await s.scalar(query) or 0)


async def reset_events(engine: AsyncEngine, clinic: uuid.UUID) -> list[dict[str, str]]:
    async with AsyncSession(engine) as s:
        rows = await s.scalars(
            select(m.AuditEvent.payload)
            .where(m.AuditEvent.clinic_id == clinic, m.AuditEvent.kind == "staff")
            .order_by(m.AuditEvent.seq)
        )
        return [p for p in rows.all() if p.get("type") == "password_reset"]


async def total_rows(engine: AsyncEngine, model: Any) -> int:
    async with AsyncSession(engine) as s:
        return int(await s.scalar(select(func.count()).select_from(model)) or 0)


async def counter_rows(engine: AsyncEngine, scope: str, *identifiers: str) -> int:
    async with AsyncSession(engine) as s:
        return int(
            await s.scalar(
                select(func.count())
                .select_from(m.RateCounter)
                .where(m.RateCounter.key == counter_key(scope, *identifiers))
            )
            or 0
        )


# The request --------------------------------------------------------------------------------------


async def test_staff_member_gets_one_link_in_the_fragment(
    client: AsyncClient, staff: StaffUser, admin_engine: AsyncEngine
) -> None:
    before = datetime.now(UTC)
    resp = await client.post(REQUEST, json={"email": staff.email})
    assert resp.status_code == 202
    assert resp.json() == SAME_ANSWER

    (text,) = reset_mails(staff.email)
    base, fields = link_parts(text)
    # The token is only in the fragment, never in the path or a query string.
    assert base.endswith("/reset-password")
    assert "?" not in base
    assert fields["email"] == staff.email
    assert "30 minutes" in text
    assert "nothing changes" in text

    async with AsyncSession(admin_engine) as s:
        row = await s.scalar(
            select(m.EmailOtp).where(
                m.EmailOtp.email == staff.email, m.EmailOtp.purpose == recovery.PURPOSE
            )
        )
    assert row is not None and row.used_at is None
    # Stored only as a keyed hash under the reset link key.
    assert row.code_hash == recovery.reset_hash(staff.email, fields["token"])
    assert fields["token"] not in row.code_hash
    assert timedelta(minutes=29) < row.expires_at - before <= timedelta(minutes=31)


async def test_unknown_emails_and_non_staff_get_the_same_answer_and_no_email(
    client: AsyncClient, clinic: uuid.UUID, admin_engine: AsyncEngine
) -> None:
    patient = await make_user(admin_engine, clinic, Role.PATIENT, with_totp=False, password=None)
    invited = await make_user(admin_engine, clinic, Role.DOCTOR, with_totp=False, password=None)
    inactive = await make_user(admin_engine, clinic, Role.RECEPTION)
    async with AsyncSession(admin_engine) as s, s.begin():
        await s.execute(update(m.User).where(m.User.id == inactive.user_id).values(is_active=False))
    left = await make_user(admin_engine, clinic, Role.LAB_TECH)
    async with AsyncSession(admin_engine) as s, s.begin():
        await s.execute(
            update(m.Membership).where(m.Membership.user_id == left.user_id).values(is_active=False)
        )

    emails = [
        f"nobody.{uuid.uuid4().hex[:8]}@reset.test",
        patient.email,
        invited.email,
        inactive.email,
        left.email,
    ]
    for email in emails:
        resp = await client.post(REQUEST, json={"email": email})
        assert (resp.status_code, resp.json()) == (202, SAME_ANSWER)
        assert reset_mails(email) == []
        assert await reset_rows(admin_engine, email) == 0
    assert await reset_events(admin_engine, clinic) == []


async def test_nothing_is_written_before_the_response(
    client: AsyncClient,
    staff: StaffUser,
    clinic: uuid.UUID,
    admin_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the per-IP limit runs before the answer. The rest is handed to a background task,
    held back here so the database can be checked as it is when the answer leaves."""
    held: list[str] = []

    async def hold(email: str) -> None:
        held.append(email)

    real = recovery.process_reset_request
    monkeypatch.setattr(recovery, "process_reset_request", hold)
    unknown = f"nobody.{uuid.uuid4().hex[:8]}@reset.test"
    otps, audits = (
        await total_rows(admin_engine, m.EmailOtp),
        await total_rows(admin_engine, m.AuditEvent),
    )

    for email in (unknown, staff.email):
        resp = await client.post(REQUEST, json={"email": email})
        assert (resp.status_code, resp.json()) == (202, SAME_ANSWER)

    assert held == [unknown, staff.email]
    assert await total_rows(admin_engine, m.EmailOtp) == otps
    assert await total_rows(admin_engine, m.AuditEvent) == audits
    for email in held:
        assert await counter_rows(admin_engine, "password-reset-email", email) == 0
    assert reset_mails(staff.email) == []

    # The held work, run now, sends the link to the staff member only.
    for email in held:
        await real(email)
    assert len(reset_mails(staff.email)) == 1
    assert reset_mails(unknown) == []
    assert await reset_rows(admin_engine, staff.email) == 1
    assert [e["action"] for e in await reset_events(admin_engine, clinic)] == ["requested"]


async def test_a_new_link_cancels_older_ones(staff: StaffUser, admin_engine: AsyncEngine) -> None:
    await recovery.process_reset_request(staff.email)
    await recovery.process_reset_request(staff.email)
    assert await reset_rows(admin_engine, staff.email) == 2
    assert await reset_rows(admin_engine, staff.email, open_only=True) == 1
    first, second = (token_from(t) for t in reset_mails(staff.email))
    async with AsyncSession(admin_engine) as s:
        open_hash = await s.scalar(
            select(m.EmailOtp.code_hash).where(
                m.EmailOtp.email == staff.email,
                m.EmailOtp.purpose == recovery.PURPOSE,
                m.EmailOtp.used_at.is_(None),
            )
        )
    assert open_hash == recovery.reset_hash(staff.email, second)
    assert open_hash != recovery.reset_hash(staff.email, first)


async def test_two_requests_at_once_leave_one_valid_link(
    staff: StaffUser, admin_engine: AsyncEngine
) -> None:
    def attempt() -> Coroutine[Any, Any, None]:
        return recovery.process_reset_request(staff.email)

    await race(
        admin_engine, "SELECT 1 FROM users WHERE id = :id FOR UPDATE", attempt, id=staff.user_id
    )
    assert len(reset_mails(staff.email)) == 2
    assert await reset_rows(admin_engine, staff.email, open_only=True) == 1


async def test_per_email_limit_still_answers_202_and_sends_nothing(
    client: AsyncClient, staff: StaffUser
) -> None:
    for _ in range(4):
        await recovery.process_reset_request(staff.email)
    # 3 an hour per email; the fourth sends nothing.
    assert len(reset_mails(staff.email)) == 3
    resp = await client.post(REQUEST, json={"email": staff.email})
    assert (resp.status_code, resp.json()) == (202, SAME_ANSWER)
    assert len(reset_mails(staff.email)) == 3


async def test_per_ip_limit_gives_429(client: AsyncClient) -> None:
    statuses = [
        (await client.post(REQUEST, json={"email": f"x{i}@reset.test"})).status_code
        for i in range(6)
    ]
    assert statuses == [202] * 5 + [429]


async def test_request_is_audited_in_every_clinic_of_the_staff_member(
    admin_engine: AsyncEngine, staff: StaffUser, clinic: uuid.UUID
) -> None:
    other = await make_clinic(admin_engine)
    third = await make_clinic(admin_engine)
    async with AsyncSession(admin_engine) as s, s.begin():
        s.add(m.Membership(clinic_id=other, user_id=staff.user_id, role=Role.DOCTOR))
        # A patient membership elsewhere is not a staff clinic.
        s.add(m.Membership(clinic_id=third, user_id=staff.user_id, role=Role.PATIENT))

    await recovery.process_reset_request(staff.email)

    token = token_from(reset_mails(staff.email)[0])
    for clinic_id in (clinic, other):
        (event,) = await reset_events(admin_engine, clinic_id)
        assert event["action"] == "requested"
        assert event["user_id"] == str(staff.user_id)
        assert set(event) == {"type", "action", "at", "user_id"}
        assert staff.email not in str(event) and token not in str(event)
        async with AsyncSession(admin_engine) as s:
            await load_tree(s, clinic_id)  # raises if the chain does not verify
    assert await reset_events(admin_engine, third) == []


async def test_reset_events_are_hidden_from_another_clinic(
    admin_engine: AsyncEngine, staff: StaffUser, clinic: uuid.UUID
) -> None:
    from app.core.db import TenantContext, tenant_session

    other = await make_clinic(admin_engine)
    await recovery.process_reset_request(staff.email)
    async with tenant_session(TenantContext(clinic_id=other, role=Role.CLINIC_ADMIN.value)) as db:
        seen = await db.scalar(
            select(func.count())
            .select_from(m.AuditEvent)
            .where(m.AuditEvent.clinic_id == clinic, m.AuditEvent.kind == "staff")
        )
    assert seen == 0
    assert len(await reset_events(admin_engine, clinic)) == 1


async def test_a_failure_in_the_background_is_swallowed(
    staff: StaffUser, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(email: str) -> None:
        raise RuntimeError("mail server down")

    monkeypatch.setattr(recovery, "_process_reset_request", broken)
    await recovery.process_reset_request(staff.email)  # does not raise
    assert "password reset request not processed" in caplog.text
