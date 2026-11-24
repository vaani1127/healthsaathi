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
from app.identity import recovery, service
from app.ledger.checkpoint import load_tree
from app.notify.email import outbox
from tests.helpers import make_clinic
from tests.identity.test_auth import (
    PASSWORD,
    StaffUser,
    bearer,
    make_user,
    refresh_with,
    sign_in,
)
from tests.identity.test_single_use import race, split

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


# Setting the new password -------------------------------------------------------------------------

CONFIRM = "/api/v1/auth/password-reset/confirm"
LOGIN = "/api/v1/auth/login"
NEW_PASSWORD = "a brand new pass phrase 42"


async def new_link(user: StaffUser) -> str:
    await recovery.process_reset_request(user.email)
    return token_from(reset_mails(user.email)[-1])


def confirm_body(user: StaffUser, token: str, code: str | None = None) -> dict[str, str]:
    body = {"email": user.email, "token": token, "password": NEW_PASSWORD}
    if code is not None:
        body["code"] = code
    return body


async def confirm(client: AsyncClient, body: dict[str, str]) -> tuple[int, str | None]:
    resp = await client.post(CONFIRM, json=body)
    code = resp.json().get("code") if resp.status_code != 204 else None
    return resp.status_code, code


async def test_reset_without_an_authenticator_still_leads_to_setup(
    client: AsyncClient, admin_engine: AsyncEngine, clinic: uuid.UUID
) -> None:
    user = await make_user(admin_engine, clinic, Role.RECEPTION, with_totp=False)
    token = await new_link(user)
    assert await confirm(client, confirm_body(user, token)) == (204, None)

    old = await client.post(LOGIN, json={"email": user.email, "password": PASSWORD})
    assert old.status_code == 401
    new = await client.post(LOGIN, json={"email": user.email, "password": NEW_PASSWORD})
    assert new.status_code == 200
    # A password alone never gives a session: the authenticator is set up first.
    assert new.json()["status"] == "mfa_enrollment_required"
    assert "access_token" not in new.json()

    changed = outbox.last_to(user.email)
    assert changed is not None and changed.subject == "Your HealthSaathi password was changed"
    assert "contact your clinic admin" in changed.text
    assert token not in changed.text


async def test_reset_with_an_authenticator_needs_a_current_code(
    client: AsyncClient, staff: StaffUser, admin_engine: AsyncEngine
) -> None:
    token = await new_link(staff)
    missing = await confirm(client, confirm_body(staff, token))
    assert missing == (400, "authenticator-code-required")
    assert await confirm(client, confirm_body(staff, token, "000000")) == (
        400,
        "invalid-reset-link",
    )
    # Both refusals left the link unused.
    assert await reset_rows(admin_engine, staff.email, open_only=True) == 1

    code = staff.code()
    assert await confirm(client, confirm_body(staff, token, code)) == (204, None)

    login = await client.post(LOGIN, json={"email": staff.email, "password": NEW_PASSWORD})
    assert login.status_code == 200
    assert login.json()["status"] == "mfa_required"
    challenge = login.json()["challenge_token"]
    # The code used for the reset cannot be used again to sign in.
    reused = await client.post(
        "/api/v1/auth/totp/verify", json={"challenge_token": challenge, "code": code}
    )
    assert reused.status_code == 401
    fresh = await client.post(
        "/api/v1/auth/totp/verify", json={"challenge_token": challenge, "code": staff.code(1)}
    )
    assert fresh.status_code == 200


async def test_a_code_already_used_to_sign_in_is_refused_and_the_link_stays_unused(
    client: AsyncClient, staff: StaffUser, admin_engine: AsyncEngine
) -> None:
    await sign_in(client, staff)  # takes the current step
    token = await new_link(staff)
    assert await confirm(client, confirm_body(staff, token, staff.code())) == (
        400,
        "invalid-reset-link",
    )
    assert await reset_rows(admin_engine, staff.email, open_only=True) == 1
    assert await confirm(client, confirm_body(staff, token, staff.code(1))) == (204, None)


