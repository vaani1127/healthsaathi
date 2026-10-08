import hashlib
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from hypothesis import given
from hypothesis import strategies as st

from e2d_core.ledger import (
    GENESIS,
    ChainLink,
    TreeHead,
    chain_hash,
    derive_clinic_key,
    key_id,
    payload_hash,
    public_key_bytes,
    sign,
    verify,
    verify_chain,
)

CLINIC = uuid.UUID("01900000-0000-7000-8000-000000000001")


def build_chain(payloads: list[dict[str, object]], start_seq: int = 1) -> list[ChainLink]:
    links = []
    prev = GENESIS
    for i, payload in enumerate(payloads):
        seq = start_seq + i * 3  # seq is global, so a clinic sees gaps
        digest = payload_hash(payload)
        h = chain_hash(prev, digest, seq, CLINIC)
        links.append(ChainLink(seq, CLINIC, digest, prev, h))
        prev = h
    return links


def test_payload_hash_uses_canonical_json() -> None:
    assert payload_hash({"b": 1, "a": 2}) == payload_hash({"a": 2, "b": 1})
    assert payload_hash({"a": 1}) == hashlib.sha256(b'{"a":1}').digest()


def test_chain_hash_layout() -> None:
    digest = bytes(range(32))
    expected = hashlib.sha256(GENESIS + digest + (7).to_bytes(8, "big") + CLINIC.bytes).digest()
    assert chain_hash(GENESIS, digest, 7, CLINIC) == expected
    with pytest.raises(ValueError):
        chain_hash(b"short", digest, 1, CLINIC)
    with pytest.raises(ValueError):
        chain_hash(GENESIS, digest, -1, CLINIC)


@given(st.lists(st.dictionaries(st.text(max_size=5), st.integers(-100, 100)), max_size=20))
def test_valid_chain_verifies(payloads: list[dict[str, object]]) -> None:
    check = verify_chain(build_chain(payloads))
    assert check.ok
    assert check.checked == len(payloads)


def test_tampering_is_located() -> None:
    links = build_chain([{"n": i} for i in range(6)])

    edited = list(links)
    bad = edited[3]
    edited[3] = ChainLink(bad.seq, CLINIC, payload_hash({"n": 99}), bad.prev_hash, bad.chain_hash)
    check = verify_chain(edited)
    assert not check.ok
    assert check.first_bad_seq == links[3].seq
    assert check.reason == "chain_hash does not match"

    removed = links[:2] + links[3:]
    assert verify_chain(removed).reason == "prev_hash does not match"

    swapped = [links[1], links[0], *links[2:]]
    assert not verify_chain(swapped).ok

    reordered = [
        *links[:2],
        ChainLink(0, CLINIC, *(links[2].payload_hash, links[2].prev_hash, links[2].chain_hash)),
    ]
    assert verify_chain(reordered).reason == "seq not increasing"


def test_recomputed_chain_after_edit_differs_from_anchored_head() -> None:
    """A DBA can recompute the whole chain, but the final hash changes, which anchoring catches."""
    original = build_chain([{"n": i} for i in range(5)])
    rewritten = build_chain([{"n": i} if i != 2 else {"n": 42} for i in range(5)])
    assert verify_chain(rewritten).ok
    assert rewritten[-1].chain_hash != original[-1].chain_hash


def head(key: Ed25519PrivateKey, **overrides: object) -> TreeHead:
    values: dict[str, object] = {
        "clinic_id": str(CLINIC),
        "tree_size": 5,
        "root_hex": "ab" * 32,
        "prev_root_hex": None,
        "timestamp": 1_800_000_000_000,
        "key_id": key_id(public_key_bytes(key)),
    }
    values.update(overrides)
    return TreeHead(**values)  # type: ignore[arg-type]


def test_sign_and_verify() -> None:
    key = derive_clinic_key(b"s" * 32, CLINIC)
    h = head(key)
    sig = sign(h, key)
    pub = public_key_bytes(key)
    assert verify(h, sig, pub)
    assert len(h.key_id) == 16
    assert h.signing_bytes().startswith(b'{"clinic_id":')
    assert TreeHead.from_json(h.to_json()) == h


@pytest.mark.parametrize(
    "change",
    [
        {"tree_size": 6},
        {"root_hex": "cd" * 32},
        {"prev_root_hex": "00" * 32},
        {"timestamp": 1},
        {"clinic_id": str(uuid.uuid4())},
    ],
)
def test_any_field_change_breaks_signature(change: dict[str, object]) -> None:
    key = derive_clinic_key(b"s" * 32, CLINIC)
    sig = sign(head(key), key)
    assert not verify(head(key, **change), sig, public_key_bytes(key))


def test_wrong_key_and_key_id() -> None:
    key = derive_clinic_key(b"s" * 32, CLINIC)
    other = derive_clinic_key(b"s" * 32, uuid.uuid4())
    h = head(key)
    sig = sign(h, key)
    assert not verify(h, sig, public_key_bytes(other))
    assert not verify(h, sig[:-1] + bytes([sig[-1] ^ 1]), public_key_bytes(key))
    assert not verify(h, sig, b"\x00" * 31)
    with pytest.raises(ValueError):
        sign(head(other), key)


def test_key_derivation_is_deterministic_and_separated() -> None:
    a1 = public_key_bytes(derive_clinic_key(b"s" * 32, CLINIC))
    a2 = public_key_bytes(derive_clinic_key(b"s" * 32, CLINIC))
    rotated = public_key_bytes(derive_clinic_key(b"s" * 32, CLINIC, version=2))
    other_seed = public_key_bytes(derive_clinic_key(b"t" * 32, CLINIC))
    assert a1 == a2
    assert len({a1, rotated, other_seed}) == 3
    with pytest.raises(ValueError):
        derive_clinic_key(b"short", CLINIC)


def test_digest_is_sha256_of_signed_bytes() -> None:
    key = derive_clinic_key(b"s" * 32, CLINIC)
    h = head(key)
    assert h.digest() == hashlib.sha256(h.signing_bytes()).digest()
