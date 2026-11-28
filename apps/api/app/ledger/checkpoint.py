"""Build signed Merkle checkpoints over a clinic's audit events.

Before signing, the service re-checks the stored chain (every payload hash and chain hash) and
checks that the previous checkpoint's root is still the root of the first `tree_size` leaves. If
history was changed in the database, no new checkpoint is signed.
"""

import asyncio
import json
import uuid
from array import array
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import Text, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import TenantContext, tenant_session
from app.db.models import AuditEvent, Clinic, MerkleCheckpoint
from app.ledger import tree_cache
from app.ledger.writer import lock_clinic
from e2d_core.ledger import (
    GENESIS,
    ChainLink,
    MerkleTree,
    TreeHead,
    derive_clinic_key,
    key_id,
    leaf_hash,
    payload_hash,
    public_key_bytes,
    sign,
    verify_chain,
    verify_consistency,
)

SYSTEM_ROLE = "system"


class LedgerIntegrityError(Exception):
    """The stored audit log no longer matches its own hashes or an earlier checkpoint."""


@lru_cache
def clinic_signing_key(clinic_id: uuid.UUID) -> Ed25519PrivateKey:
    seed = get_settings().ledger_signing_seed.get_secret_value().encode()
    return derive_clinic_key(seed, clinic_id)


def tree_head(clinic_id: uuid.UUID, checkpoint: MerkleCheckpoint) -> TreeHead:
    return TreeHead(
        clinic_id=str(clinic_id),
        tree_size=checkpoint.tree_size,
        root_hex=checkpoint.root.hex(),
        prev_root_hex=checkpoint.prev_root.hex() if checkpoint.prev_root else None,
        timestamp=round(checkpoint.signed_at.timestamp() * 1000),
        key_id=checkpoint.key_id,
    )


Row = tuple[int, str, bytes, bytes, bytes]  # seq, payload as text, payload_hash, prev, chain


def _check_rows(clinic_id: uuid.UUID, rows: list[Row], start_prev: bytes = GENESIS) -> None:
    """Every payload re-hashed and every chain link recomputed, from `start_prev`."""
    for seq, payload_text, digest, _, _ in rows:
        if payload_hash(json.loads(payload_text)) != digest:
            raise LedgerIntegrityError(f"payload of audit event {seq} was changed")
    check = verify_chain((ChainLink(r[0], clinic_id, r[2], r[3], r[4]) for r in rows), start_prev)
    if not check.ok:
        raise LedgerIntegrityError(
            f"hash chain broken at seq {check.first_bad_seq}: {check.reason}"
        )


def _verify_rows(clinic_id: uuid.UUID, rows: list[Row]) -> tuple[MerkleTree, list[int]]:
    """CPU part of load_tree, run in a worker thread so it never blocks the event loop."""
    _check_rows(clinic_id, rows)
    return MerkleTree([leaf_hash(r[2]) for r in rows]), [r[0] for r in rows]


def _verify_appended(
    clinic_id: uuid.UUID, old: tree_cache.Entry, start_prev: bytes, rows: list[Row]
) -> tree_cache.Entry:
    _check_rows(clinic_id, rows, start_prev)
    return tree_cache._build(old, [(r[0], r[2]) for r in rows])


async def _rows(
    db: AsyncSession, clinic_id: uuid.UUID, after_seq: int = -1, up_to_seq: int | None = None
) -> list[Row]:
    query = select(
        AuditEvent.seq,
        cast(AuditEvent.payload, Text),
        AuditEvent.payload_hash,
        AuditEvent.prev_hash,
        AuditEvent.chain_hash,
    ).where(AuditEvent.clinic_id == clinic_id, AuditEvent.seq > after_seq)
    if up_to_seq is not None:
        query = query.where(AuditEvent.seq <= up_to_seq)
    rows = (await db.execute(query.order_by(AuditEvent.seq))).all()
    return [(int(r[0]), str(r[1]), bytes(r[2]), bytes(r[3]), bytes(r[4])) for r in rows]


@dataclass(frozen=True)
class VerifiedLog:
    entry: tree_cache.Entry  # seqs and Merkle tree of the verified rows
    last_chain: bytes  # chain_hash of the last verified row (GENESIS if none)


async def load_tree(db: AsyncSession, clinic_id: uuid.UUID) -> tuple[MerkleTree, list[int]]:
    """Verify the clinic's chain and return its Merkle tree plus the seq of each leaf.

    The full check: every payload re-hashed, every chain link recomputed. Payloads are fetched
    as text (JSONB's own output) and decoded, hashed and checked in a worker thread, so a large
    log does not stop the API answering other requests (ADR 0011)."""
    plain = await _rows(db, clinic_id)
    return await asyncio.to_thread(_verify_rows, clinic_id, plain)


async def verify_log(db: AsyncSession, clinic_id: uuid.UUID, up_to_seq: int) -> VerifiedLog:
    """The full check (as load_tree) of the rows with seq <= up_to_seq."""
    plain = await _rows(db, clinic_id, up_to_seq=up_to_seq)
    tree, seqs = await asyncio.to_thread(_verify_rows, clinic_id, plain)
    entry = tree_cache.Entry(array("q", seqs), tree)
    return VerifiedLog(entry, plain[-1][4] if plain else GENESIS)


