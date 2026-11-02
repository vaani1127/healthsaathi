import uuid

from e2d_core.ledger.merkle import MerkleTree, leaf_hash
from e2d_core.ledger.sth import TreeHead, derive_clinic_key, key_id, public_key_bytes, sign
from e2d_core.ledger.witness import PublishedHead, check_log

CLINIC = uuid.UUID("01920000-0000-7000-8000-0000000000c9")
KEY = derive_clinic_key(b"witness-test-seed-0000000000000001", CLINIC)
PUBLIC = public_key_bytes(KEY)


def tree(n: int, changed: int | None = None) -> MerkleTree:
    return MerkleTree(
        [leaf_hash(f"event-{i}{'-rewritten' if i == changed else ''}".encode()) for i in range(n)]
    )


def published(t: MerkleTree, size: int, key=KEY) -> PublishedHead:  # type: ignore[no-untyped-def]
    public = public_key_bytes(key)
    head = TreeHead(
        clinic_id=str(CLINIC),
        tree_size=size,
        root_hex=t.root(size).hex(),
        prev_root_hex=None,
        timestamp=1_790_000_000_000 + size,
        key_id=key_id(public),
    )
    return PublishedHead(head, sign(head, key), public)


def test_an_untouched_log_matches_every_published_head() -> None:
    log = tree(10)
    heads = [published(log, s) for s in (7, 3, 10)]
    results = check_log(log, heads, PUBLIC)
    assert [r.tree_size for r in results] == [3, 7, 10]
    assert all(r.ok for r in results)


def test_a_rewritten_event_fails_every_head_that_covers_it() -> None:
    original = tree(10)
    heads = [published(original, s) for s in (3, 7, 10)]
    rewritten = tree(12, changed=5)  # event 5 rewritten, hash chain recomputed, 2 new events
    results = {r.tree_size: r for r in check_log(rewritten, heads, PUBLIC)}
    assert results[3].ok  # published before event 5 existed
    assert not results[7].ok and not results[10].ok
    assert results[7].signature_ok and "root differs" in (results[7].reason or "")


def test_a_shorter_log_fails() -> None:
    heads = [published(tree(10), 10)]
    (result,) = check_log(tree(6), heads, PUBLIC)
    assert not result.ok and "shorter" in (result.reason or "")


def test_a_head_signed_with_another_key_is_rejected() -> None:
    other = derive_clinic_key(b"someone-else-seed-00000000000000002", CLINIC)
    log = tree(4)
    (result,) = check_log(log, [published(log, 4, key=other)], PUBLIC)
    assert not result.signature_ok and not result.ok


def test_a_head_with_a_changed_root_is_rejected() -> None:
    log = tree(4)
    good = published(log, 4)
    forged_head = TreeHead(**{**good.head.__dict__, "root_hex": "00" * 32})
    (result,) = check_log(log, [PublishedHead(forged_head, good.signature, PUBLIC)], PUBLIC)
    assert not result.signature_ok
