"""Check an audit log against signed tree heads published on a witness (SPEC 6).

A database owner can rewrite audit rows, recompute the hash chain and even delete the stored
checkpoints, but cannot change a tree head that was already published on a public witness, nor
sign a new one without the clinic key. Each published head must still match the log: its signature
is valid for the clinic key, and the log's Merkle root at the head's size equals the head's root.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from e2d_core.ledger.merkle import MerkleTree
from e2d_core.ledger.sth import TreeHead, key_id, verify


@dataclass(frozen=True)
class PublishedHead:
    head: TreeHead
    signature: bytes
    public_key: bytes


@dataclass(frozen=True)
class HeadCheck:
    tree_size: int
    signature_ok: bool
    root_ok: bool
    reason: str | None

    @property
    def ok(self) -> bool:
        return self.signature_ok and self.root_ok


def check_head(tree: MerkleTree, published: PublishedHead, clinic_key: bytes) -> HeadCheck:
    head = published.head
    signature_ok = (
        published.public_key == clinic_key
        and head.key_id == key_id(clinic_key)
        and verify(head, published.signature, clinic_key)
    )
    if not signature_ok:
        return HeadCheck(head.tree_size, False, False, "signature is not the clinic's")
    if head.tree_size > tree.size:
        return HeadCheck(head.tree_size, True, False, "the log is shorter than the published head")
    if tree.root(head.tree_size).hex() != head.root_hex:
        return HeadCheck(
            head.tree_size, True, False, "the log's root differs from the published head"
        )
    return HeadCheck(head.tree_size, True, True, None)


def check_log(
    tree: MerkleTree, heads: Iterable[PublishedHead], clinic_key: bytes
) -> list[HeadCheck]:
    """One result per published head, smallest tree size first."""
    ordered = sorted(heads, key=lambda p: p.head.tree_size)
    return [check_head(tree, p, clinic_key) for p in ordered]
