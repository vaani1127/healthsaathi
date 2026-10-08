import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.errors import ProblemError
from app.db import models as m
from app.db.enums import Role
from app.identity import service, tokens, totp
from app.identity.deps import PatientDataPrincipal
from app.identity.passwords import hash_password
from app.main import create_app
from app.notify.email import outbox
from tests.factories import ClinicGraph, build_clinic_graph

PASSWORD = "correct-horse-battery-staple"


@dataclass
class StaffUser:
    user_id: uuid.UUID
    email: str
    totp_secret: str
    clinic_id: uuid.UUID

    def code(self, steps_ahead: int = 0) -> str:
        # Each code works once, so a second sign-in in the same 30 s uses the next step's code.
        return pyotp.TOTP(self.totp_secret).at(int(time.time()) + 30 * steps_ahead)


@pytest.fixture(scope="module")
async def clinic(admin_engine: AsyncEngine) -> ClinicGraph:
    return await build_clinic_graph(admin_engine, "auth")


@pytest.fixture(scope="module")
async def other_clinic(admin_engine: AsyncEngine) -> ClinicGraph:
    return await build_clinic_graph(admin_engine, "auth-other")


async def make_user(
    engine: AsyncEngine,
    clinic_id: uuid.UUID,
    role: Role,
    *,
    with_totp: bool = True,
    password: str | None = PASSWORD,
) -> StaffUser:
    secret = totp.new_secret()
    email = f"{role.value}.{uuid.uuid4().hex[:12]}@auth.test"
    async with AsyncSession(engine) as s, s.begin():
        user = m.User(
            email=email,
            name="Auth Test",
            password_hash=hash_password(password) if password else None,
        )
        s.add(user)
        await s.flush()
        s.add(m.Membership(clinic_id=clinic_id, user_id=user.id, role=role))
        if with_totp:
            s.add(
                m.MfaSecret(
                    user_id=user.id,
                    totp_secret_enc=totp.encrypt_secret(secret, str(user.id)),
                    enabled_at=datetime.now(UTC),
                )
            )
        user_id = user.id
    return StaffUser(user_id, email, secret, clinic_id)


