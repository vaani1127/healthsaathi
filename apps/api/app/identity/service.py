"""Identity: staff login with TOTP, patient email OTP, sessions with rotating refresh tokens.

Staff never get a session from a password alone. A correct password returns a short-lived
challenge token; the session (and refresh token) is created only after the TOTP code is checked.
A staff user without TOTP gets an enrolment token instead and must set it up first.

Refresh tokens look like `<kind>.<random>`. The kind records how the session was proven
("mfa" for password + TOTP, "email" for an email code). The database stores a SHA-256 of the whole
string, so a client cannot change the kind without the token stopping working.
"""

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import ANONYMOUS, TenantContext, tenant_session
from app.core.errors import ProblemError
from app.core.ratelimit import hit
from app.db.enums import STAFF_ROLES, Role
from app.db.models import Device, EmailOtp, Membership, MfaSecret, User, UserSession
from app.identity import lockout, password_policy, passwords, tokens, totp
from app.notify.email import Email, send_email
from e2d_core.ids import uuid7

SessionKind = Literal["mfa", "email"]
AMR: dict[SessionKind, list[str]] = {"mfa": ["pwd", "otp"], "email": ["email_otp"]}

OTP_TTL = timedelta(minutes=10)
OTP_MAX_ATTEMPTS = 5
INVITE_TTL = timedelta(hours=72)
TOTP_ATTEMPTS = "5/15 minutes"

# Verifying against this keeps the response time the same when the email is unknown.
_DUMMY_HASH = passwords.hash_password(secrets.token_urlsafe(16))


@dataclass(frozen=True)
class DeviceInfo:
    fingerprint: str | None
    user_agent: str | None


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    access_expires_at: datetime
    refresh_token: str | None
    session_id: uuid.UUID
    clinic_id: uuid.UUID | None = None
    role: str | None = None


@dataclass(frozen=True)
class LoginChallenge:
    status: Literal["mfa_required", "mfa_enrollment_required"]
    challenge_token: str
    expires_at: datetime


@dataclass(frozen=True)
class MembershipInfo:
    clinic_id: uuid.UUID
    clinic_name: str
    role: Role


def _now() -> datetime:
    return datetime.now(UTC)


def _normalise_email(email: str) -> str:
    return email.strip().lower()


def _otp_hash(email: str, purpose: str, code: str) -> str:
    key = get_settings().otp_hmac_key.get_secret_value().encode()
    message = f"{_normalise_email(email)}|{purpose}|{code}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


async def _user_by_email(db: AsyncSession, email: str) -> User | None:
    return await db.scalar(select(User).where(func.lower(User.email) == _normalise_email(email)))


async def active_memberships(user_id: uuid.UUID) -> list[MembershipInfo]:
    from app.db.models import Clinic

    async with tenant_session(TenantContext(user_id=user_id)) as db:
        rows = await db.execute(
            select(Membership.clinic_id, Clinic.name, Membership.role)
            .join(Clinic, Clinic.id == Membership.clinic_id)
            .where(Membership.user_id == user_id, Membership.is_active.is_(True))
            .order_by(Clinic.name, Membership.role)
        )
        return [MembershipInfo(r.clinic_id, r.name, Role(r.role)) for r in rows]


# Login --------------------------------------------------------------------------------------------


async def login_with_password(email: str, password: str) -> LoginChallenge:
    if lockout.is_locked(email):
        raise ProblemError(
            429, "account-locked", "Too many wrong passwords. Try again in 15 minutes."
        )

    async with tenant_session(ANONYMOUS) as db:
        user = await _user_by_email(db, email)
    valid = False
    if user is not None and user.is_active and user.password_hash:
        valid = passwords.verify_password(user.password_hash, password)
    else:
        passwords.verify_password(_DUMMY_HASH, password)
    if not valid or user is None:
        lockout.record_failure(email)
        raise ProblemError(401, "invalid-credentials", "Email or password is wrong.")
    lockout.clear(email)

    memberships = await active_memberships(user.id)
    if memberships and all(m.role == Role.PATIENT for m in memberships):
        raise ProblemError(400, "use-email-code", "Patients sign in with a code sent by email.")

    async with tenant_session(ANONYMOUS) as db:
        mfa = await db.get(MfaSecret, user.id)
    if mfa is not None and mfa.enabled_at is not None:
        token, expires = tokens.encode_token(
            "mfa_challenge", user.id, timedelta(minutes=tokens.CHALLENGE_MINUTES)
        )
        return LoginChallenge("mfa_required", token, expires)
    token, expires = tokens.encode_token(
        "mfa_enroll", user.id, timedelta(minutes=tokens.ENROLL_MINUTES)
    )
    return LoginChallenge("mfa_enrollment_required", token, expires)


