import hashlib

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from e2d_core.ledger.merkle import (
    EMPTY_ROOT,
    MerkleTree,
    leaf_hash,
    node_hash,
    verify_consistency,
    verify_inclusion,
)

# Test data from the Certificate Transparency reference implementation (merkle_tree_test).
CT_LEAVES = [
    "",
    "00",
    "10",
    "2021",
    "3031",
    "40414243",
    "5051525354555657",
    "606162636465666768696a6b6c6d6e6f",
]
CT_ROOTS = [
    "6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d",
    "fac54203e7cc696cf0dfcb42c92a1d9dbaf70ad9e621f4bd8d98662f00e3c125",
    "aeb6bcfe274b70a14fb067a5e5578264db0fa9b51af5e0ba159158f329e06e77",
    "d37ee418976dd95753c1c73862b9398fa2a2cf9b4ff0fdfe8b30cd95209614b7",
    "4e3bbb1f7b478dcfe71fb631631519a3bca12c9aefca1612bfce4c13a86264d4",
    "76e67dadbcdf1e10e1b74ddc608abd2f98dfb16fbce75277b5232a127f2087ef",
    "ddb89be403809e325750d3d263cd78929c2942b7942a34b77e122c9594a74c8c",
    "5dc9da79a70659a9ad559cb701ded9a2ab9d823aad2f4960cfe370eff4604328",
]


# Reference implementation written straight from the RFC 6962 definitions, kept deliberately naive.
def ref_mth(d: list[bytes]) -> bytes:
    n = len(d)
    if n == 0:
        return hashlib.sha256(b"").digest()
    if n == 1:
        return hashlib.sha256(b"\x00" + d[0]).digest()
    k = 1
    while k * 2 < n:
        k *= 2
    return hashlib.sha256(b"\x01" + ref_mth(d[:k]) + ref_mth(d[k:])).digest()


def ref_path(m: int, d: list[bytes]) -> list[bytes]:
    n = len(d)
    if n == 1:
        return []
    k = 1
    while k * 2 < n:
        k *= 2
    if m < k:
        return [*ref_path(m, d[:k]), ref_mth(d[k:])]
    return [*ref_path(m - k, d[k:]), ref_mth(d[:k])]


def ref_subproof(m: int, d: list[bytes], b: bool) -> list[bytes]:
    n = len(d)
    if m == n:
        return [] if b else [ref_mth(d)]
    k = 1
    while k * 2 < n:
        k *= 2
    if m <= k:
        return [*ref_subproof(m, d[:k], b), ref_mth(d[k:])]
    return [*ref_subproof(m - k, d[k:], False), ref_mth(d[:k])]


def ct_tree(size: int) -> MerkleTree:
    return MerkleTree.from_data([bytes.fromhex(h) for h in CT_LEAVES[:size]])


def test_empty_tree_root() -> None:
    assert MerkleTree([]).root() == EMPTY_ROOT
    assert EMPTY_ROOT.hex() == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


@pytest.mark.parametrize("size", range(1, 9))
def test_ct_reference_roots(size: int) -> None:
    assert ct_tree(8).root(size).hex() == CT_ROOTS[size - 1]
    assert ct_tree(size).root().hex() == CT_ROOTS[size - 1]


def test_leaf_and_node_domain_separation() -> None:
    assert leaf_hash(b"") != hashlib.sha256(b"").digest()
    a, b = leaf_hash(b"a"), leaf_hash(b"b")
    assert node_hash(a, b) == hashlib.sha256(b"\x01" + a + b).digest()


data_lists = st.lists(st.binary(max_size=16), min_size=1, max_size=70)


@settings(max_examples=150, deadline=None)
@given(data_lists)
def test_root_matches_reference(data: list[bytes]) -> None:
    tree = MerkleTree.from_data(data)
    for size in range(len(data) + 1):
        assert tree.root(size) == ref_mth(data[:size])


@settings(max_examples=150, deadline=None)
@given(data_lists)
def test_every_inclusion_proof_verifies(data: list[bytes]) -> None:
    tree = MerkleTree.from_data(data)
    root = tree.root()
    for i, item in enumerate(data):
        proof = tree.inclusion_proof(i)
        assert proof == ref_path(i, data)
        assert verify_inclusion(leaf_hash(item), i, len(data), proof, root)