async def sign_in(client: AsyncClient, user: StaffUser, steps_ahead: int = 0) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login", json={"email": user.email, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    challenge = resp.json()["challenge_token"]
    resp = await client.post(
        "/api/v1/auth/totp/verify",
        json={"challenge_token": challenge, "code": user.code(steps_ahead)},
    )
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def refresh_with(client: AsyncClient, token: str, body: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    client.cookies.clear()
    return await client.post(
        "/api/v1/auth/refresh", json=body, headers={"cookie": f"hs_refresh={token}"}
    )


# Password and lockout -----------------------------------------------------------------------------


async def test_wrong_password_is_rejected(
    client: AsyncClient, clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    user = await make_user(admin_engine, clinic.clinic_id, Role.DOCTOR)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": "nope-nope-1"}
    )
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["code"] == "invalid-credentials"


async def test_unknown_email_looks_like_wrong_password(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@auth.test", "password": "whatever-123"}
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid-credentials"


async def test_account_locks_after_five_wrong_passwords(
    clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    user = await make_user(admin_engine, clinic.clinic_id, Role.NURSE)
    for _ in range(5):
        with pytest.raises(ProblemError) as exc:
            await service.login_with_password(user.email, "wrong-password-1")
        assert exc.value.code == "invalid-credentials"
    with pytest.raises(ProblemError) as exc:
        await service.login_with_password(user.email, PASSWORD)
    assert exc.value.status == 429
    assert exc.value.code == "account-locked"


async def test_login_is_rate_limited_per_ip(client: AsyncClient) -> None:
    statuses = [
        (
            await client.post(
                "/api/v1/auth/login", json={"email": f"x{i}@auth.test", "password": "x" * 12}
            )
        ).status_code
        for i in range(6)
    ]
    assert statuses[:5] == [401] * 5
    assert statuses[5] == 429


async def test_inactive_user_cannot_log_in(
    client: AsyncClient, clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    user = await make_user(admin_engine, clinic.clinic_id, Role.DOCTOR)
    async with AsyncSession(admin_engine) as s, s.begin():
        await s.execute(update(m.User).where(m.User.id == user.user_id).values(is_active=False))
    resp = await client.post("/api/v1/auth/login", json={"email": user.email, "password": PASSWORD})
    assert resp.status_code == 401


# TOTP ---------------------------------------------------------------------------------------------


async def test_password_alone_gives_no_session(
    client: AsyncClient, clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    user = await make_user(admin_engine, clinic.clinic_id, Role.DOCTOR)
    resp = await client.post("/api/v1/auth/login", json={"email": user.email, "password": PASSWORD})
    body = resp.json()
    assert body["status"] == "mfa_required"
    assert "access_token" not in body
    assert "hs_refresh" not in resp.cookies

    # The challenge token is not an access token.
    me = await client.get("/api/v1/auth/me", headers=bearer(body["challenge_token"]))
    assert me.status_code == 401


async def test_wrong_totp_code_is_rejected(
    client: AsyncClient, clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    user = await make_user(admin_engine, clinic.clinic_id, Role.DOCTOR)
    resp = await client.post("/api/v1/auth/login", json={"email": user.email, "password": PASSWORD})
    wrong = f"{(int(user.code()) + 1) % 10**6:06d}"
    resp = await client.post(
        "/api/v1/auth/totp/verify",
        json={"challenge_token": resp.json()["challenge_token"], "code": wrong},
    )
    assert resp.status_code == 401


async def test_staff_without_totp_must_enrol_first(
    client: AsyncClient, clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    user = await make_user(admin_engine, clinic.clinic_id, Role.RECEPTION, with_totp=False)
    resp = await client.post("/api/v1/auth/login", json={"email": user.email, "password": PASSWORD})
    assert resp.json()["status"] == "mfa_enrollment_required"
    enroll_token = resp.json()["challenge_token"]

    # An enrolment token cannot be used as a TOTP challenge.
    bad = await client.post(
        "/api/v1/auth/totp/verify", json={"challenge_token": enroll_token, "code": "123456"}
    )
    assert bad.status_code == 401

    resp = await client.post("/api/v1/auth/totp/enroll", json={"enroll_token": enroll_token})
    assert resp.status_code == 200
    secret = resp.json()["secret"]
    assert resp.json()["otpauth_uri"].startswith("otpauth://totp/")

    resp = await client.post(
        "/api/v1/auth/totp/activate",
        json={"enroll_token": enroll_token, "code": pyotp.TOTP(secret).now()},
    )
    assert resp.status_code == 200
    assert "hs_refresh" in resp.cookies

    again = await client.post("/api/v1/auth/totp/enroll", json={"enroll_token": enroll_token})
    assert again.status_code == 409


async def test_totp_secret_is_encrypted_at_rest(
    clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    user = await make_user(admin_engine, clinic.clinic_id, Role.DOCTOR)
    async with AsyncSession(admin_engine) as s:
        blob = await s.scalar(
            select(m.MfaSecret.totp_secret_enc).where(m.MfaSecret.user_id == user.user_id)
        )
    assert blob is not None
    assert user.totp_secret.encode() not in blob
    assert blob.startswith(b"t1:")


patient_data_router = APIRouter()


@patient_data_router.get("/test-patient-data")
async def _patient_data(principal: PatientDataPrincipal) -> dict[str, str]:
    return {"role": principal.role.value}


@pytest.fixture
async def gated_client() -> AsyncClient:
    app: FastAPI = create_app()
    app.include_router(patient_data_router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_patient_data_requires_totp_for_staff(
    gated_client: AsyncClient, clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    user = await make_user(admin_engine, clinic.clinic_id, Role.DOCTOR)
    issued = await service.create_session(user.user_id, "mfa", service.DeviceInfo(None, None))

    # A token for this session that claims only a password, as if TOTP had been skipped.
    no_otp, _ = tokens.access_token(
        user.user_id, issued.session_id, ["pwd"], clinic.clinic_id, Role.DOCTOR.value
    )
    resp = await gated_client.get("/test-patient-data", headers=bearer(no_otp))
    assert resp.status_code == 403
    assert resp.json()["code"] == "mfa-required"

    with_otp, _ = tokens.access_token(
        user.user_id, issued.session_id, ["pwd", "otp"], clinic.clinic_id, Role.DOCTOR.value
    )
    resp = await gated_client.get("/test-patient-data", headers=bearer(with_otp))
    assert resp.status_code == 200

    no_clinic, _ = tokens.access_token(user.user_id, issued.session_id, ["pwd", "otp"])
    resp = await gated_client.get("/test-patient-data", headers=bearer(no_clinic))
    assert resp.json()["code"] == "clinic-not-selected"


# Clinic selection ---------------------------------------------------------------------------------


async def test_select_clinic_puts_membership_in_token(
    client: AsyncClient, clinic: ClinicGraph, other_clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    user = await make_user(admin_engine, clinic.clinic_id, Role.DOCTOR)
    tokens_ = await sign_in(client, user)
    access = tokens_["access_token"]

    resp = await client.post(
        "/api/v1/auth/select-clinic",
        json={"clinic_id": str(clinic.clinic_id), "role": "doctor"},
        headers=bearer(access),
    )
    assert resp.status_code == 200
    claims = tokens.decode_token(resp.json()["access_token"], "access")
    assert claims["clinic_id"] == str(clinic.clinic_id)
    assert claims["role"] == "doctor"
    assert claims["sid"] == tokens_["session_id"]

    wrong_role = await client.post(
        "/api/v1/auth/select-clinic",
        json={"clinic_id": str(clinic.clinic_id), "role": "clinic_admin"},
        headers=bearer(access),
    )
    assert wrong_role.status_code == 403

    wrong_clinic = await client.post(
        "/api/v1/auth/select-clinic",
        json={"clinic_id": str(other_clinic.clinic_id), "role": "doctor"},
        headers=bearer(access),
    )
    assert wrong_clinic.status_code == 403
    assert wrong_clinic.json()["code"] == "not-a-member"


async def test_refresh_keeps_clinic_only_if_still_a_member(
    client: AsyncClient, clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    user = await make_user(admin_engine, clinic.clinic_id, Role.NURSE)
    await sign_in(client, user)
    refresh_token = client.cookies["hs_refresh"]

    resp = await refresh_with(
        client, refresh_token, {"clinic_id": str(clinic.clinic_id), "role": "nurse"}
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "nurse"

    async with AsyncSession(admin_engine) as s, s.begin():
        await s.execute(
            update(m.Membership).where(m.Membership.user_id == user.user_id).values(is_active=False)
        )
    resp = await refresh_with(
        client, resp.cookies["hs_refresh"], {"clinic_id": str(clinic.clinic_id), "role": "nurse"}
    )
    assert resp.status_code == 403


# Refresh tokens -----------------------------------------------------------------------------------


async def test_refresh_rotates_the_token(
    client: AsyncClient, clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    user = await make_user(admin_engine, clinic.clinic_id, Role.DOCTOR)
    first = await sign_in(client, user)
    old = client.cookies["hs_refresh"]

    resp = await refresh_with(client, old)
    assert resp.status_code == 200
    new = resp.cookies["hs_refresh"]
    assert new != old
    assert resp.json()["session_id"] == first["session_id"]

    cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/api/v1/auth" in cookie


async def test_reusing_a_rotated_refresh_token_ends_the_session(
    client: AsyncClient, clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    user = await make_user(admin_engine, clinic.clinic_id, Role.DOCTOR)
    await sign_in(client, user)
    old = client.cookies["hs_refresh"]
    rotated = await refresh_with(client, old)
    new = rotated.cookies["hs_refresh"]
    access = rotated.json()["access_token"]

    reuse = await refresh_with(client, old)
    assert reuse.status_code == 401
    assert reuse.json()["code"] == "refresh-token-reused"

    # The whole family is gone: the newest refresh token and the access token stop working.
    assert (await refresh_with(client, new)).status_code == 401
    me = await client.get("/api/v1/auth/me", headers=bearer(access))
    assert me.status_code == 401
    assert me.json()["code"] == "session-ended"


async def test_tampered_session_kind_is_rejected(
    client: AsyncClient, clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    user = await make_user(admin_engine, clinic.clinic_id, Role.DOCTOR)
    await sign_in(client, user)
    token = client.cookies["hs_refresh"]
    assert token.startswith("mfa.")
    forged = "email." + token.split(".", 1)[1]
    assert (await refresh_with(client, forged)).status_code == 401


async def test_expired_refresh_token(
    client: AsyncClient, clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    user = await make_user(admin_engine, clinic.clinic_id, Role.DOCTOR)
    await sign_in(client, user)
    token = client.cookies["hs_refresh"]
    async with AsyncSession(admin_engine) as s, s.begin():
        await s.execute(
            update(m.UserSession)
            .where(m.UserSession.refresh_hash == tokens.hash_refresh_token(token))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    resp = await refresh_with(client, token)
    assert resp.status_code == 401
    assert resp.json()["code"] == "session-expired"


# Sessions -----------------------------------------------------------------------------------------


async def test_sessions_list_and_revoke(
    client: AsyncClient, clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    user = await make_user(admin_engine, clinic.clinic_id, Role.DOCTOR)
    first = await sign_in(client, user)
    second = await sign_in(client, user, steps_ahead=1)

    resp = await client.get("/api/v1/auth/sessions", headers=bearer(second["access_token"]))
    sessions = resp.json()
    assert {s["id"] for s in sessions} == {first["session_id"], second["session_id"]}
    assert [s["current"] for s in sessions if s["id"] == second["session_id"]] == [True]

    resp = await client.delete(
        f"/api/v1/auth/sessions/{first['session_id']}", headers=bearer(second["access_token"])
    )
    assert resp.status_code == 204
    assert (
        await client.get("/api/v1/auth/me", headers=bearer(first["access_token"]))
    ).status_code == 401
    assert (
        await client.get("/api/v1/auth/me", headers=bearer(second["access_token"]))
    ).status_code == 200

    # Another user's session id is not found.
    other = await make_user(admin_engine, clinic.clinic_id, Role.NURSE)
    other_tokens = await sign_in(client, other)
    resp = await client.delete(
        f"/api/v1/auth/sessions/{second['session_id']}",
        headers=bearer(other_tokens["access_token"]),
    )
    assert resp.status_code == 404


async def test_logout_ends_session_and_clears_cookie(
    client: AsyncClient, clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    user = await make_user(admin_engine, clinic.clinic_id, Role.DOCTOR)
    issued = await sign_in(client, user)
    resp = await client.post("/api/v1/auth/logout", headers=bearer(issued["access_token"]))
    assert resp.status_code == 204
    assert (
        'hs_refresh=""' in resp.headers["set-cookie"]
        or "max-age=0" in resp.headers["set-cookie"].lower()
    )
    assert (
        await client.get("/api/v1/auth/me", headers=bearer(issued["access_token"]))
    ).status_code == 401


# Patient email codes ------------------------------------------------------------------------------


async def make_patient(engine: AsyncEngine, clinic: ClinicGraph) -> str:
    user = await make_user(engine, clinic.clinic_id, Role.PATIENT, with_totp=False, password=None)
    return user.email


def last_code(email: str) -> str:
    sent = outbox.last_to(email)
    assert sent is not None
    return next(w for w in sent.text.replace(".", " ").split() if w.isdigit() and len(w) == 6)


async def test_patient_signs_in_with_email_code(
    client: AsyncClient, clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    email = await make_patient(admin_engine, clinic)
    resp = await client.post("/api/v1/auth/otp/request", json={"email": email})
    assert resp.status_code == 202
    resp = await client.post(
        "/api/v1/auth/otp/verify", json={"email": email, "code": last_code(email)}
    )
    assert resp.status_code == 200
    claims = tokens.decode_token(resp.json()["access_token"], "access")
    assert claims["amr"] == ["email_otp"]

    selected = await client.post(
        "/api/v1/auth/select-clinic",
        json={"clinic_id": str(clinic.clinic_id), "role": "patient"},
        headers=bearer(resp.json()["access_token"]),
    )
    assert selected.status_code == 200

    # An email code session can never act as staff.
    staff = await client.post(
        "/api/v1/auth/select-clinic",
        json={"clinic_id": str(clinic.clinic_id), "role": "doctor"},
        headers=bearer(resp.json()["access_token"]),
    )
    assert staff.status_code == 403
    assert staff.json()["code"] == "mfa-required"


async def test_email_code_can_be_used_once(
    client: AsyncClient, clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    email = await make_patient(admin_engine, clinic)
    await client.post("/api/v1/auth/otp/request", json={"email": email})
    code = last_code(email)
    assert (
        await client.post("/api/v1/auth/otp/verify", json={"email": email, "code": code})
    ).status_code == 200
    again = await client.post("/api/v1/auth/otp/verify", json={"email": email, "code": code})
    assert again.status_code == 400


async def test_email_code_expires(
    client: AsyncClient, clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    email = await make_patient(admin_engine, clinic)
    await client.post("/api/v1/auth/otp/request", json={"email": email})
    async with AsyncSession(admin_engine) as s, s.begin():
        await s.execute(
            update(m.EmailOtp)
            .where(m.EmailOtp.email == email)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    resp = await client.post(
        "/api/v1/auth/otp/verify", json={"email": email, "code": last_code(email)}
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "code-expired"


async def test_email_code_locks_after_five_wrong_attempts(
    client: AsyncClient, clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    email = await make_patient(admin_engine, clinic)
    await client.post("/api/v1/auth/otp/request", json={"email": email})
    code = last_code(email)
    wrong = f"{(int(code) + 1) % 10**6:06d}"
    device = service.DeviceInfo(None, None)
    for _ in range(5):
        with pytest.raises(ProblemError) as exc:
            await service.verify_email_code(email, wrong, device)
        assert exc.value.code == "invalid-code"
    with pytest.raises(ProblemError) as exc:
        await service.verify_email_code(email, code, device)
    assert exc.value.code == "too-many-attempts"


async def test_email_code_requests_are_limited_per_email(
    client: AsyncClient, clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    email = await make_patient(admin_engine, clinic)
    statuses = [
        (await client.post("/api/v1/auth/otp/request", json={"email": email})).status_code
        for _ in range(4)
    ]
    assert statuses == [202, 202, 202, 429]


async def test_email_code_request_does_not_reveal_unknown_emails(client: AsyncClient) -> None:
    before = len(outbox.sent)
    resp = await client.post("/api/v1/auth/otp/request", json={"email": "ghost@auth.test"})
    assert resp.status_code == 202
    assert len(outbox.sent) == before


async def test_codes_are_stored_hashed(clinic: ClinicGraph, admin_engine: AsyncEngine) -> None:
    email = await make_patient(admin_engine, clinic)
    await service.request_email_code(email)
    async with AsyncSession(admin_engine) as s:
        stored = await s.scalar(select(m.EmailOtp.code_hash).where(m.EmailOtp.email == email))
    assert stored is not None
    assert last_code(email) not in stored


async def test_patient_cannot_use_password_login(
    client: AsyncClient, clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    user = await make_user(admin_engine, clinic.clinic_id, Role.PATIENT, with_totp=False)
    resp = await client.post("/api/v1/auth/login", json={"email": user.email, "password": PASSWORD})
    assert resp.status_code == 400
    assert resp.json()["code"] == "use-email-code"


# Invites ------------------------------------------------------------------------------------------


async def test_clinic_admin_invites_staff_who_then_enrols(
    client: AsyncClient, clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    admin = await make_user(admin_engine, clinic.clinic_id, Role.CLINIC_ADMIN)
    issued = await sign_in(client, admin)
    scoped = await client.post(
        "/api/v1/auth/select-clinic",
        json={"clinic_id": str(clinic.clinic_id), "role": "clinic_admin"},
        headers=bearer(issued["access_token"]),
    )
    admin_token = scoped.json()["access_token"]

    email = f"new.nurse.{uuid.uuid4().hex[:12]}@auth.test"
    resp = await client.post(
        "/api/v1/staff/invites",
        json={"email": email, "name": "New Nurse", "role": "nurse"},
        headers=bearer(admin_token),
    )
    assert resp.status_code == 201

    dup = await client.post(
        "/api/v1/staff/invites",
        json={"email": email, "name": "New Nurse", "role": "nurse"},
        headers=bearer(admin_token),
    )
    assert dup.status_code == 409

    sent = outbox.last_to(email)
    assert sent is not None
    invite_token = sent.text.split("token=")[1].split()[0]

    weak = await client.post(
        "/api/v1/auth/invite/accept",
        json={"email": email, "token": invite_token, "password": "1234567890"},
    )
    assert weak.status_code == 422
    resp = await client.post(
        "/api/v1/auth/invite/accept",
        json={"email": email, "token": invite_token, "password": "a-long-new-pass-phrase"},
    )
    assert resp.status_code == 204
    reused = await client.post(
        "/api/v1/auth/invite/accept",
        json={"email": email, "token": invite_token, "password": "a-long-new-pass-phrase"},
    )
    assert reused.status_code == 400

    login = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "a-long-new-pass-phrase"}
    )
    assert login.json()["status"] == "mfa_enrollment_required"


async def test_only_clinic_admin_can_invite(
    client: AsyncClient, clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    doctor = await make_user(admin_engine, clinic.clinic_id, Role.DOCTOR)
    issued = await sign_in(client, doctor)
    scoped = await client.post(
        "/api/v1/auth/select-clinic",
        json={"clinic_id": str(clinic.clinic_id), "role": "doctor"},
        headers=bearer(issued["access_token"]),
    )
    resp = await client.post(
        "/api/v1/staff/invites",
        json={"email": "x@auth.test", "name": "X Y", "role": "nurse"},
        headers=bearer(scoped.json()["access_token"]),
    )
    assert resp.status_code == 403


async def test_invite_creates_membership_only_in_admins_clinic(
    client: AsyncClient, clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    admin = await make_user(admin_engine, clinic.clinic_id, Role.CLINIC_ADMIN)
    from app.core.db import TenantContext

    ctx = TenantContext(clinic_id=clinic.clinic_id, user_id=admin.user_id, role="clinic_admin")
    user_id, _ = await service.invite_staff(
        ctx, f"lab.{uuid.uuid4().hex[:12]}@auth.test", "Lab", Role.LAB_TECH
    )
    async with AsyncSession(admin_engine) as s:
        clinics = (
            (
                await s.execute(
                    text("SELECT clinic_id FROM memberships WHERE user_id = :u"), {"u": user_id}
                )
            )
            .scalars()
            .all()
        )
    assert clinics == [clinic.clinic_id]


# Headers and OpenAPI ------------------------------------------------------------------------------


async def test_security_headers(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/health")
    assert resp.headers["strict-transport-security"].startswith("max-age=")
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "no-referrer"


async def test_openapi_lists_auth_routes(client: AsyncClient) -> None:
    paths = (await client.get("/api/v1/openapi.json")).json()["paths"]
    for path in (
        "/api/v1/auth/login",
        "/api/v1/auth/totp/verify",
        "/api/v1/auth/totp/enroll",
        "/api/v1/auth/totp/activate",
        "/api/v1/auth/otp/request",
        "/api/v1/auth/otp/verify",
        "/api/v1/auth/refresh",
        "/api/v1/auth/select-clinic",
        "/api/v1/auth/sessions",
        "/api/v1/auth/logout",
        "/api/v1/staff/invites",
    ):
        assert path in paths


async def test_no_secret_in_access_token(
    client: AsyncClient, clinic: ClinicGraph, admin_engine: AsyncEngine
) -> None:
    user = await make_user(admin_engine, clinic.clinic_id, Role.DOCTOR)
    issued = await sign_in(client, user)
    claims = tokens.decode_token(issued["access_token"], "access")
    assert set(claims) == {
        "iss",
        "typ",
        "sub",
        "jti",
        "iat",
        "exp",
        "sid",
        "amr",
        "clinic_id",
        "role",
    }
    assert claims["exp"] - claims["iat"] == 15 * 60