def _user_from(token: str, token_type: tokens.TokenType) -> uuid.UUID:
    try:
        return uuid.UUID(tokens.decode_token(token, token_type)["sub"])
    except (tokens.TokenError, ValueError) as exc:
        raise ProblemError(401, "invalid-token", "Sign in again.") from exc


async def verify_totp(challenge_token: str, code: str, device: DeviceInfo) -> IssuedTokens:
    user_id = _user_from(challenge_token, "mfa_challenge")
    hit(TOTP_ATTEMPTS, "totp", str(user_id))
    async with tenant_session(ANONYMOUS) as db:
        mfa = await db.get(MfaSecret, user_id)
    if mfa is None or mfa.enabled_at is None:
        raise ProblemError(400, "mfa-not-enrolled")
    if not totp.verify(totp.decrypt_secret(mfa.totp_secret_enc, str(user_id)), code, str(user_id)):
        raise ProblemError(401, "invalid-code", "The code is wrong or has expired.")
    return await create_session(user_id, "mfa", device)


async def start_totp_enrolment(enroll_token: str) -> tuple[str, str]:
    user_id = _user_from(enroll_token, "mfa_enroll")
    async with tenant_session(ANONYMOUS) as db:
        user = await db.get(User, user_id)
        if user is None:
            raise ProblemError(401, "invalid-token")
        mfa = await db.get(MfaSecret, user_id)
        if mfa is not None and mfa.enabled_at is not None:
            raise ProblemError(409, "mfa-already-enrolled")
        secret = totp.new_secret()
        encrypted = totp.encrypt_secret(secret, str(user_id))
        if mfa is None:
            db.add(MfaSecret(user_id=user_id, totp_secret_enc=encrypted))
        else:
            mfa.totp_secret_enc = encrypted
        return secret, totp.provisioning_uri(secret, user.email)


async def activate_totp(enroll_token: str, code: str, device: DeviceInfo) -> IssuedTokens:
    user_id = _user_from(enroll_token, "mfa_enroll")
    hit(TOTP_ATTEMPTS, "totp", str(user_id))
    async with tenant_session(ANONYMOUS) as db:
        mfa = await db.get(MfaSecret, user_id)
        if mfa is None:
            raise ProblemError(400, "mfa-enrolment-not-started")
        if mfa.enabled_at is not None:
            raise ProblemError(409, "mfa-already-enrolled")
        if not totp.verify(
            totp.decrypt_secret(mfa.totp_secret_enc, str(user_id)), code, str(user_id)
        ):
            raise ProblemError(401, "invalid-code", "The code is wrong or has expired.")
        mfa.enabled_at = _now()
    return await create_session(user_id, "mfa", device)


# Patient email OTP --------------------------------------------------------------------------------


async def request_email_code(email: str) -> None:
    """Send a login code to a patient. Gives the same answer whether or not the email is known."""
    email = _normalise_email(email)
    hit("3/minute", "otp-email", email)
    async with tenant_session(ANONYMOUS) as db:
        user = await _user_by_email(db, email)
    if user is None or not user.is_active:
        return
    memberships = await active_memberships(user.id)
    if not any(m.role == Role.PATIENT for m in memberships):
        return

    code = f"{secrets.randbelow(10**6):06d}"
    async with tenant_session(ANONYMOUS) as db:
        db.add(
            EmailOtp(
                email=email,
                code_hash=_otp_hash(email, "login", code),
                purpose="login",
                expires_at=_now() + OTP_TTL,
            )
        )
    await send_email(
        Email(
            to=email,
            subject="Your HealthSaathi sign-in code",
            text=f"Your sign-in code is {code}. It works for 10 minutes. "
            "If you did not ask for it, you can ignore this email.",
        )
    )


async def verify_email_code(email: str, code: str, device: DeviceInfo) -> IssuedTokens:
    email = _normalise_email(email)
    async with tenant_session(ANONYMOUS) as db:
        otp = await db.scalar(
            select(EmailOtp)
            .where(
                EmailOtp.email == email,
                EmailOtp.purpose == "login",
                EmailOtp.used_at.is_(None),
            )
            .order_by(EmailOtp.created_at.desc())
            .limit(1)
            .with_for_update()
        )
        if otp is None:
            raise ProblemError(400, "invalid-code", "Ask for a new code.")
        if otp.expires_at <= _now():
            raise ProblemError(400, "code-expired", "The code has expired. Ask for a new one.")
        if otp.attempts >= OTP_MAX_ATTEMPTS:
            raise ProblemError(400, "too-many-attempts", "Too many wrong codes. Ask for a new one.")
        if not hmac.compare_digest(otp.code_hash, _otp_hash(email, "login", code.strip())):
            otp.attempts += 1
            raise_after = ProblemError(400, "invalid-code", "The code is wrong.")
        else:
            otp.used_at = _now()
            raise_after = None
        user = await _user_by_email(db, email)
    if raise_after is not None:
        raise raise_after
    if user is None:
        raise ProblemError(400, "invalid-code")
    return await create_session(user.id, "email", device)