async def verify_appended(
    db: AsyncSession, clinic_id: uuid.UUID, done: VerifiedLog, up_to_seq: int
) -> VerifiedLog:
    """Extend a verified log with the rows appended after it (seq above its last, up to
    `up_to_seq`): each new payload re-hashed and each new chain link recomputed from the last
    verified chain hash. The caller must already know the verified rows are unchanged."""
    after = done.entry.seqs[-1] if len(done.entry.seqs) else -1
    plain = await _rows(db, clinic_id, after_seq=after, up_to_seq=up_to_seq)
    if not plain:
        return done
    entry = await asyncio.to_thread(_verify_appended, clinic_id, done.entry, done.last_chain, plain)
    return VerifiedLog(entry, plain[-1][4])


async def leaf_index(db: AsyncSession, clinic_id: uuid.UUID, seq: int) -> int:
    """Position of audit event `seq` in the clinic's log (its leaf index), from the index on
    (clinic_id, seq), without loading the log."""
    count = await db.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.clinic_id == clinic_id, AuditEvent.seq < seq)
    )
    return int(count or 0)


async def latest_checkpoint(db: AsyncSession, clinic_id: uuid.UUID) -> MerkleCheckpoint | None:
    return await db.scalar(
        select(MerkleCheckpoint)
        .where(MerkleCheckpoint.clinic_id == clinic_id)
        .order_by(MerkleCheckpoint.tree_size.desc())
        .limit(1)
    )


@dataclass(frozen=True)
class CheckpointResult:
    checkpoint: MerkleCheckpoint
    head: TreeHead
    created: bool


async def build_checkpoint(
    clinic_id: uuid.UUID, now: datetime | None = None, db_role: str = "app_rw"
) -> CheckpointResult:
    key = clinic_signing_key(clinic_id)
    public_key = public_key_bytes(key)
    ctx = TenantContext(clinic_id=clinic_id, role=SYSTEM_ROLE, db_role=db_role)
    async with tenant_session(ctx) as db:
        await lock_clinic(db, "checkpoint", clinic_id)
        clinic = await db.get(Clinic, clinic_id)
        if clinic is None:
            raise LookupError("clinic not found")
        if clinic.signer_pubkey is None:
            clinic.signer_pubkey = public_key
        elif clinic.signer_pubkey != public_key:
            raise LedgerIntegrityError("clinic signer key does not match the configured seed")

        tree, _ = await load_tree(db, clinic_id)
        previous = await latest_checkpoint(db, clinic_id)
        if previous is not None:
            if previous.tree_size > tree.size:
                raise LedgerIntegrityError("audit log is shorter than the last checkpoint")
            if previous.tree_size == tree.size:
                return CheckpointResult(previous, tree_head(clinic_id, previous), created=False)
            if previous.tree_size > 0:
                proof = tree.consistency_proof(previous.tree_size)
                if not verify_consistency(
                    previous.tree_size, tree.size, previous.root, tree.root(), proof
                ):
                    raise LedgerIntegrityError("audit log changed since the last checkpoint")

        signed_at = (now or datetime.now(UTC)).replace(microsecond=0)
        head = TreeHead(
            clinic_id=str(clinic_id),
            tree_size=tree.size,
            root_hex=tree.root().hex(),
            prev_root_hex=previous.root.hex() if previous else None,
            timestamp=round(signed_at.timestamp() * 1000),
            key_id=key_id(public_key),
        )
        checkpoint = MerkleCheckpoint(
            clinic_id=clinic_id,
            tree_size=head.tree_size,
            root=tree.root(),
            prev_root=previous.root if previous else None,
            signed_at=signed_at,
            signature=sign(head, key),
            key_id=head.key_id,
        )
        db.add(checkpoint)
        await db.flush()
        return CheckpointResult(checkpoint, head, created=True)


@dataclass(frozen=True)
class InclusionProof:
    seq: int
    leaf_index: int
    leaf_hash: bytes
    proof: list[bytes]
    head: TreeHead
    signature: bytes


async def inclusion_proof(clinic_id: uuid.UUID, seq: int) -> InclusionProof:
    """Proof that audit event `seq` is in the clinic's latest checkpoint."""
    ctx = TenantContext(clinic_id=clinic_id, role=SYSTEM_ROLE)
    async with tenant_session(ctx) as db:
        checkpoint = await latest_checkpoint(db, clinic_id)
        if checkpoint is None:
            raise LookupError("no checkpoint yet")
        tree, seqs = await load_tree(db, clinic_id)
    try:
        index = seqs.index(seq)
    except ValueError as exc:
        raise LookupError("audit event not found") from exc
    if index >= checkpoint.tree_size:
        raise LookupError("audit event is newer than the latest checkpoint")
    return InclusionProof(
        seq=seq,
        leaf_index=index,
        leaf_hash=tree.levels[0][index],
        proof=tree.inclusion_proof(index, checkpoint.tree_size),
        head=tree_head(clinic_id, checkpoint),
        signature=checkpoint.signature,
    )
