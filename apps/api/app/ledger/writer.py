"""Append audit events to a clinic's hash chain.

Call inside the caller's transaction (the same one as the read or write being audited), with a
session whose tenant context is the event's clinic. A transaction-level advisory lock per clinic
makes concurrent writers take turns, so every event links to the one before it.
"""

import hashlib
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, insert, select, text
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


async def _reserve(db: AsyncSession, clinic_id: uuid.UUID, count: int) -> tuple[bytes, list[int]]:
    """The clinic's chain head and `count` new sequence numbers, in one statement. Call after
    lock_clinic, so the head cannot move until the transaction ends."""
    head = (
        select(AuditEvent.chain_hash)
        .where(AuditEvent.clinic_id == clinic_id)
        .order_by(AuditEvent.seq.desc())
        .limit(1)
        .scalar_subquery()
    )
    rows = (
        await db.execute(
            select(head, AUDIT_SEQ.next_value()).select_from(func.generate_series(1, count))
        )
    ).all()
    prev: bytes = rows[0][0] if rows and rows[0][0] is not None else GENESIS
    return prev, sorted(int(r[1]) for r in rows)


async def append_audit_event(
    db: AsyncSession, clinic_id: uuid.UUID, kind: str, payload: dict[str, Any]
) -> AuditEvent:
    await lock_clinic(db, "audit-chain", clinic_id)
    prev_hash, (seq,) = await _reserve(db, clinic_id, 1)
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


async def append_audit_events(
    db: AsyncSession, clinic_id: uuid.UUID, kind: str, payloads: Sequence[dict[str, Any]]
) -> list[bytes]:
    """Append several events in order with one insert: the same rows and links as calling
    append_audit_event for each payload in turn. Returns their chain hashes."""
    if not payloads:
        return []
    await lock_clinic(db, "audit-chain", clinic_id)
    prev_hash, seqs = await _reserve(db, clinic_id, len(payloads))
    rows: list[dict[str, Any]] = []
    links: list[bytes] = []
    for seq, payload in zip(seqs, payloads, strict=True):
        digest = payload_hash(payload)
        link = chain_hash(prev_hash, digest, seq, clinic_id)
        rows.append(
            {
                "seq": seq,
                "clinic_id": clinic_id,
                "kind": kind,
                "payload": payload,
                "payload_hash": digest,
                "prev_hash": prev_hash,
                "chain_hash": link,
            }
        )
        links.append(link)
        prev_hash = link
    await db.execute(insert(AuditEvent), rows)
    return links