# Sessions -----------------------------------------------------------------------------------------


async def _device_id(db: AsyncSession, user_id: uuid.UUID, device: DeviceInfo) -> uuid.UUID | None:
    if not device.fingerprint:
        return None
    fingerprint_hash = hashlib.sha256(device.fingerprint.encode()).hexdigest()
    row = await db.scalar(
        select(Device).where(Device.user_id == user_id, Device.fingerprint_hash == fingerprint_hash)
    )
    if row is None:
        row = Device(
            user_id=user_id, fingerprint_hash=fingerprint_hash, user_agent=device.user_agent
        )
        db.add(row)
        await db.flush()
    else:
        row.last_seen = _now()
        row.user_agent = device.user_agent or row.user_agent
    return row.id


def _new_refresh(kind: SessionKind) -> tuple[str, str]:
    raw, _ = tokens.new_refresh_token()
    token = f"{kind}.{raw}"
    return token, tokens.hash_refresh_token(token)


def session_kind(refresh_token: str) -> SessionKind | None:
    kind = refresh_token.split(".", 1)[0]
    return kind if kind in AMR else None


async def create_session(user_id: uuid.UUID, kind: SessionKind, device: DeviceInfo) -> IssuedTokens:
    refresh, refresh_hash = _new_refresh(kind)
    family = uuid7()
    async with tenant_session(ANONYMOUS) as db:
        device_id = await _device_id(db, user_id, device)
        db.add(
            UserSession(
                user_id=user_id,
                device_id=device_id,
                refresh_hash=refresh_hash,
                family_id=family,
                expires_at=_now() + timedelta(days=get_settings().refresh_token_days),
            )
        )
    access, expires = tokens.access_token(user_id, family, AMR[kind])
    return IssuedTokens(access, expires, refresh, family)


async def _check_membership(
    user_id: uuid.UUID, clinic_id: uuid.UUID, role: Role, amr: list[str]
) -> None:
    if role in STAFF_ROLES and "otp" not in amr:
        raise ProblemError(403, "mfa-required", "Staff must sign in with an authenticator code.")
    if role == Role.PLATFORM_ADMIN:
        raise ProblemError(403, "not-a-member")
    async with tenant_session(TenantContext(user_id=user_id)) as db:
        found = await db.scalar(
            select(Membership.id).where(
                Membership.user_id == user_id,
                Membership.clinic_id == clinic_id,
                Membership.role == role,
                Membership.is_active.is_(True),
            )
        )
    if found is None:
        raise ProblemError(403, "not-a-member", "You do not have this role in this clinic.")


async def select_clinic(
    user_id: uuid.UUID, family: uuid.UUID, amr: list[str], clinic_id: uuid.UUID, role: Role
) -> IssuedTokens:
    await _check_membership(user_id, clinic_id, role, amr)
    access, expires = tokens.access_token(user_id, family, amr, clinic_id, role.value)
    return IssuedTokens(access, expires, None, family, clinic_id, role.value)


async def refresh(
    refresh_token: str, clinic_id: uuid.UUID | None = None, role: Role | None = None
) -> IssuedTokens:
    kind = session_kind(refresh_token)
    if kind is None:
        raise ProblemError(401, "invalid-refresh-token", "Sign in again.")
    now = _now()
    async with tenant_session(ANONYMOUS) as db:
        row = await db.scalar(
            select(UserSession)
            .where(UserSession.refresh_hash == tokens.hash_refresh_token(refresh_token))
            .with_for_update()
        )
        if row is None:
            raise ProblemError(401, "invalid-refresh-token", "Sign in again.")
        if row.revoked_at is not None:
            # A rotated token came back: someone else may hold it. End the whole session.
            await db.execute(
                update(UserSession)
                .where(UserSession.family_id == row.family_id, UserSession.revoked_at.is_(None))
                .values(revoked_at=now)
            )
            reused = True
        else:
            reused = False
            if row.expires_at <= now:
                raise ProblemError(401, "session-expired", "Sign in again.")
            row.revoked_at = now
            new_token, new_hash = _new_refresh(kind)
            db.add(
                UserSession(
                    user_id=row.user_id,
                    device_id=row.device_id,
                    refresh_hash=new_hash,
                    family_id=row.family_id,
                    expires_at=now + timedelta(days=get_settings().refresh_token_days),
                )
            )
        user_id, family = row.user_id, row.family_id
    if reused:
        raise ProblemError(401, "refresh-token-reused", "Sign in again.")

    amr = AMR[kind]
    if clinic_id is not None and role is not None:
        await _check_membership(user_id, clinic_id, role, amr)
        access, expires = tokens.access_token(user_id, family, amr, clinic_id, role.value)
    else:
        access, expires = tokens.access_token(user_id, family, amr)
        clinic_id, role = None, None
    return IssuedTokens(access, expires, new_token, family, clinic_id, role.value if role else None)


