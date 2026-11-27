"""Proof trees from stored leaf hashes (app.ledger.tree_cache): same proofs as the full re-hash,
cached per clinic, extended for a newer checkpoint, and a tampered payload_hash fails the check
the browser does."""

import hashlib
import tracemalloc
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db import TenantContext, tenant_session
from app.db.enums import Role
from app.ledger import tree_cache
from app.ledger.checkpoint import build_checkpoint, load_tree
from app.ledger.writer import append_audit_events
from e2d_core.ledger import TreeHead, leaf_hash, verify_inclusion
from e2d_core.ledger.jcs import canonicalize
from tests.helpers import Actor, make_actor, make_clinic, make_patient


async def fill(clinic_id: uuid.UUID, n: int, start: int = 0) -> None:
    ctx = TenantContext(clinic_id=clinic_id, role="system")
    async with tenant_session(ctx) as db:
        await append_audit_events(
            db, clinic_id, "test", [{"type": "test", "i": i} for i in range(start, start + n)]
        )


@pytest.fixture
async def clinic(admin_engine: AsyncEngine) -> uuid.UUID:
    return await make_clinic(admin_engine)


async def test_proofs_from_stored_hashes_equal_the_full_rehash(clinic: uuid.UUID) -> None:
    await fill(clinic, 37)
    tree_cache.clear()
    async with tenant_session(TenantContext(clinic_id=clinic, role="system")) as db:
        full, _ = await load_tree(db, clinic)
        entry = await tree_cache.tree_for(db, clinic, 37)
    assert entry.size == 37 and entry.tree.root() == full.root()
    for size in (1, 13, 37):
        for index in range(size):
            assert entry.tree.inclusion_proof(index, size) == full.inclusion_proof(index, size)


async def test_a_second_call_at_the_same_size_does_not_query(clinic: uuid.UUID) -> None:
    await fill(clinic, 20)
    tree_cache.clear()
    ctx = TenantContext(clinic_id=clinic, role="system")
    async with tenant_session(ctx) as db:
        first = await tree_cache.tree_for(db, clinic, 20)
        assert tree_cache.stats == {"fetches": 1, "rows": 20}
        again = await tree_cache.tree_for(db, clinic, 20)
        smaller = await tree_cache.tree_for(db, clinic, 5)
    assert again is first and smaller is first
    assert tree_cache.stats == {"fetches": 1, "rows": 20}


async def test_a_newer_checkpoint_extends_the_cache_with_only_new_leaves(
    clinic: uuid.UUID,
) -> None:
    await fill(clinic, 10)
    tree_cache.clear()
    ctx = TenantContext(clinic_id=clinic, role="system")
    async with tenant_session(ctx) as db:
        await tree_cache.tree_for(db, clinic, 10)
    await fill(clinic, 6, start=10)
    async with tenant_session(ctx) as db:
        entry = await tree_cache.tree_for(db, clinic, 16)
        full, seqs = await load_tree(db, clinic)
    assert tree_cache.stats == {"fetches": 2, "rows": 16}
    assert entry.size == 16 and entry.tree.root() == full.root()
    assert [entry.index_of(s) for s in seqs] == list(range(16))


async def test_only_the_newest_tree_per_clinic_is_kept(clinic: uuid.UUID) -> None:
    other = uuid.uuid4()
    tree_cache.clear()
    await fill(clinic, 4)
    async with tenant_session(TenantContext(clinic_id=clinic, role="system")) as db:
        await tree_cache.tree_for(db, clinic, 4)
    await fill(clinic, 4, start=4)
    async with tenant_session(TenantContext(clinic_id=clinic, role="system")) as db:
        await tree_cache.tree_for(db, clinic, 8)
    assert set(tree_cache._entries) == {clinic} and other not in tree_cache._entries
    assert tree_cache._entries[clinic].size == 8