@settings(max_examples=150, deadline=None)
@given(data_lists)
def test_every_consistency_proof_verifies(data: list[bytes]) -> None:
    tree = MerkleTree.from_data(data)
    n = len(data)
    for m in range(1, n + 1):
        proof = tree.consistency_proof(m, n)
        assert proof == ref_subproof(m, data, True)
        assert verify_consistency(m, n, tree.root(m), tree.root(n), proof)


@settings(max_examples=100, deadline=None)
@given(data_lists, st.data())
def test_single_byte_change_breaks_inclusion(data: list[bytes], draw: st.DataObject) -> None:
    tree = MerkleTree.from_data(data)
    root = tree.root()
    i = draw.draw(st.integers(0, len(data) - 1))
    proof = tree.inclusion_proof(i)
    leaf = bytearray(leaf_hash(data[i]))
    byte = draw.draw(st.integers(0, 31))
    leaf[byte] ^= draw.draw(st.integers(1, 255))
    assert not verify_inclusion(bytes(leaf), i, len(data), proof, root)

    if proof:
        j = draw.draw(st.integers(0, len(proof) - 1))
        bad = bytearray(proof[j])
        bad[draw.draw(st.integers(0, 31))] ^= 1
        tampered = [*proof[:j], bytes(bad), *proof[j + 1 :]]
        assert not verify_inclusion(leaf_hash(data[i]), i, len(data), tampered, root)


@settings(max_examples=100, deadline=None)
@given(st.lists(st.binary(max_size=8), min_size=2, max_size=40), st.data())
def test_changing_any_leaf_breaks_consistency(data: list[bytes], draw: st.DataObject) -> None:
    n = len(data)
    m = draw.draw(st.integers(1, n - 1))
    old_root = MerkleTree.from_data(data).root(m)
    i = draw.draw(st.integers(0, m - 1))
    rewritten = [*data[:i], data[i] + b"!", *data[i + 1 :]]
    tree = MerkleTree.from_data(rewritten)
    proof = tree.consistency_proof(m, n)
    assert not verify_consistency(m, n, old_root, tree.root(n), proof)


def test_inclusion_rejects_bad_arguments() -> None:
    tree = ct_tree(8)
    proof = tree.inclusion_proof(3)
    leaf = leaf_hash(bytes.fromhex(CT_LEAVES[3]))
    root = tree.root()
    assert verify_inclusion(leaf, 3, 8, proof, root)
    assert not verify_inclusion(leaf, 8, 8, proof, root)
    assert not verify_inclusion(leaf, -1, 8, proof, root)
    assert not verify_inclusion(leaf, 3, 8, [*proof, proof[0]], root)
    assert not verify_inclusion(leaf, 3, 8, proof[:-1], root)
    assert not verify_inclusion(leaf, 3, 8, [b"short", *proof[1:]], root)
    assert not verify_inclusion(leaf, 2, 8, proof, root)
    with pytest.raises(ValueError):
        tree.inclusion_proof(8)


def test_consistency_edge_cases() -> None:
    tree = ct_tree(8)
    r4, r8 = tree.root(4), tree.root(8)
    proof = tree.consistency_proof(4, 8)
    assert verify_consistency(4, 8, r4, r8, proof)
    assert verify_consistency(8, 8, r8, r8, [])
    assert not verify_consistency(8, 8, r8, r4, [])
    assert verify_consistency(0, 8, EMPTY_ROOT, r8, [])
    assert not verify_consistency(0, 8, EMPTY_ROOT, r8, proof)
    assert not verify_consistency(4, 8, r4, r8, [])
    assert not verify_consistency(5, 4, r4, r8, proof)
    assert not verify_consistency(4, 8, r4, r8, [*proof, proof[0]])
    assert not verify_consistency(4, 8, r4, r8, [b"short"])
    assert not verify_consistency(3, 8, tree.root(3), r8, proof)
    with pytest.raises(ValueError):
        tree.consistency_proof(0, 8)
    with pytest.raises(ValueError):
        tree.root(9)


def test_large_tree_root_is_fast_and_matches_split() -> None:
    data = [i.to_bytes(4, "big") for i in range(5000)]
    tree = MerkleTree.from_data(data)
    k = 4096
    assert tree.root() == node_hash(
        MerkleTree.from_data(data[:k]).root(), MerkleTree.from_data(data[k:]).root()
    )