async def session_is_active(family: uuid.UUID) -> bool:
    async with tenant_session(ANONYMOUS) as db:
        found = await db.scalar(
            select(UserSession.id)
            .where(
                UserSession.family_id == family,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > _now(),
            )
            .limit(1)
        )
    return found is not None


@dataclass(frozen=True)
class SessionInfo:
    id: uuid.UUID
    started_at: datetime
    last_used_at: datetime
    expires_at: datetime
    user_agent: str | None


async def list_sessions(user_id: uuid.UUID) -> list[SessionInfo]:
    async with tenant_session(ANONYMOUS) as db:
        active = (
            await db.execute(
                select(UserSession, Device.user_agent)
                .outerjoin(Device, Device.id == UserSession.device_id)
                .where(
                    UserSession.user_id == user_id,
                    UserSession.revoked_at.is_(None),
                    UserSession.expires_at > _now(),
                )
            )
        ).all()
        started = dict(
            (
                await db.execute(
                    select(UserSession.family_id, func.min(UserSession.created_at))
                    .where(UserSession.user_id == user_id)
                    .group_by(UserSession.family_id)
                )
            ).all()
        )
    return sorted(
        (
            SessionInfo(
                id=s.family_id,
                started_at=started.get(s.family_id, s.created_at),
                last_used_at=s.created_at,
                expires_at=s.expires_at,
                user_agent=agent,
            )
            for s, agent in active
        ),
        key=lambda s: s.last_used_at,
        reverse=True,
    )


async def revoke_session(user_id: uuid.UUID, family: uuid.UUID) -> bool:
    async with tenant_session(ANONYMOUS) as db:
        result = await db.execute(
            update(UserSession)
            .where(
                UserSession.user_id == user_id,
                UserSession.family_id == family,
                UserSession.revoked_at.is_(None),
            )
            .values(revoked_at=_now())
        )
    return bool(result.rowcount)  # type: ignore[attr-defined]


# Staff invites ------------------------------------------------------------------------------------

INVITABLE_ROLES = frozenset(STAFF_ROLES - {Role.PLATFORM_ADMIN})


async def invite_staff(
    ctx: TenantContext, email: str, name: str, role: Role
) -> tuple[uuid.UUID, uuid.UUID]:
    if role not in INVITABLE_ROLES:
        raise ProblemError(422, "role-not-invitable")
    assert ctx.clinic_id is not None
    email = _normalise_email(email)
    token = secrets.token_urlsafe(24)
    async with tenant_session(ctx) as db:
        user = await _user_by_email(db, email)
        if user is None:
            user = User(email=email, name=name.strip())
            db.add(user)
            await db.flush()
        exists = await db.scalar(
            select(Membership.id).where(
                Membership.clinic_id == ctx.clinic_id,
                Membership.user_id == user.id,
                Membership.role == role,
            )
        )
        if exists is not None:
            raise ProblemError(409, "already-a-member")
        membership = Membership(clinic_id=ctx.clinic_id, user_id=user.id, role=role)
        db.add(membership)
        db.add(
            EmailOtp(
                email=email,
                code_hash=_otp_hash(email, "invite", token),
                purpose="invite",
                expires_at=_now() + INVITE_TTL,
            )
        )
        await db.flush()
        user_id, membership_id = user.id, membership.id

    link = f"{get_settings().public_app_url}/invite?email={email}&token={token}"
    await send_email(
        Email(
            to=email,
            subject="You have been invited to HealthSaathi",
            text=f"You have been added as {role.value.replace('_', ' ')}. "
            f"Set your password here within 72 hours: {link}",
        )
    )
    return user_id, membership_id


async def accept_invite(email: str, token: str, password: str) -> None:
    email = _normalise_email(email)
    password_policy.check_password(password, email)
    async with tenant_session(ANONYMOUS) as db:
        invite = await db.scalar(
            select(EmailOtp)
            .where(
                EmailOtp.email == email,
                EmailOtp.purpose == "invite",
                EmailOtp.used_at.is_(None),
                EmailOtp.code_hash == _otp_hash(email, "invite", token),
            )
            .with_for_update()
        )
        if invite is None or invite.expires_at <= _now():
            raise ProblemError(400, "invalid-invite", "This invite link is not valid any more.")
        invite.used_at = _now()
        user = await _user_by_email(db, email)
        if user is None:
            raise ProblemError(400, "invalid-invite")
        if user.password_hash is None:
            user.password_hash = passwords.hash_password(password)
