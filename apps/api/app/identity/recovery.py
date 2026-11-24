"""Staff password recovery (ADR 0010).

A staff member who forgot their password asks for a link by email. The link holds a random token
in its fragment (`/reset-password#email=...&token=...`), so the token never reaches a server or
its access logs. Only a keyed hash of the token is stored (derived key `RESET_LINK`, ADR 0009),
in `email_otps` with purpose `password_reset`, for 30 minutes; a new link makes every older one
stop working.

The request answers 202 at once and does everything else in a background task, so the answer
and its timing are the same for an unknown email, a patient and a real staff member.
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
from app.core.ratelimit import allow
from app.db.enums import STAFF_ROLES
from app.db.models import EmailOtp, Membership, User
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
