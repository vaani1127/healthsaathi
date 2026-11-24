"""Staff password recovery (ADR 0010).

A staff member who forgot their password asks for a link by email. The link holds a random token
in its fragment (`/reset-password#email=...&token=...`), so the token never reaches a server or
its access logs. Only a keyed hash of the token is stored (derived key `RESET_LINK`, ADR 0009),
in `email_otps` with purpose `password_reset`, for 30 minutes; a new link makes every older one
stop working.

The request answers 202 at once and does everything else in a background task, so the answer
and its timing are the same for an unknown email, a patient and a real staff member.

Setting the new password also needs a current authenticator code when the person has one, so a
reset link alone (someone who can read the mailbox) is not enough. A reset never signs anyone
in: it signs out every session, and the next sign-in asks for the authenticator as always.
"""

import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlencode

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import keys
from app.core.config import get_settings
from app.core.db import ANONYMOUS, TenantContext, set_scope, tenant_session
from app.core.errors import ProblemError
from app.core.ratelimit import allow, hit
from app.db.enums import STAFF_ROLES
from app.db.models import EmailOtp, Membership, MfaSecret, User, UserSession
from app.identity import lockout, password_policy, passwords, totp
from app.identity.service import TOTP_ATTEMPTS, _claim_step
from app.ledger.writer import append_audit_event
from app.notify.email import Email, send_email

logger = logging.getLogger(__name__)

PURPOSE = "password_reset"
RESET_TTL = timedelta(minutes=30)
RESET_PER_EMAIL = "3/hour"
AUDIT_KIND = "staff"


def _now() -> datetime:
    return datetime.now(UTC)


def normalise_email(email: str) -> str:
    return email.strip().lower()


def reset_hash(email: str, token: str) -> str:
    """The stored form of a reset token, under its own derived key (ADR 0009)."""
    message = f"{normalise_email(email)}|{PURPOSE}|{token}".encode()
    return keys.hmac_hex(keys.RESET_LINK, message)


def reset_link(email: str, token: str) -> str:
    """The token goes in the fragment: browsers do not send it to any server."""
    fragment = urlencode({"email": email, "token": token})
    return f"{get_settings().public_app_url}/reset-password#{fragment}"


