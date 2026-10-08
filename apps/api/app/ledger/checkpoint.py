"""Build signed Merkle checkpoints over a clinic's audit events.

Before signing, the service re-checks the stored chain (every payload hash and chain hash) and
checks that the previous checkpoint's root is still the root of the first `tree_size` leaves. If
history was changed in the database, no new checkpoint is signed.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import TenantContext, tenant_session
from app.db.models import AuditEvent, Clinic, MerkleCheckpoint
from app.ledger.writer import lock_clinic
from e2d_core.ledger import (
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


async def load_tree(db: AsyncSession, clinic_id: uuid.UUID) -> tuple[MerkleTree, list[int]]:
    """Verify the clinic's chain and return its Merkle tree plus the seq of each leaf."""
    rows = (
        await db.execute(
            select(
                AuditEvent.seq,
                AuditEvent.payload,
                AuditEvent.payload_hash,
                AuditEvent.prev_hash,
                AuditEvent.chain_hash,
            )
            .where(AuditEvent.clinic_id == clinic_id)
            .order_by(AuditEvent.seq)
        )
    ).all()
    for row in rows:
        if payload_hash(row.payload) != row.payload_hash:
            raise LedgerIntegrityError(f"payload of audit event {row.seq} was changed")
    check = verify_chain(
        ChainLink(r.seq, clinic_id, r.payload_hash, r.prev_hash, r.chain_hash) for r in rows
    )
    if not check.ok:
        raise LedgerIntegrityError(
            f"hash chain broken at seq {check.first_bad_seq}: {check.reason}"
        )
    return MerkleTree([leaf_hash(r.payload_hash) for r in rows]), [r.seq for r in rows]


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


async def build_checkpoint(clinic_id: uuid.UUID, now: datetime | None = None) -> CheckpointResult:
    key = clinic_signing_key(clinic_id)
    public_key = public_key_bytes(key)
    ctx = TenantContext(clinic_id=clinic_id, role=SYSTEM_ROLE)
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
