"""Append audit events to a clinic's hash chain.

Call inside the caller's transaction (the same one as the read or write being audited), with a
session whose tenant context is the event's clinic. A transaction-level advisory lock per clinic
makes concurrent writers take turns, so every event links to the one before it.
"""

import hashlib
import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditEvent
from app.db.models.audit import AUDIT_SEQ
from e2d_core.ledger import GENESIS, chain_hash, payload_hash


def advisory_key(purpose: str, clinic_id: uuid.UUID) -> int:
    digest = hashlib.sha256(purpose.encode() + clinic_id.bytes).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


async def lock_clinic(db: AsyncSession, purpose: str, clinic_id: uuid.UUID) -> None:
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": advisory_key(purpose, clinic_id)}
    )


async def append_audit_event(
    db: AsyncSession, clinic_id: uuid.UUID, kind: str, payload: dict[str, Any]
) -> AuditEvent:
    await lock_clinic(db, "audit-chain", clinic_id)
    prev = await db.scalar(
        select(AuditEvent.chain_hash)
        .where(AuditEvent.clinic_id == clinic_id)
        .order_by(AuditEvent.seq.desc())
        .limit(1)
    )
    prev_hash = prev if prev is not None else GENESIS
    seq = await db.scalar(select(AUDIT_SEQ.next_value()))
    assert seq is not None
    digest = payload_hash(payload)
    event = AuditEvent(
        seq=seq,
        clinic_id=clinic_id,
        kind=kind,
        payload=payload,
        payload_hash=digest,
        prev_hash=prev_hash,
        chain_hash=chain_hash(prev_hash, digest, seq, clinic_id),
    )
    db.add(event)
    await db.flush()
    return event
