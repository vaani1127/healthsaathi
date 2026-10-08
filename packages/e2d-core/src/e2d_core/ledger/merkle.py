"""RFC 6962 Merkle hash trees with inclusion and consistency proofs.

leaf hash = SHA-256(0x00 || data), node hash = SHA-256(0x01 || left || right), and the hash of an
empty tree is SHA-256 of the empty string. Proof generation follows RFC 6962 section 2.1;
verification follows RFC 9162 sections 2.1.3.2 and 2.1.4.2.
"""

import hashlib
from collections.abc import Sequence

HASH_SIZE = 32
EMPTY_ROOT = hashlib.sha256(b"").digest()


def leaf_hash(data: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + data).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _split(n: int) -> int:
    """Largest power of two strictly smaller than n (n > 1)."""
    return 1 << ((n - 1).bit_length() - 1)


class MerkleTree:
    """A tree over leaf hashes. Perfect subtrees are cached level by level."""

    def __init__(self, leaf_hashes: Sequence[bytes]) -> None:
        self.levels: list[list[bytes]] = [list(leaf_hashes)]
        level = self.levels[0]
        while len(level) > 1:
            level = [node_hash(level[i], level[i + 1]) for i in range(0, len(level) - 1, 2)]
            self.levels.append(level)

    @classmethod
    def from_data(cls, items: Sequence[bytes]) -> "MerkleTree":
        return cls([leaf_hash(d) for d in items])

    @property
    def size(self) -> int:
        return len(self.levels[0])

    def root(self, size: int | None = None) -> bytes:
        size = self.size if size is None else size
        if not 0 <= size <= self.size:
            raise ValueError("size out of range")
        return EMPTY_ROOT if size == 0 else self._hash(0, size)

    def _hash(self, start: int, end: int) -> bytes:
        width = end - start
        if width & (width - 1) == 0 and start % width == 0:
            return self.levels[width.bit_length() - 1][start // width]
        k = _split(width)
        return node_hash(self._hash(start, start + k), self._hash(start + k, end))

    def inclusion_proof(self, index: int, size: int | None = None) -> list[bytes]:
        size = self.size if size is None else size
        if not 0 <= index < size <= self.size:
            raise ValueError("index or size out of range")
        return self._path(index, 0, size)

    def _path(self, m: int, start: int, end: int) -> list[bytes]:
        n = end - start
        if n == 1:
            return []
        k = _split(n)
        if m < k:
            return [*self._path(m, start, start + k), self._hash(start + k, end)]
        return [*self._path(m - k, start + k, end), self._hash(start, start + k)]

    def consistency_proof(self, old_size: int, new_size: int | None = None) -> list[bytes]:
        new_size = self.size if new_size is None else new_size
        if not 0 < old_size <= new_size <= self.size:
            raise ValueError("sizes out of range")
        return self._subproof(old_size, 0, new_size, True)

    def _subproof(self, m: int, start: int, end: int, complete: bool) -> list[bytes]:
        n = end - start
        if m == n:
            return [] if complete else [self._hash(start, end)]
        k = _split(n)
        if m <= k:
            return [*self._subproof(m, start, start + k, complete), self._hash(start + k, end)]
        return [*self._subproof(m - k, start + k, end, False), self._hash(start, start + k)]


def verify_inclusion(
    leaf: bytes, index: int, size: int, proof: Sequence[bytes], root: bytes
) -> bool:
    """Check that `leaf` (a leaf hash) is at `index` in the tree of `size` with `root`."""
    if not 0 <= index < size:
        return False
    fn, sn = index, size - 1
    r = leaf
    for p in proof:
        if len(p) != HASH_SIZE or sn == 0:
            return False
        if fn & 1 or fn == sn:
            r = node_hash(p, r)
            if not fn & 1:
                while fn & 1 == 0 and fn != 0:
                    fn >>= 1
                    sn >>= 1
        else:
            r = node_hash(r, p)
        fn >>= 1
        sn >>= 1
    return sn == 0 and r == root


def verify_consistency(
    old_size: int, new_size: int, old_root: bytes, new_root: bytes, proof: Sequence[bytes]
) -> bool:
    """Check that the tree of `old_size` with `old_root` is a prefix of the one with `new_root`."""
    if old_size < 0 or new_size < old_size:
        return False
    if old_size == new_size:
        return len(proof) == 0 and old_root == new_root
    if old_size == 0:
        return len(proof) == 0
    if not proof or any(len(p) != HASH_SIZE for p in proof):
        return False

    path = list(proof)
    if old_size & (old_size - 1) == 0:
        path.insert(0, old_root)
    fn, sn = old_size - 1, new_size - 1
    while fn & 1:
        fn >>= 1
        sn >>= 1
    fr = sr = path[0]
    for c in path[1:]:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            fr = node_hash(c, fr)
            sr = node_hash(c, sr)
            if not fn & 1:
                while fn & 1 == 0 and fn != 0:
                    fn >>= 1
                    sn >>= 1
        else:
            sr = node_hash(sr, c)
        fn >>= 1
        sn >>= 1
    return sn == 0 and fr == old_root and sr == new_root
