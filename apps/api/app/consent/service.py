"""Consent notices (versioned, English and Hindi), consent at registration, withdrawal and history.

Each consent stores `salt_hash`, an HMAC commitment to (consent, patient, notice). It can later be
committed to the optional ConsentRegistry contract without revealing who the patient is.
"""

import hashlib
import hmac
import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.service import RequestInfo, db_now, record_and_explain
from app.consent import schemas
from app.core.config import get_settings
from app.core.errors import ProblemError
from app.db.enums import AccessAction, ConsentChannel, ConsentEventKind, ResourceType, Role
from app.db.models import Consent, ConsentEvent, ConsentNotice
from app.identity.deps import ClinicPrincipal
from e2d_core.ids import uuid7

CONSENT = ResourceType.CONSENT


def commitment(consent_id: uuid.UUID, patient_id: uuid.UUID, notice_id: uuid.UUID) -> str:
    key = get_settings().otp_hmac_key.get_secret_value().encode()
    message = f"consent|{consent_id}|{patient_id}|{notice_id}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


# Notices ------------------------------------------------------------------------------------------


async def list_notices(
    db: AsyncSession, clinic_id: uuid.UUID, include_drafts: bool
) -> Sequence[ConsentNotice]:
    stmt = select(ConsentNotice).where(ConsentNotice.clinic_id == clinic_id)
    if not include_drafts:
        stmt = stmt.where(ConsentNotice.published_at.is_not(None))
    return (await db.scalars(stmt.order_by(ConsentNotice.version.desc()))).all()


async def create_notice(
    db: AsyncSession, principal: ClinicPrincipal, body: schemas.NoticeIn
) -> ConsentNotice:
    latest = await db.scalar(
        select(func.max(ConsentNotice.version)).where(
            ConsentNotice.clinic_id == principal.clinic_id
        )
    )
    notice = ConsentNotice(
        clinic_id=principal.clinic_id,
        version=(latest or 0) + 1,
        text_en=body.text_en,
        text_hi=body.text_hi,
        purposes=body.purposes,
    )
    db.add(notice)
    await db.flush()
    await db.refresh(notice)
    return notice


async def publish_notice(db: AsyncSession, notice_id: uuid.UUID) -> ConsentNotice:
    notice = await db.get(ConsentNotice, notice_id)
    if notice is None:
        raise ProblemError(404, "notice-not-found")
    if notice.published_at is not None:
        raise ProblemError(409, "already-published", "A published notice cannot change.")
    notice.published_at = await db_now(db)
    await db.flush()
    return notice


async def current_notice(db: AsyncSession, clinic_id: uuid.UUID) -> ConsentNotice | None:
    return await db.scalar(
        select(ConsentNotice)
        .where(ConsentNotice.clinic_id == clinic_id, ConsentNotice.published_at.is_not(None))
        .order_by(ConsentNotice.version.desc())
        .limit(1)
    )


# Consents -----------------------------------------------------------------------------------------


async def consent_out(db: AsyncSession, consent: Consent) -> schemas.ConsentOut:
    notice = await db.get(ConsentNotice, consent.notice_id)
    assert notice is not None
    events = (
        await db.scalars(
            select(ConsentEvent)
            .where(ConsentEvent.consent_id == consent.id)
            .order_by(ConsentEvent.at)
        )
    ).all()
    return schemas.ConsentOut(
        id=consent.id,
        patient_id=consent.patient_id,
        notice_id=consent.notice_id,
        notice_version=notice.version,
        granted_at=consent.granted_at,
        channel=consent.channel,
        withdrawn_at=consent.withdrawn_at,
        events=[schemas.ConsentEventOut.model_validate(e) for e in events],
    )


async def grant(
    db: AsyncSession, principal: ClinicPrincipal, patient_id: uuid.UUID, req: RequestInfo
) -> Consent:
    await record_and_explain(
        db, principal, patient_id, CONSENT, AccessAction.CREATE, req, detail={"op": "consent.grant"}
    )
    notice = await current_notice(db, principal.clinic_id)
    if notice is None:
        raise ProblemError(409, "no-published-notice", "Publish a consent notice first.")
    active = await db.scalar(
        select(Consent.id).where(
            Consent.patient_id == patient_id,
            Consent.notice_id == notice.id,
            Consent.withdrawn_at.is_(None),
        )
    )
    if active is not None:
        raise ProblemError(
            409, "already-consented", "This patient already consented to this notice."
        )
    now = await db_now(db)
    consent_id = uuid7()
    consent = Consent(
        id=consent_id,
        clinic_id=principal.clinic_id,
        patient_id=patient_id,
        notice_id=notice.id,
        granted_at=now,
        channel=(
            ConsentChannel.PATIENT_PORTAL
            if principal.role == Role.PATIENT
            else ConsentChannel.RECEPTION
        ),
        salt_hash=commitment(consent_id, patient_id, notice.id),
    )
    db.add(consent)
    await db.flush()
    db.add(
        ConsentEvent(
            clinic_id=principal.clinic_id,
            consent_id=consent.id,
            kind=ConsentEventKind.GRANTED,
            at=now,
            by_user_id=principal.user_id,
        )
    )
    await db.flush()
    await db.refresh(consent)
    return consent


async def withdraw(
    db: AsyncSession, principal: ClinicPrincipal, consent_id: uuid.UUID, req: RequestInfo
) -> Consent:
    consent = await db.get(Consent, consent_id, with_for_update=True)
    if consent is None:
        raise ProblemError(404, "consent-not-found")
    await record_and_explain(
        db,
        principal,
        consent.patient_id,
        CONSENT,
        AccessAction.EDIT,
        req,
        detail={"op": "consent.withdraw", "consent_id": str(consent.id)},
    )
    if consent.withdrawn_at is not None:
        raise ProblemError(409, "already-withdrawn")
    now = await db_now(db)
    consent.withdrawn_at = now
    db.add(
        ConsentEvent(
            clinic_id=principal.clinic_id,
            consent_id=consent.id,
            kind=ConsentEventKind.WITHDRAWN,
            at=now,
            by_user_id=principal.user_id,
        )
    )
    await db.flush()
    return consent


async def history(
    db: AsyncSession, principal: ClinicPrincipal, patient_id: uuid.UUID, req: RequestInfo
) -> list[schemas.ConsentOut]:
    await record_and_explain(db, principal, patient_id, CONSENT, AccessAction.VIEW, req)
    consents = (
        await db.scalars(
            select(Consent)
            .where(Consent.patient_id == patient_id)
            .order_by(Consent.granted_at.desc())
        )
    ).all()
    return [await consent_out(db, c) for c in consents]
