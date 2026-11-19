"""Sending a staff invite again (APP_GAPS 35): only the newest link works, refused cases, limits,
the audit event, and the new audit kind does not disturb receipts, detection or research."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db import tenant_session
from app.core.errors import ProblemError
from app.db import models as m
from app.db.enums import Role
from app.detect import days as detect_days
from app.detect.loader import load_clinic
from app.identity import service
from app.ledger.checkpoint import build_checkpoint
from app.notify.email import outbox
from e2d_core.ledger import TreeHead, leaf_hash, verify_inclusion
from tests.helpers import Actor, make_actor, make_clinic, make_patient
from tests.identity.test_single_use import race

PASSWORD = "a long enough pass phrase"


async def pending_member(
    engine: AsyncEngine, clinic: uuid.UUID, role: Role = Role.NURSE
) -> tuple[uuid.UUID, str]:
    """A staff member with a membership and no password yet. Returns (membership id, email)."""
    email = f"pending.{uuid.uuid4().hex[:10]}@invite.test"
    async with AsyncSession(engine) as s, s.begin():
        user = m.User(email=email, name="Pending Member")
        s.add(user)
        await s.flush()
        membership = m.Membership(clinic_id=clinic, user_id=user.id, role=role)
        s.add(membership)
        await s.flush()
        return membership.id, email


def links_to(email: str) -> list[str]:
    return [mail.text.split("token=")[1].split()[0] for mail in outbox.sent if mail.to == email]


async def open_invites(engine: AsyncEngine, email: str) -> int:
    async with AsyncSession(engine) as s:
        return int(
            await s.scalar(
                select(func.count())
                .select_from(m.EmailOtp)
                .where(
                    m.EmailOtp.email == email,
                    m.EmailOtp.purpose == "invite",
                    m.EmailOtp.used_at.is_(None),
                )
            )
            or 0
        )


async def staff_events(engine: AsyncEngine, clinic: uuid.UUID) -> list[dict[str, str]]:
    async with AsyncSession(engine) as s:
        rows = await s.scalars(
            select(m.AuditEvent.payload)
            .where(m.AuditEvent.clinic_id == clinic, m.AuditEvent.kind == "staff")
            .order_by(m.AuditEvent.seq)
        )
        return list(rows.all())


@pytest.fixture
async def clinic(admin_engine: AsyncEngine) -> uuid.UUID:
    return await make_clinic(admin_engine)


@pytest.fixture
async def admin(admin_engine: AsyncEngine, clinic: uuid.UUID) -> Actor:
    return await make_actor(admin_engine, clinic, Role.CLINIC_ADMIN)


def resend_url(membership_id: uuid.UUID) -> str:
    return f"/api/v1/staff/members/{membership_id}/invite"


# The new link replaces the old one ------------------------------------------------------------


async def test_the_old_link_stops_working_and_the_new_one_works(
    client: AsyncClient, admin_engine: AsyncEngine, clinic: uuid.UUID, admin: Actor
) -> None:
    membership, email = await pending_member(admin_engine, clinic)
    assert (await client.post(resend_url(membership), headers=admin.headers)).status_code == 204
    assert (await client.post(resend_url(membership), headers=admin.headers)).status_code == 204
    old, new = links_to(email)
    assert await open_invites(admin_engine, email) == 1
    with pytest.raises(ProblemError) as refused:
        await service.accept_invite(email, old, PASSWORD)
    assert refused.value.code == "invalid-invite"
    await service.accept_invite(email, new, PASSWORD)
    members = await client.get("/api/v1/staff/members", headers=admin.headers)
    row = next(r for r in members.json() if r["membership_id"] == str(membership))
    assert row["password_set"] is True
    assert "password_hash" not in row


async def test_two_resends_at_once_leave_exactly_one_valid_invite(
    admin_engine: AsyncEngine, clinic: uuid.UUID, admin: Actor
) -> None:
    membership, email = await pending_member(admin_engine, clinic)
    results = await race(
        admin_engine,
        "SELECT 1 FROM memberships WHERE id = :id FOR UPDATE",
        lambda: service.invite_again(admin.ctx, membership),
        id=membership,
    )
    assert results == [None, None]
    assert await open_invites(admin_engine, email) == 1
    tokens = links_to(email)
    assert len(tokens) == 2
    works = 0
    for token in tokens:
        try:
            await service.accept_invite(email, token, PASSWORD)
            works += 1
        except ProblemError:
            pass
    assert works == 1


async def test_two_resends_waiting_on_the_open_invite_leave_exactly_one_valid(
    admin_engine: AsyncEngine, clinic: uuid.UUID, admin: Actor
) -> None:
    """Both resends overlap at the update of the open invite. Without the membership lock each
    update would miss the invite the other one inserts, leaving two valid links."""
    membership, email = await pending_member(admin_engine, clinic)
    await service.invite_again(admin.ctx, membership)
    results = await race(
        admin_engine,
        "SELECT 1 FROM email_otps WHERE email = :email AND purpose = 'invite' "
        "AND used_at IS NULL FOR UPDATE",
        lambda: service.invite_again(admin.ctx, membership),
        email=email,
    )
    assert results == [None, None]
    assert await open_invites(admin_engine, email) == 1


# Refused cases --------------------------------------------------------------------------------


async def test_a_member_with_a_password_is_refused(
    client: AsyncClient, admin_engine: AsyncEngine, clinic: uuid.UUID, admin: Actor
) -> None:
    membership, email = await pending_member(admin_engine, clinic)
    await client.post(resend_url(membership), headers=admin.headers)
    await service.accept_invite(email, links_to(email)[-1], PASSWORD)
    resp = await client.post(resend_url(membership), headers=admin.headers)
    assert resp.status_code == 409
    assert resp.json()["code"] == "already-has-password"


async def test_an_inactive_membership_is_refused(
    client: AsyncClient, admin_engine: AsyncEngine, clinic: uuid.UUID, admin: Actor
) -> None:
    membership, _ = await pending_member(admin_engine, clinic)
    async with AsyncSession(admin_engine) as s, s.begin():
        row = await s.get(m.Membership, membership)
        assert row is not None
        row.is_active = False
    resp = await client.post(resend_url(membership), headers=admin.headers)
    assert resp.status_code == 409
    assert resp.json()["code"] == "membership-inactive"


async def test_another_clinics_member_is_not_found(
    client: AsyncClient, admin_engine: AsyncEngine, admin: Actor
) -> None:
    other_clinic = await make_clinic(admin_engine)
    membership, email = await pending_member(admin_engine, other_clinic)
    resp = await client.post(resend_url(membership), headers=admin.headers)
    assert resp.status_code == 404
    assert resp.json()["code"] == "membership-not-found"
    assert links_to(email) == []


async def test_a_patient_membership_is_not_found(
    client: AsyncClient, admin_engine: AsyncEngine, clinic: uuid.UUID, admin: Actor
) -> None:
    membership, _ = await pending_member(admin_engine, clinic, Role.PATIENT)
    resp = await client.post(resend_url(membership), headers=admin.headers)
    assert resp.status_code == 404
    assert resp.json()["code"] == "membership-not-found"


async def test_only_a_clinic_admin_may_resend(
    client: AsyncClient, admin_engine: AsyncEngine, clinic: uuid.UUID
) -> None:
    membership, _ = await pending_member(admin_engine, clinic)
    doctor = await make_actor(admin_engine, clinic, Role.DOCTOR)
    resp = await client.post(resend_url(membership), headers=doctor.headers)
    assert resp.status_code == 403


# Limits ---------------------------------------------------------------------------------------


async def test_the_fourth_resend_in_an_hour_is_refused(
    client: AsyncClient, admin_engine: AsyncEngine, clinic: uuid.UUID, admin: Actor
) -> None:
    membership, _ = await pending_member(admin_engine, clinic)
    for _ in range(3):
        assert (await client.post(resend_url(membership), headers=admin.headers)).status_code == 204
    refused = await client.post(resend_url(membership), headers=admin.headers)
    assert refused.status_code == 429
    assert refused.json()["code"] == "rate-limited"
    assert 1 <= int(refused.headers["retry-after"]) <= 3600


async def test_one_admin_may_resend_20_an_hour(
    client: AsyncClient, admin_engine: AsyncEngine, clinic: uuid.UUID, admin: Actor
) -> None:
    for _ in range(20):
        membership, _ = await pending_member(admin_engine, clinic)
        assert (await client.post(resend_url(membership), headers=admin.headers)).status_code == 204
    membership, _ = await pending_member(admin_engine, clinic)
    refused = await client.post(resend_url(membership), headers=admin.headers)
    assert refused.status_code == 429


# Email failure --------------------------------------------------------------------------------


async def test_a_failed_email_answers_502_and_the_next_resend_works(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    clinic: uuid.UUID,
    admin: Actor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    membership, email = await pending_member(admin_engine, clinic)

    async def broken(_email: object) -> None:
        raise RuntimeError("mail server down")

    monkeypatch.setattr(service, "send_email", broken)
    failed = await client.post(resend_url(membership), headers=admin.headers)
    assert failed.status_code == 502
    assert failed.json()["code"] == "invite-email-failed"
    # The invite row stays; the next resend replaces it.
    assert await open_invites(admin_engine, email) == 1
    monkeypatch.undo()
    assert (await client.post(resend_url(membership), headers=admin.headers)).status_code == 204
    assert len(links_to(email)) == 1
    assert await open_invites(admin_engine, email) == 1
    await service.accept_invite(email, links_to(email)[0], PASSWORD)


# Audit ----------------------------------------------------------------------------------------


async def test_first_invite_and_resend_are_audited_without_the_email(
    client: AsyncClient, admin_engine: AsyncEngine, clinic: uuid.UUID, admin: Actor
) -> None:
    email = f"audited.{uuid.uuid4().hex[:10]}@invite.test"
    first = await client.post(
        "/api/v1/staff/invites",
        json={"email": email, "name": "Audited Nurse", "role": "nurse"},
        headers=admin.headers,
    )
    assert first.status_code == 201
    membership = first.json()["membership_id"]
    assert (
        await client.post(f"/api/v1/staff/members/{membership}/invite", headers=admin.headers)
    ).status_code == 204
    events = await staff_events(admin_engine, clinic)
    assert [e["action"] for e in events] == ["invite", "invite_again"]
    for event in events:
        assert event["type"] == "staff_invite"
        assert event["membership_id"] == membership
        assert event["user_id"] == first.json()["user_id"]
        assert event["role"] == "nurse"
        assert event["by"] == str(admin.user_id)
        assert email not in str(event)


# The new audit kind is ignored where only accesses count ---------------------------------------


async def test_staff_events_do_not_disturb_receipts_detection_or_research(
    client: AsyncClient, admin_engine: AsyncEngine, clinic: uuid.UUID, admin: Actor
) -> None:
    reception = await make_actor(admin_engine, clinic, Role.RECEPTION)
    patient_actor = await make_actor(admin_engine, clinic, Role.PATIENT)
    patient = await make_patient(admin_engine, clinic, reception.user_id, patient_actor.user_id)
    before, _ = await pending_member(admin_engine, clinic)
    await service.invite_again(admin.ctx, before)

    # One access between two staff events.
    assert (
        await client.get(f"/api/v1/patients/{patient}", headers=reception.headers)
    ).status_code == 200
    after, _ = await pending_member(admin_engine, clinic)
    await service.invite_again(admin.ctx, after)
    assert len(await staff_events(admin_engine, clinic)) == 2

    # Receipts: the access still gets a receipt that verifies against the signed tree head, with
    # the staff events in the same chain.
    log = await client.get(f"/api/v1/patients/{patient}/access-log", headers=patient_actor.headers)
    event_id = log.json()["items"][-1]["id"]
    await build_checkpoint(clinic)
    receipt = await client.get(
        f"/api/v1/access-events/{event_id}/receipt", headers=patient_actor.headers
    )
    assert receipt.status_code == 200, receipt.text
    body = receipt.json()
    head = TreeHead.from_json(body["sth"])
    assert verify_inclusion(
        leaf_hash(bytes.fromhex(body["payload_hash"])),
        body["leaf_index"],
        head.tree_size,
        [bytes.fromhex(p) for p in body["proof"]],
        bytes.fromhex(head.root_hex),
    )

    async with tenant_session(admin.ctx) as db:
        # The daily job reads only its own kind.
        assert await detect_days.entries(db, clinic) == {}
        # The detection loader reads access events only.
        now = datetime.now(UTC)
        events, _, _ = await load_clinic(
            db, clinic, now - timedelta(days=1), now + timedelta(minutes=1)
        )

    async with admin_engine.connect() as conn:
        accesses = await conn.scalar(
            text("SELECT count(*) FROM access_events WHERE clinic_id = :c"), {"c": clinic}
        )
        # The research view is built from access events only.
        research = await conn.scalar(
            text("SELECT count(*) FROM research.access_events_deid WHERE clinic_id = :c"),
            {"c": clinic},
        )
        audit = await conn.scalar(
            text("SELECT count(*) FROM audit_events WHERE clinic_id = :c"), {"c": clinic}
        )
    assert accesses is not None and accesses > 0
    assert events.height == accesses
    assert research == accesses
    # The chain holds the accesses and the two staff events.
    assert audit == accesses + 2