async def staff_clinics(db: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    """Clinics where the user has an active staff membership. The session must be scoped to the
    user (`app.user_id`), which lets row level security show their own memberships."""
    rows = await db.scalars(
        select(Membership.clinic_id)
        .where(
            Membership.user_id == user_id,
            Membership.is_active.is_(True),
            Membership.role.in_(sorted(STAFF_ROLES)),
        )
        .distinct()
        .order_by(Membership.clinic_id)
    )
    return list(rows.all())


async def audit(
    db: AsyncSession,
    user_id: uuid.UUID,
    clinic_ids: list[uuid.UUID],
    action: Literal["requested", "completed"],
) -> None:
    """One audit event in each of the user's clinics, in the caller's transaction. No email,
    token, password or address in it."""
    at = await db.scalar(text("SELECT clock_timestamp()"))
    assert isinstance(at, datetime)
    payload = {
        "type": "password_reset",
        "action": action,
        "at": at.isoformat(),
        "user_id": str(user_id),
    }
    for clinic_id in clinic_ids:
        await set_scope(db, clinic_id=clinic_id, user_id=user_id)
        await append_audit_event(db, clinic_id, AUDIT_KIND, dict(payload))
    await set_scope(db, clinic_id=None, user_id=user_id)


async def process_reset_request(email: str) -> None:
    """The background part of a reset request. It never raises: the caller has already answered,
    and a failure must look the same as an unknown email."""
    try:
        await _process_reset_request(email)
    except Exception:
        logger.exception("password reset request not processed")


async def _process_reset_request(email: str) -> None:
    email = normalise_email(email)
    # Over the limit nothing is sent, and the caller cannot tell (the answer was already 202).
    if not await allow(RESET_PER_EMAIL, "password-reset-email", email):
        return
    async with tenant_session(ANONYMOUS) as db:
        found = await db.scalar(select(User.id).where(func.lower(User.email) == email))
    if found is None:
        return

    token = secrets.token_urlsafe(32)
    async with tenant_session(TenantContext(user_id=found)) as db:
        # The user row lock puts two requests for one person in a line, so exactly one link
        # stays valid.
        user = await db.scalar(select(User).where(User.id == found).with_for_update())
        # Patients and invited staff without a password get nothing: patients sign in with an
        # email code, and an invite is how a first password is set.
        if user is None or not user.is_active or not user.password_hash:
            return
        clinics = await staff_clinics(db, user.id)
        if not clinics:
            return
        await db.execute(
            update(EmailOtp)
            .where(
                EmailOtp.email == email,
                EmailOtp.purpose == PURPOSE,
                EmailOtp.used_at.is_(None),
            )
            .values(used_at=_now())
        )
        db.add(
            EmailOtp(
                email=email,
                code_hash=reset_hash(email, token),
                purpose=PURPOSE,
                expires_at=_now() + RESET_TTL,
            )
        )
        await db.flush()
        await audit(db, user.id, clinics, "requested")
        to = user.email

    await send_email(
        Email(
            to=to,
            subject="Reset your HealthSaathi password",
            text=(
                "Someone asked to reset the password of your HealthSaathi staff account. "
                f"Set a new password here within 30 minutes: {reset_link(email, token)}\n\n"
                "The link works once. If you did not ask for this, ignore this email: nothing "
                "changes and your password stays the same. You still need your authenticator "
                "code to sign in."
            ),
        )
    )


def _bad_link() -> ProblemError:
    """One answer for a link that is wrong, used, expired or cancelled, and for a wrong or reused
    authenticator code, so the answer does not say which part failed."""
    return ProblemError(
        400, "invalid-reset-link", "This reset link or code is not valid. Ask for a new link."
    )


async def confirm_reset(email: str, token: str, password: str, code: str | None) -> None:
    """Set a new password with a reset link (and the authenticator code, if the person has one).

    Everything runs in one transaction with the link row locked, so two requests with the same
    link run one after the other and only the first can use it. When anything is refused the
    transaction rolls back: the link stays unused and the authenticator step stays unclaimed.
    """
    email = normalise_email(email)
    password_policy.check_password(password, email)
    async with tenant_session(ANONYMOUS) as db:
        link = await db.scalar(
            select(EmailOtp)
            .where(
                EmailOtp.email == email,
                EmailOtp.purpose == PURPOSE,
                EmailOtp.used_at.is_(None),
                EmailOtp.code_hash == reset_hash(email, token),
            )
            .with_for_update()
        )
        if link is None or link.expires_at <= _now():
            raise _bad_link()
        user = await db.scalar(
            select(User).where(func.lower(User.email) == email).with_for_update()
        )
        if user is None or not user.is_active or not user.password_hash:
            raise _bad_link()
        await set_scope(db, clinic_id=None, user_id=user.id)
        clinics = await staff_clinics(db, user.id)
        if not clinics:
            raise _bad_link()

        mfa = await db.get(MfaSecret, user.id)
        if mfa is not None and mfa.enabled_at is not None:
            if not code:
                raise ProblemError(
                    400,
                    "authenticator-code-required",
                    "Type the current code from your authenticator app.",
                )
            # The same attempt limit and single-use step as signing in.
            await hit(TOTP_ATTEMPTS, "totp", str(user.id))
            secret = totp.decrypt_secret(mfa.totp_secret_enc, str(user.id))
            if not await _claim_step(db, user.id, totp.matching_step(secret, code)):
                raise _bad_link()

        if passwords.verify_password(user.password_hash, password):
            raise ProblemError(422, "same-password", "Choose a password you have not used here.")
        user.password_hash = passwords.hash_password(password)
        now = _now()
        # This link and any other open one for the email.
        await db.execute(
            update(EmailOtp)
            .where(
                EmailOtp.email == email,
                EmailOtp.purpose == PURPOSE,
                EmailOtp.used_at.is_(None),
            )
            .values(used_at=now)
            .execution_options(synchronize_session=False)
        )
        # Every session, this device's too: the person signs in again with the new password.
        await db.execute(
            update(UserSession)
            .where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        await audit(db, user.id, clinics, "completed")
        to = user.email

    # The person proved they own the account: forget earlier wrong passwords.
    await lockout.clear(email)
    try:
        await send_email(
            Email(
                to=to,
                subject="Your HealthSaathi password was changed",
                text=(
                    f"The password of your HealthSaathi staff account was changed with a reset "
                    f"link at {now:%d %b %Y %H:%M} UTC, and every device was signed out. If this "
                    "was not you, contact your clinic admin straight away."
                ),
            )
        )
    except Exception:
        logger.exception("password changed email not sent")
