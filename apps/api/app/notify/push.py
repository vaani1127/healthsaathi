"""Web push to patients when staff open their record (SPEC 1.2 patient portal, SPEC 9 opt-in).

The patient opts in from the portal; the browser's push subscription is stored per user. When a
staff member opens the record of a patient with a subscription, a job is queued in the same
transaction as the access. The worker sends it. The message carries no patient data: it only
says that someone opened the record and links to the access log, where the patient sees who and
why after signing in.

Push is off unless VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY are set.
"""

import asyncio
import base64
import json
import logging
import uuid
from datetime import datetime, timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import ANONYMOUS, tenant_session
from app.db.models import Job, PushSubscription

logger = logging.getLogger(__name__)

ACCESS_JOB = "push.access"
MAX_ATTEMPTS = 5
BATCH = 20
MESSAGE = {
    "title": "HealthSaathi",
    "body": "Someone at your clinic opened your record. Open the app to see who and why.",
    "url": "/patient?tab=log",
}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def generate_vapid_keys() -> tuple[str, str]:
    """(public, private) VAPID keys as base64url: the public key is the uncompressed P-256 point
    the browser needs, the private key is the raw 32-byte scalar pywebpush accepts."""
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    private = key.private_numbers().private_value.to_bytes(32, "big")
    return _b64url(public), _b64url(private)


def enabled() -> bool:
    s = get_settings()
    return bool(s.vapid_public_key and s.vapid_private_key)


async def subscribe(
    db: AsyncSession, user_id: uuid.UUID, endpoint: str, p256dh: str, auth: str
) -> None:
    row = await db.scalar(select(PushSubscription).where(PushSubscription.endpoint == endpoint))
    if row is None:
        db.add(PushSubscription(user_id=user_id, endpoint=endpoint, p256dh=p256dh, auth=auth))
    else:
        # The browser reuses an endpoint after a sign-in by someone else: it moves to this user.
        row.user_id, row.p256dh, row.auth = user_id, p256dh, auth
    await db.flush()


async def unsubscribe(db: AsyncSession, user_id: uuid.UUID, endpoint: str) -> None:
    await db.execute(
        delete(PushSubscription).where(
            PushSubscription.user_id == user_id, PushSubscription.endpoint == endpoint
        )
    )


async def enqueue_access(
    db: AsyncSession, patient_user_id: uuid.UUID, access_event_id: uuid.UUID
) -> bool:
    """Queue a push for the patient if they opted in. Call inside the access transaction."""
    if not enabled():
        return False
    subscribed = await db.scalar(
        select(func.count())
        .select_from(PushSubscription)
        .where(PushSubscription.user_id == patient_user_id)
    )
    if not subscribed:
        return False
    db.add(
        Job(
            kind=ACCESS_JOB,
            payload={"user_id": str(patient_user_id), "access_event_id": str(access_event_id)},
        )
    )
    return True


async def enqueue_accesses(db: AsyncSession, accesses: list[tuple[uuid.UUID, uuid.UUID]]) -> None:
    """enqueue_access for several (patient user, access event) pairs with one lookup."""
    if not enabled() or not accesses:
        return
    users = {user for user, _ in accesses}
    subscribed = set(
        (
            await db.scalars(
                select(PushSubscription.user_id)
                .where(PushSubscription.user_id.in_(users))
                .distinct()
            )
        ).all()
    )
    for user, event in accesses:
        if user in subscribed:
            db.add(
                Job(
                    kind=ACCESS_JOB,
                    payload={"user_id": str(user), "access_event_id": str(event)},
                )
            )


def _send(subscription: PushSubscription, data: str) -> None:
    s = get_settings()
    assert s.vapid_private_key is not None
    webpush(
        subscription_info={
            "endpoint": subscription.endpoint,
            "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
        },
        data=data,
        vapid_private_key=s.vapid_private_key.get_secret_value(),
        vapid_claims={"sub": s.vapid_subject},
        timeout=10,
        ttl=3600,
    )


async def send_pending(limit: int = BATCH) -> int:
    """Send queued push jobs. Gone subscriptions (404/410) are removed; other failures retry
    with a growing delay, up to MAX_ATTEMPTS."""
    if not enabled():
        return 0
    sent = 0
    async with tenant_session(ANONYMOUS) as db:
        jobs = (
            await db.scalars(
                select(Job)
                .where(
                    Job.kind == ACCESS_JOB,
                    Job.done_at.is_(None),
                    Job.run_after <= func.now(),
                    Job.attempts < MAX_ATTEMPTS,
                )
                .order_by(Job.run_after)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for job in jobs:
            user_id = uuid.UUID(job.payload["user_id"])
            subs = (
                await db.scalars(
                    select(PushSubscription).where(PushSubscription.user_id == user_id)
                )
            ).all()
            error = None
            for sub in subs:
                try:
                    await asyncio.to_thread(_send, sub, json.dumps(MESSAGE))
                    sent += 1
                except WebPushException as exc:
                    status = exc.response.status_code if exc.response is not None else None
                    if status in (404, 410):
                        await db.delete(sub)
                    else:
                        error = f"push failed with status {status}"
            job.attempts += 1
            if error is None:
                job.done_at = await _db_now(db)
            else:
                job.error = error
                job.run_after = await _db_now(db) + timedelta(minutes=2**job.attempts)
    return sent


async def _db_now(db: AsyncSession) -> datetime:
    now = await db.scalar(text("SELECT clock_timestamp()"))
    assert isinstance(now, datetime)
    return now
