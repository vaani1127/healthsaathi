"""Patient receipts and the admin audit status page (SPEC 6 and 9).

A receipt lets the patient check, in the browser, that one access to their record is in the
clinic's audit log: the audit payload, its leaf hash, an inclusion proof to a signed tree head, the
clinic's public key, and where that tree head was anchored.
"""

import uuid
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.service import RequestInfo, record_and_explain
from app.anchor.evm import clinic_bytes32
from app.core.config import get_settings
from app.core.errors import ProblemError
from app.db.enums import AccessAction, AnchorStatus, ResourceType
from app.db.models import AccessEvent, AnchorReceipt, AuditEvent, MerkleCheckpoint
from app.identity.deps import ClinicPrincipal
from app.ledger.checkpoint import (
    LedgerIntegrityError,
    clinic_signing_key,
    latest_checkpoint,
    load_tree,
    tree_head,
)
from e2d_core.ledger import leaf_hash, public_key_bytes, verify_consistency


class AnchorRef(BaseModel):
    backend: str
    status: str
    tx_ref: str | None
    block_ref: str | None
    link: str | None


class Receipt(BaseModel):
    version: int = 1
    clinic_id: str
    clinic_id_bytes32: str
    public_key: str
    audit_seq: int
    payload: dict[str, Any]
    payload_hash: str
    leaf_hash: str
    leaf_index: int
    proof: list[str]
    sth: dict[str, Any]
    signature: str
    anchors: list[AnchorRef]


def _link(backend: str, tx_ref: str | None) -> str | None:
    settings = get_settings()
    if tx_ref is None:
        return None
    if backend in ("amoy", "besu") and settings.anchor_explorer_url:
        return f"{settings.anchor_explorer_url.rstrip('/')}/tx/{tx_ref}"
    if backend == "github" and settings.witness_repo:
        return f"https://github.com/{settings.witness_repo}/commit/{tx_ref}"
    return None


async def _anchors(db: AsyncSession, checkpoint_id: uuid.UUID) -> list[AnchorRef]:
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
            link=_link(r.backend.value, r.tx_ref),
        )
        for r in rows
    ]


async def receipt_for(
    db: AsyncSession, principal: ClinicPrincipal, access_event_id: uuid.UUID, req: RequestInfo
) -> Receipt:
    event = await db.scalar(select(AccessEvent).where(AccessEvent.id == access_event_id))
    if event is None:
        raise ProblemError(404, "access-not-found")
    await record_and_explain(
        db,
        principal,
        event.patient_id,
        ResourceType.DEMOGRAPHICS,
        AccessAction.VIEW,
        req,
        detail={"op": "receipt", "access_event_id": str(event.id)},
    )
    audit = await db.scalar(
        select(AuditEvent).where(
            AuditEvent.kind == "access",
            text("(payload ->> 'access_event_id') = :id").bindparams(id=str(event.id)),
        )
    )
    if audit is None:
        raise ProblemError(404, "audit-not-found")
    checkpoint = await latest_checkpoint(db, principal.clinic_id)
    tree, seqs = await load_tree(db, principal.clinic_id)
    index = seqs.index(audit.seq)
    if checkpoint is None or index >= checkpoint.tree_size:
        raise ProblemError(
            409, "receipt-not-ready", "This access is not in a signed checkpoint yet. Try later."
        )
    return Receipt(
        clinic_id=str(principal.clinic_id),
        clinic_id_bytes32="0x" + clinic_bytes32(principal.clinic_id).hex(),
        public_key=public_key_bytes(clinic_signing_key(principal.clinic_id)).hex(),
        audit_seq=audit.seq,
        payload=audit.payload,
        payload_hash=audit.payload_hash.hex(),
        leaf_hash=leaf_hash(audit.payload_hash).hex(),
        leaf_index=index,
        proof=[p.hex() for p in tree.inclusion_proof(index, checkpoint.tree_size)],
        sth=tree_head(principal.clinic_id, checkpoint).to_json(),
        signature=checkpoint.signature.hex(),
        anchors=await _anchors(db, checkpoint.id),
    )


class AuditStatus(BaseModel):
    sth: dict[str, Any] | None
    signature: str | None
    public_key: str
    events: int
    chain_ok: bool
    consistency_ok: bool | None
    problem: str | None
    anchors: list[AnchorRef]
    confirmed_backends: list[str]


async def audit_status(db: AsyncSession, clinic_id: uuid.UUID) -> AuditStatus:
    problem = None
    events = 0
    chain_ok = True
    consistency_ok: bool | None = None
    try:
        tree, _ = await load_tree(db, clinic_id)
        events = tree.size
    except LedgerIntegrityError as exc:
        chain_ok, problem, tree = False, str(exc), None
    checkpoints = (
        await db.scalars(
            select(MerkleCheckpoint)
            .where(MerkleCheckpoint.clinic_id == clinic_id)
            .order_by(MerkleCheckpoint.tree_size.desc())
            .limit(2)
        )
    ).all()
    latest = checkpoints[0] if checkpoints else None
    if tree is not None and latest is not None and latest.tree_size <= tree.size:
        if tree.root(latest.tree_size) != latest.root:
            consistency_ok, problem = False, "the log no longer matches the latest checkpoint"
        elif len(checkpoints) == 2 and checkpoints[1].tree_size > 0:
            older = checkpoints[1]
            consistency_ok = verify_consistency(
                older.tree_size,
                latest.tree_size,
                older.root,
                latest.root,
                tree.consistency_proof(older.tree_size, latest.tree_size),
            )
        else:
            consistency_ok = True
    anchors = await _anchors(db, latest.id) if latest else []
    return AuditStatus(
        sth=tree_head(clinic_id, latest).to_json() if latest else None,
        signature=latest.signature.hex() if latest else None,
        public_key=public_key_bytes(clinic_signing_key(clinic_id)).hex(),
        events=events,
        chain_ok=chain_ok,
        consistency_ok=consistency_ok,
        problem=problem,
        anchors=anchors,
        confirmed_backends=[a.backend for a in anchors if a.status == AnchorStatus.CONFIRMED.value],
    )
