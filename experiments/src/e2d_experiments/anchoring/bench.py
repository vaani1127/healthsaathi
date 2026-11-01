"""Proof size and Merkle tree build time against tree size, using the shared e2d-core ledger.

The build time is the in-memory part of a checkpoint (leaf hashes to root) on the machine that runs
the script; reading the events from the database is not included. The machine is recorded.
"""

import os
import platform
import statistics
import time
import uuid
from datetime import UTC, datetime

import polars as pl

from e2d_core.ledger.merkle import MerkleTree, leaf_hash
from e2d_core.ledger.sth import TreeHead

HASH_BYTES = 32
SIGNATURE_BYTES = 64


def sth_bytes(tree_size: int) -> int:
    """Size of a signed tree head as published: canonical JSON plus the Ed25519 signature."""
    head = TreeHead(
        clinic_id=str(uuid.UUID(int=0)),
        tree_size=tree_size,
        root_hex="00" * HASH_BYTES,
        prev_root_hex="00" * HASH_BYTES,
        timestamp=int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000),
        key_id="0" * 16,
    )
    return len(head.signing_bytes()) + SIGNATURE_BYTES


def leaves(n: int) -> list[bytes]:
    return [leaf_hash(i.to_bytes(8, "big")) for i in range(n)]


def proof_sizes(sizes: list[int]) -> pl.DataFrame:
    rows = []
    for n in sizes:
        tree = MerkleTree(leaves(n))
        longest = max(len(tree.inclusion_proof(i)) for i in (0, n // 2, n - 1))
        consistency = len(tree.consistency_proof(max(1, n // 2))) if n > 1 else 0
        rows.append(
            {
                "tree_size": n,
                "inclusion_hashes": longest,
                "inclusion_bytes": longest * HASH_BYTES,
                "consistency_bytes": consistency * HASH_BYTES,
                "sth_bytes": sth_bytes(n),
            }
        )
    return pl.DataFrame(rows)


def build_times(sizes: list[int], repeats: int) -> pl.DataFrame:
    rows = []
    for n in sizes:
        hashes = leaves(n)
        times = []
        for _ in range(repeats):
            start = time.perf_counter()
            MerkleTree(hashes).root()
            times.append(time.perf_counter() - start)
        rows.append(
            {
                "tree_size": n,
                "repeats": repeats,
                "median_s": statistics.median(times),
                "max_s": max(times),
            }
        )
    return pl.DataFrame(rows)


def machine() -> str:
    system = f"{platform.system()} {platform.machine()}"
    return f"{system}, {os.cpu_count()} CPUs, Python {platform.python_version()}"
