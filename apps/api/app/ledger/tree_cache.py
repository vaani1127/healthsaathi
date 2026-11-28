"""Merkle trees for inclusion proofs, built from the stored payload_hash column and cached.

Proofs for patient receipts and record integrity checks need the clinic's Merkle tree up to a
signed checkpoint. Building it by fetching every payload and re-hashing its canonical JSON
(load_tree) takes seconds for a large log and is the full verification, which belongs to the
anchor job and the admin audit page. A proof needs only the leaf hashes, which are
SHA-256(0x00 || payload_hash), so the tree is built from the payload_hash column alone.

This is safe because the proof is never trusted on its own: the browser recomputes the leaf from
the returned payload and checks the proof against the signed tree head, whose root is anchored on
public witnesses. A changed payload or payload_hash gives a leaf or a root that does not match.
The integrity endpoint also checks each proof against the checkpoint's signed root itself.

Cache: per API process, one entry per clinic (only the newest tree), holding the seqs and the
tree of the first `size` leaves in seq order. A request for a size the entry already covers is
served from memory (MerkleTree answers roots and proofs for any smaller size); a larger size
fetches only the missing leaves and rebuilds the tree in a worker thread. The admin audit page
installs the tree it has just verified (the same object, no copy) unless the cached one is
larger. The startup warm-up fills it (app/anchor/warmup.py). Memory cost: see ADR 0011
(measured by tests/ledger/test_tree_cache.py).
"""

import asyncio
import bisect
import uuid
from array import array
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditEvent
from e2d_core.ledger import MerkleTree, leaf_hash


@dataclass
class Entry:
    seqs: array  # type: ignore[type-arg]  # 'q' array of seq, in log order
    tree: MerkleTree

    @property
    def size(self) -> int:
        return self.tree.size

    def index_of(self, seq: int) -> int | None:
        i = bisect.bisect_left(self.seqs, seq)
        return i if i < len(self.seqs) and self.seqs[i] == seq else None


_entries: dict[uuid.UUID, Entry] = {}
_locks: dict[uuid.UUID, asyncio.Lock] = {}
# How many rows were fetched from the database, for tests that check the cache is used.
stats = {"fetches": 0, "rows": 0}


def _lock(clinic_id: uuid.UUID) -> asyncio.Lock:
    lock = _locks.get(clinic_id)
    if lock is None:
        lock = _locks[clinic_id] = asyncio.Lock()
    return lock


async def _fetch(
    db: AsyncSession, clinic_id: uuid.UUID, after_seq: int, limit: int
) -> list[tuple[int, bytes]]:
    rows = (
        await db.execute(
            select(AuditEvent.seq, AuditEvent.payload_hash)
            .where(AuditEvent.clinic_id == clinic_id, AuditEvent.seq > after_seq)
            .order_by(AuditEvent.seq)
            .limit(limit)
        )
    ).all()
    stats["fetches"] += 1
    stats["rows"] += len(rows)
    return [(int(seq), bytes(digest)) for seq, digest in rows]


def _build(old: Entry | None, rows: list[tuple[int, bytes]]) -> Entry:
    seqs = array("q", old.seqs if old else [])
    leaves = list(old.tree.levels[0]) if old else []
    for seq, digest in rows:
        seqs.append(seq)
        leaves.append(leaf_hash(digest))
    return Entry(seqs, MerkleTree(leaves))


async def tree_for(db: AsyncSession, clinic_id: uuid.UUID, size: int) -> Entry:
    """The clinic's tree covering at least `size` leaves (fewer only if the log is shorter)."""
    async with _lock(clinic_id):
        entry = _entries.get(clinic_id)
        if entry is not None and entry.size >= size:
            return entry
        have = entry.size if entry else 0
        after = entry.seqs[-1] if entry and len(entry.seqs) else -1
        rows = await _fetch(db, clinic_id, after, size - have)
        entry = await asyncio.to_thread(_build, entry, rows)
        _entries[clinic_id] = entry
        return entry


def install(clinic_id: uuid.UUID, entry: Entry) -> None:
    """Replace the clinic's entry with a tree just verified (the admin audit page), unless the
    cached one is larger. The same object is kept by the verification cache, so no copy."""
    current = _entries.get(clinic_id)
    if current is None or current.size <= entry.size:
        _entries[clinic_id] = entry


def clear() -> None:
    _entries.clear()
    stats.update(fetches=0, rows=0)
