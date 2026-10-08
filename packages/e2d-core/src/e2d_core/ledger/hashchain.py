"""Per-clinic hash chain over audit events (SPEC 6).

payload_hash = SHA-256(JCS(payload))
chain_hash_n = SHA-256(prev_hash || payload_hash || seq || clinic_id)

seq is 8 bytes big endian and clinic_id is the 16 raw UUID bytes.

The first event of a clinic uses 32 zero bytes as prev_hash.
"""

import hashlib
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from e2d_core.ledger.jcs import canonicalize

GENESIS = bytes(32)


def payload_hash(payload: Any) -> bytes:
    return hashlib.sha256(canonicalize(payload)).digest()


def chain_hash(prev: bytes, payload_digest: bytes, seq: int, clinic_id: uuid.UUID) -> bytes:
    if len(prev) != 32 or len(payload_digest) != 32:
        raise ValueError("hashes must be 32 bytes")
    if not 0 <= seq < 2**63:
        raise ValueError("seq out of range")
    return hashlib.sha256(prev + payload_digest + seq.to_bytes(8, "big") + clinic_id.bytes).digest()


@dataclass(frozen=True)
class ChainLink:
    seq: int
    clinic_id: uuid.UUID
    payload_hash: bytes
    prev_hash: bytes
    chain_hash: bytes


@dataclass(frozen=True)
class ChainCheck:
    ok: bool
    checked: int
    first_bad_seq: int | None = None
    reason: str | None = None


def verify_chain(links: Iterable[ChainLink], start_prev: bytes = GENESIS) -> ChainCheck:
    """Walk links in seq order and report the first one that does not follow from the last."""
    prev = start_prev
    last_seq = -1
    count = 0
    for link in links:
        if link.seq <= last_seq:
            return ChainCheck(False, count, link.seq, "seq not increasing")
        if link.prev_hash != prev:
            return ChainCheck(False, count, link.seq, "prev_hash does not match")
        if chain_hash(prev, link.payload_hash, link.seq, link.clinic_id) != link.chain_hash:
            return ChainCheck(False, count, link.seq, "chain_hash does not match")
        prev = link.chain_hash
        last_seq = link.seq
        count += 1
    return ChainCheck(True, count)
