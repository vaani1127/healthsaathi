"""Record integrity check (ADR 0011): is this clinical record still what was committed?

The check reads the record's audit leaves (the reference: they are in the hash chain and under the
signed, anchored roots), its record_versions rows (salt and commitment) and the record row, and
returns a verdict plus, for each version, the data to verify it again in the browser.

A note or prescription is a chain of rows, one per version; the check covers the whole chain the
requested row belongs to. Every check is recorded through `record_and_explain()`, like any other
read of patient data.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.service import RequestInfo, record_and_explain
from app.anchor.evm import clinic_bytes32
from app.anchor.proofs import anchors_of, checkpoint_for_leaf
from app.core.errors import ProblemError
from app.db.enums import AccessAction, RecordType
from app.db.models import MerkleCheckpoint, RecordVersion
from app.identity.deps import ClinicPrincipal
from app.ledger.checkpoint import (
    LedgerIntegrityError,
    clinic_signing_key,
    load_tree,
    tree_head,
)
from app.ledger.records import (
    CHAINED,
    MODELS,
    RESOURCES,
    Leaf,
    RecordCheck,
    Verdict,
    VersionCheck,
    VersionStatus,
    check_record,
    leaves_for,
    stored_for,
    verdict,
)
from app.records.schemas import RecordIntegrity, VersionProof
from e2d_core.ledger import MerkleTree, leaf_hash, payload_hash, public_key_bytes

MAX_CHAIN = 1000  # far above any real note history; stops a cycle written into the database

REASONS = {
    Verdict.MISSING_VERSION: "a signed record has no committed version",
    Verdict.PENDING: "committed, but not yet under a tree head confirmed on a chain",
}


async def _chain(db: AsyncSession, model: Any, row: Any) -> list[Any]:
    """Every version row of a note or prescription, oldest first."""
    older: list[Any] = []
    current = row
    while current.parent_id is not None and len(older) < MAX_CHAIN:
        parent = await db.get(model, current.parent_id)
        if parent is None:
            break
        older.append(parent)
        current = parent
    chain = [*reversed(older), row]
    current = row
    while len(chain) < MAX_CHAIN:
        child = await db.scalar(select(model).where(model.parent_id == current.id))
        if child is None:
            break
        chain.append(child)
        current = child
    return chain


class _Proofs:
    """Inclusion proofs for leaves, each against the checkpoint receipts would use."""

    def __init__(
        self, db: AsyncSession, clinic_id: uuid.UUID, tree: MerkleTree | None, seqs: list[int]
    ):
        self.db = db
        self.clinic_id = clinic_id
        self.tree = tree
        self.index = {seq: i for i, seq in enumerate(seqs)}
        self.found: dict[int, tuple[MerkleCheckpoint | None, bool]] = {}

    async def load(self, seqs: Sequence[int]) -> None:
        for seq in seqs:
            index = self.index.get(seq)
            if index is None or self.tree is None:
                self.found[seq] = (None, False)
            else:
                self.found[seq] = await checkpoint_for_leaf(self.db, self.clinic_id, index)

    def anchored(self, seq: int) -> bool:
        return self.found.get(seq, (None, False))[1]

    async def out(self, check: VersionCheck) -> VersionProof:
        leaf, stored = check.leaf, check.stored
        backfilled = bool(
            (stored is not None and stored.backfilled)
            or (leaf is not None and leaf.payload.get("backfilled") is True)
        )
        base: dict[str, Any] = {
            "record_id": check.record_id,
            "version": check.version,
            "status": check.status.value,
            "reason": check.reason,
            "backfilled": backfilled,
            "committed_at": stored.created_at if stored is not None else None,
            "content_schema": stored.content_schema if stored is not None else None,
            "content": check.content,
            "salt": stored.salt.hex() if stored is not None else None,
            "commitment": leaf.payload.get("commitment") if leaf is not None else None,
            "audit_seq": leaf.seq if leaf is not None else None,
            "payload": leaf.payload if leaf is not None else None,
            "payload_hash": None,
            "leaf_hash": None,
            "leaf_index": None,
            "proof": None,
            "sth": None,
            "signature": None,
            "anchored": False,
            "anchors": [],
        }
        if leaf is None:
            return VersionProof(**base)
        digest = payload_hash(leaf.payload)
        base["payload_hash"] = digest.hex()
        base["leaf_hash"] = leaf_hash(digest).hex()
        index = self.index.get(leaf.seq)
        base["leaf_index"] = index
        checkpoint, anchored = self.found.get(leaf.seq, (None, False))
        if checkpoint is not None and index is not None and self.tree is not None:
            base["proof"] = [
                p.hex() for p in self.tree.inclusion_proof(index, checkpoint.tree_size)
            ]
            base["sth"] = tree_head(self.clinic_id, checkpoint).to_json()
            base["signature"] = checkpoint.signature.hex()
            base["anchored"] = anchored
            base["anchors"] = await anchors_of(self.db, checkpoint.id)
        return VersionProof(**base)


def _covered_since(versions: Sequence[RecordVersion], leaves: Sequence[Leaf]) -> datetime | None:
    times = [v.created_at for v in versions]
    if times:
        return min(times)
    stamps = [
        datetime.fromisoformat(str(leaf.payload["at"])) for leaf in leaves if "at" in leaf.payload
    ]
    return min(stamps) if stamps else None


def _reason(result: Verdict, checks: Sequence[RecordCheck]) -> str | None:
    if result == Verdict.MATCH:
        return None
    for check in checks:
        for version in check.versions:
            if version.status in (VersionStatus.MISMATCH, VersionStatus.MISSING_ROW):
                return version.reason
    return REASONS.get(result)


async def integrity(
    db: AsyncSession,
    principal: ClinicPrincipal,
    record_type: RecordType,
    record_id: uuid.UUID,
    req: RequestInfo,
) -> RecordIntegrity:
    clinic_id = principal.clinic_id
    model = MODELS[record_type]
    row = await db.get(model, record_id)
    own_versions = [
        v for v in await stored_for(db, clinic_id, [record_id]) if v.record_type == record_type
    ]
    if row is not None:
        patient_id = row.patient_id
    elif own_versions:
        patient_id = own_versions[0].patient_id
    else:
        raise ProblemError(404, "record-not-found")
    await record_and_explain(
        db,
        principal,
        patient_id,
        RESOURCES[record_type],
        AccessAction.VIEW,
        req,
        detail={
            "op": "record.integrity",
            "record_type": record_type.value,
            "record_id": str(record_id),
        },
    )

    rows: dict[uuid.UUID, Any] = {record_id: row}
    if record_type in CHAINED and row is not None:
        rows = {r.id: r for r in await _chain(db, model, row)}
    ids = list(rows)
    leaves = [
        leaf
        for leaf in await leaves_for(db, clinic_id, ids)
        if leaf.payload.get("record_type") == record_type.value
    ]
    stored = [v for v in await stored_for(db, clinic_id, ids) if v.record_type == record_type]
    checks = [
        check_record(
            record_type,
            rid,
            rows[rid],
            [v for v in stored if v.record_id == rid],
            [leaf for leaf in leaves if leaf.payload.get("record_id") == str(rid)],
        )
        for rid in ids
    ]

    problem = None
    try:
        tree: MerkleTree | None
        tree, seqs = await load_tree(db, clinic_id)
    except LedgerIntegrityError as exc:
        tree, seqs, problem = None, [], f"the audit log does not match its own hashes: {exc}"
    proofs = _Proofs(db, clinic_id, tree, seqs)
    await proofs.load([v.leaf.seq for c in checks for v in c.versions if v.leaf is not None])
    result = verdict(checks, proofs.anchored)
    reason = _reason(result, checks)
    if problem is not None:
        result, reason = Verdict.MISMATCH, problem

    return RecordIntegrity(
        record_type=record_type,
        record_id=record_id,
        patient_id=patient_id,
        verdict=result.value,
        reason=reason,
        covered_since=_covered_since(stored, leaves),
        backfilled=any(v.backfilled for v in stored)
        or any(leaf.payload.get("backfilled") is True for leaf in leaves),
        clinic_id=clinic_id,
        clinic_id_bytes32="0x" + clinic_bytes32(clinic_id).hex(),
        public_key=public_key_bytes(clinic_signing_key(clinic_id)).hex(),
        versions=[await proofs.out(v) for c in checks for v in c.versions],
    )