async def test_wrong_used_expired_and_older_links_get_the_same_answer(
    client: AsyncClient, staff: StaffUser, admin_engine: AsyncEngine
) -> None:
    older = await new_link(staff)
    newest = await new_link(staff)
    bad = (400, "invalid-reset-link")
    assert await confirm(client, confirm_body(staff, "x" * 43, staff.code())) == bad
    assert await confirm(client, confirm_body(staff, older, staff.code())) == bad

    async with AsyncSession(admin_engine) as s, s.begin():
        await s.execute(
            update(m.EmailOtp)
            .where(m.EmailOtp.email == staff.email, m.EmailOtp.used_at.is_(None))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert await confirm(client, confirm_body(staff, newest, staff.code())) == bad

    fresh = await new_link(staff)
    assert await confirm(client, confirm_body(staff, fresh, staff.code())) == (204, None)
    assert await confirm(client, confirm_body(staff, fresh, staff.code(1))) == bad


async def test_two_resets_with_one_link_at_once_succeed_once(
    admin_engine: AsyncEngine, clinic: uuid.UUID
) -> None:
    user = await make_user(admin_engine, clinic, Role.NURSE, with_totp=False)
    token = await new_link(user)

    def attempt() -> Coroutine[Any, Any, None]:
        return recovery.confirm_reset(user.email, token, NEW_PASSWORD, None)

    results = await race(
        admin_engine,
        "SELECT 1 FROM email_otps WHERE email = :email AND purpose = 'password_reset' "
        "AND used_at IS NULL FOR UPDATE",
        attempt,
        email=user.email,
    )
    ok, failed = split(results)
    assert len(ok) == 1 and len(failed) == 1
    assert getattr(failed[0], "code", None) == "invalid-reset-link"
    assert [e["action"] for e in await reset_events(admin_engine, clinic)] == [
        "requested",
        "completed",
    ]


async def test_every_session_is_signed_out(
    client: AsyncClient, staff: StaffUser, admin_engine: AsyncEngine
) -> None:
    device = service.DeviceInfo(fingerprint=None, user_agent=None)
    sessions = [await service.create_session(staff.user_id, "mfa", device) for _ in range(2)]
    for issued in sessions:
        me = await client.get("/api/v1/auth/me", headers=bearer(issued.access_token))
        assert me.status_code == 200

    token = await new_link(staff)
    assert await confirm(client, confirm_body(staff, token, staff.code())) == (204, None)

    for issued in sessions:
        me = await client.get("/api/v1/auth/me", headers=bearer(issued.access_token))
        assert me.status_code == 401
        assert issued.refresh_token is not None
        assert (await refresh_with(client, issued.refresh_token)).status_code == 401
    async with AsyncSession(admin_engine) as s:
        still_open = await s.scalar(
            select(func.count())
            .select_from(m.UserSession)
            .where(m.UserSession.user_id == staff.user_id, m.UserSession.revoked_at.is_(None))
        )
    assert still_open == 0


async def test_a_reset_clears_the_password_lockout(client: AsyncClient, staff: StaffUser) -> None:
    for _ in range(5):
        await client.post(LOGIN, json={"email": staff.email, "password": "wrong-password-1"})
    locked = await client.post(LOGIN, json={"email": staff.email, "password": "wrong-password-1"})
    assert locked.status_code == 429

    token = await new_link(staff)
    assert await confirm(client, confirm_body(staff, token, staff.code())) == (204, None)
    login = await client.post(LOGIN, json={"email": staff.email, "password": NEW_PASSWORD})
    assert login.status_code == 200


async def test_a_weak_or_same_password_is_refused_and_the_link_stays_unused(
    client: AsyncClient, admin_engine: AsyncEngine, clinic: uuid.UUID
) -> None:
    user = await make_user(admin_engine, clinic, Role.DOCTOR, with_totp=False)
    token = await new_link(user)
    weak = await confirm(client, {**confirm_body(user, token), "password": "short"})
    same = await confirm(client, {**confirm_body(user, token), "password": PASSWORD})
    assert weak[0] == 422
    assert same == (422, "same-password")
    assert await reset_rows(admin_engine, user.email, open_only=True) == 1


async def test_completed_reset_is_audited_without_secrets(
    client: AsyncClient, staff: StaffUser, clinic: uuid.UUID, admin_engine: AsyncEngine
) -> None:
    token = await new_link(staff)
    assert await confirm(client, confirm_body(staff, token, staff.code())) == (204, None)
    events = await reset_events(admin_engine, clinic)
    assert [e["action"] for e in events] == ["requested", "completed"]
    text = str(events)
    assert token not in text and NEW_PASSWORD not in text and staff.email not in text
    async with AsyncSession(admin_engine) as s:
        await load_tree(s, clinic)


async def test_confirm_is_limited_per_ip(client: AsyncClient, staff: StaffUser) -> None:
    statuses = [
        (await confirm(client, confirm_body(staff, "y" * 43, "123456")))[0] for _ in range(6)
    ]
    assert statuses == [400] * 5 + [429]
