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
from dataclasses import asdict
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.service import RequestInfo, db_now, record_and_explain
from app.anchor.evm import clinic_bytes32
from app.anchor.proofs import anchors_of, checkpoint_for_leaf
from app.core.errors import ProblemError
from app.db.enums import AccessAction, RecordType
from app.db.models import AuditEvent, MerkleCheckpoint, RecordVersion
from app.identity.deps import ClinicPrincipal
from app.ledger import tree_cache
from app.ledger.checkpoint import (
    clinic_signing_key,
    latest_checkpoint,
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
    anchored_size,
    check_clinic,
    check_record,
    leaves_for,
    stored_for,
    verdict,
)
from app.records.schemas import RecordIntegrity, RecordsStatus, VersionProof
from e2d_core.ledger import leaf_hash, payload_hash, public_key_bytes, verify_inclusion

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
    """Inclusion proofs for leaves, each against the checkpoint receipts would use, from the cached
    tree of stored leaf hashes (app.ledger.tree_cache), never a re-hash of the whole log."""

    def __init__(self, db: AsyncSession, clinic_id: uuid.UUID, entry: tree_cache.Entry | None):
        self.db = db
        self.clinic_id = clinic_id
        self.entry = entry
        self.found: dict[int, tuple[MerkleCheckpoint | None, bool]] = {}
        self.leaves: dict[int, Leaf] = {}

    def index(self, seq: int) -> int | None:
        return self.entry.index_of(seq) if self.entry is not None else None

    async def load(self, leaves: Sequence[Leaf]) -> None:
        for leaf in leaves:
            self.leaves[leaf.seq] = leaf
            index = self.index(leaf.seq)
            if index is None:
                self.found[leaf.seq] = (None, False)
            else:
                self.found[leaf.seq] = await checkpoint_for_leaf(self.db, self.clinic_id, index)

    def anchored(self, seq: int) -> bool:
        return self.found.get(seq, (None, False))[1]

    def rewritten(self) -> bool:
        """True when a record leaf no longer hashes to its stored payload_hash, or its proof no
        longer reaches its checkpoint's signed root: the log was changed after that checkpoint,
        even if its hash chain was recomputed."""
        for seq, (cp, _) in self.found.items():
            leaf = self.leaves[seq]
            if leaf.payload_hash is None or payload_hash(leaf.payload) != leaf.payload_hash:
                return True
            index = self.index(seq)
            if cp is None or index is None or self.entry is None:
                continue
            proof = self.entry.tree.inclusion_proof(index, cp.tree_size)
            if not verify_inclusion(
                leaf_hash(leaf.payload_hash), index, cp.tree_size, proof, cp.root
            ):
                return True
        return False

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
        # The stored hash, as in receipts: the browser recomputes it from the payload.
        digest = leaf.payload_hash if leaf.payload_hash is not None else payload_hash(leaf.payload)
        base["payload_hash"] = digest.hex()
        base["leaf_hash"] = leaf_hash(digest).hex()
        index = self.index(leaf.seq)
        base["leaf_index"] = index
        checkpoint, anchored = self.found.get(leaf.seq, (None, False))
        if checkpoint is not None and index is not None and self.entry is not None:
            base["proof"] = [
                p.hex() for p in self.entry.tree.inclusion_proof(index, checkpoint.tree_size)
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
    latest = await latest_checkpoint(db, clinic_id)
    entry = None
    if latest is not None:
        entry = await tree_cache.tree_for(db, clinic_id, latest.tree_size)
        if entry.size < latest.tree_size:
            entry, problem = None, "the audit log is shorter than its signed checkpoint"
    proofs = _Proofs(db, clinic_id, entry)
    await proofs.load([v.leaf for c in checks for v in c.versions if v.leaf is not None])
    result = verdict(checks, proofs.anchored)
    reason = _reason(result, checks)
    if problem is None and proofs.rewritten():
        problem = "the audit log no longer matches its signed checkpoint"
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


# Records status for the admin page ---------------------------------------------------------------

# (clinic, latest checkpoint size, chain-anchored size) -> result. The check reads and decrypts
# every committed row of the clinic, so a repeat request reuses the result until a new checkpoint is
# signed or anchored. Per API process; a restart or another worker computes it again.
_STATUS_CACHE: dict[tuple[uuid.UUID, int, int], RecordsStatus] = {}
_STATUS_CACHE_LIMIT = 256


async def _leaf_positions(db: AsyncSession, clinic_id: uuid.UUID) -> dict[int, int]:
    """seq -> position in the clinic's log, without re-hashing it (the audit page checks that)."""
    seqs = await db.scalars(
        select(AuditEvent.seq).where(AuditEvent.clinic_id == clinic_id).order_by(AuditEvent.seq)
    )
    return {int(seq): i for i, seq in enumerate(seqs.all())}


async def records_status(db: AsyncSession, clinic_id: uuid.UUID) -> RecordsStatus:
    latest = await latest_checkpoint(db, clinic_id)
    tree_size = latest.tree_size if latest is not None else 0
    size = await anchored_size(db, clinic_id)
    key = (clinic_id, tree_size, size)
    cached = _STATUS_CACHE.get(key)
    if cached is not None:
        return cached.model_copy(update={"cached": True})
    positions = await _leaf_positions(db, clinic_id)
    found = await check_clinic(db, clinic_id, lambda seq: positions.get(seq, size) < size)
    result = RecordsStatus(
        **asdict(found),
        computed_at=await db_now(db),
        tree_size=tree_size,
        anchored_size=size,
        cached=False,
    )
    if len(_STATUS_CACHE) >= _STATUS_CACHE_LIMIT:
        _STATUS_CACHE.clear()
    _STATUS_CACHE[key] = result
    return result


def clear_status_cache() -> None:
    _STATUS_CACHE.clear()
