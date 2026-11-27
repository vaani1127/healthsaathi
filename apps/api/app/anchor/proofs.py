"""Which signed checkpoint to prove a leaf against, and where that checkpoint was anchored.

Shared by patient access receipts and record integrity checks (ADR 0011), so both choose the same
checkpoint for a leaf: the newest one that contains it and is confirmed on a chain, else the latest
checkpoint if it contains the leaf.
"""

import uuid

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.enums import AnchorStatus
from app.db.models import AnchorReceipt, MerkleCheckpoint
from app.ledger.checkpoint import latest_checkpoint
from app.ledger.records import CHAIN_BACKENDS

__all__ = [
    "CHAIN_BACKENDS",
    "AnchorRef",
    "anchor_link",
    "anchored_checkpoint",
    "anchors_of",
    "checkpoint_for_leaf",
]


class AnchorRef(BaseModel):
    backend: str
    status: str
    tx_ref: str | None
    block_ref: str | None
    link: str | None


def anchor_link(backend: str, tx_ref: str | None) -> str | None:
    settings = get_settings()
    if tx_ref is None:
        return None
    if backend in ("amoy", "besu") and settings.anchor_explorer_url:
        return f"{settings.anchor_explorer_url.rstrip('/')}/tx/{tx_ref}"
    if backend == "github" and settings.witness_repo and not settings.witness_dir:
        return f"https://github.com/{settings.witness_repo}/commit/{tx_ref}"
    return None


async def anchors_of(db: AsyncSession, checkpoint_id: uuid.UUID) -> list[AnchorRef]:
    rows = (
        await db.scalars(
            select(AnchorReceipt)
            .where(AnchorReceipt.checkpoint_id == checkpoint_id)
            .order_by(AnchorReceipt.backend)
        )
    ).all()
    return [
        AnchorRef(
            backend=r.backend.value,
            status=r.status.value,
            tx_ref=r.tx_ref,
            block_ref=r.block_ref,
            link=anchor_link(r.backend.value, r.tx_ref),
        )
        for r in rows
    ]


async def anchored_checkpoint(
    db: AsyncSession, clinic_id: uuid.UUID, index: int
) -> MerkleCheckpoint | None:
    """The newest checkpoint that contains this leaf and is confirmed on a chain. The latest
    checkpoint may still be waiting for its transaction, and a proof the patient cannot check on
    chain yet is less useful than one for a slightly older, anchored head."""
    confirmed = (
        select(AnchorReceipt.id)
        .where(
            AnchorReceipt.checkpoint_id == MerkleCheckpoint.id,
            AnchorReceipt.backend.in_(CHAIN_BACKENDS),
            AnchorReceipt.status == AnchorStatus.CONFIRMED,
        )
        .exists()
    )
    return await db.scalar(
        select(MerkleCheckpoint)
        .where(
            MerkleCheckpoint.clinic_id == clinic_id,
            MerkleCheckpoint.tree_size > index,
            confirmed,
        )
        .order_by(MerkleCheckpoint.tree_size.desc())
        .limit(1)
    )


async def checkpoint_for_leaf(
    db: AsyncSession, clinic_id: uuid.UUID, index: int
) -> tuple[MerkleCheckpoint | None, bool]:
    """(checkpoint, anchored on a chain). None when no checkpoint contains the leaf yet."""
    checkpoint = await anchored_checkpoint(db, clinic_id, index)
    if checkpoint is not None:
        return checkpoint, True
    latest = await latest_checkpoint(db, clinic_id)
    if latest is None or index >= latest.tree_size:
        return None, False
    return latest, False