def test_memory_of_a_cached_tree_of_100000_leaves() -> None:
    """The figure in ADR 0011 comes from this test (run with -s to see it)."""
    leaves = [hashlib.sha256(i.to_bytes(8, "big")).digest() for i in range(100_000)]
    rows = [(i + 1, d) for i, d in enumerate(leaves)]
    tracemalloc.start()
    entry = tree_cache._build(None, rows)
    current, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert entry.size == 100_000
    mib = current / 2**20
    print(f"\ncached tree of 100,000 leaves: {mib:.1f} MiB")
    assert mib < 64


async def test_a_tampered_payload_hash_gives_a_receipt_that_fails_the_browser_check(
    client: AsyncClient, admin_engine: AsyncEngine, clinic: uuid.UUID
) -> None:
    reception: Actor = await make_actor(admin_engine, clinic, Role.RECEPTION)
    patient_actor = await make_actor(admin_engine, clinic, Role.PATIENT)
    patient = await make_patient(admin_engine, clinic, reception.user_id, patient_actor.user_id)
    await client.get(f"/api/v1/patients/{patient}", headers=reception.headers)
    log = await client.get(f"/api/v1/patients/{patient}/access-log", headers=patient_actor.headers)
    event_id = log.json()["items"][0]["id"]
    await build_checkpoint(clinic)
    url = f"/api/v1/access-events/{event_id}/receipt"
    good = (await client.get(url, headers=patient_actor.headers)).json()
    assert browser_accepts(good)

    async with AsyncSession(admin_engine) as s, s.begin():
        await s.execute(text("ALTER TABLE audit_events DISABLE TRIGGER audit_events_append_only"))
        await s.execute(
            text("UPDATE audit_events SET payload_hash = :h WHERE seq = :seq"),
            {"h": bytes(32), "seq": good["audit_seq"]},
        )
        await s.execute(text("ALTER TABLE audit_events ENABLE TRIGGER audit_events_append_only"))
    for cached in (True, False):
        if not cached:
            tree_cache.clear()  # also when the tree is rebuilt from the tampered rows
        bad = (await client.get(url, headers=patient_actor.headers)).json()
        assert bad["payload_hash"] == "00" * 32
        assert not browser_accepts(bad)


def browser_accepts(r: dict[str, object]) -> bool:
    """packages/receipt-verify verifyReceipt, in Python: payload -> payload_hash -> leaf_hash,
    then the inclusion proof to the signed root."""
    payload_digest = hashlib.sha256(canonicalize(r["payload"])).digest()
    if payload_digest.hex() != r["payload_hash"]:
        return False
    if leaf_hash(payload_digest).hex() != r["leaf_hash"]:
        return False
    head = TreeHead.from_json(r["sth"])  # type: ignore[arg-type]
    return verify_inclusion(
        bytes.fromhex(str(r["leaf_hash"])),
        int(r["leaf_index"]),  # type: ignore[call-overload]
        head.tree_size,
        [bytes.fromhex(p) for p in r["proof"]],  # type: ignore[attr-defined]
        bytes.fromhex(head.root_hex),
    )


async def test_cached_tree_is_used_by_receipts_and_the_rows_counted(
    client: AsyncClient, admin_engine: AsyncEngine, clinic: uuid.UUID
) -> None:
    reception = await make_actor(admin_engine, clinic, Role.RECEPTION)
    patient_actor = await make_actor(admin_engine, clinic, Role.PATIENT)
    patient = await make_patient(admin_engine, clinic, reception.user_id, patient_actor.user_id)
    await client.get(f"/api/v1/patients/{patient}", headers=reception.headers)
    event_id = (
        await client.get(f"/api/v1/patients/{patient}/access-log", headers=patient_actor.headers)
    ).json()["items"][0]["id"]
    await build_checkpoint(clinic)
    tree_cache.clear()
    url = f"/api/v1/access-events/{event_id}/receipt"
    assert (await client.get(url, headers=patient_actor.headers)).status_code == 200
    fetched = tree_cache.stats["fetches"]
    assert fetched == 1
    assert (await client.get(url, headers=patient_actor.headers)).status_code == 200
    assert tree_cache.stats["fetches"] == fetched
