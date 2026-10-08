import asyncio
import json
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db import TenantContext, tenant_session
from app.db.models import AuditEvent, Clinic
from app.ledger.checkpoint import (
    LedgerIntegrityError,
    build_checkpoint,
    inclusion_proof,
    tree_head,
)
from app.ledger.writer import append_audit_event
from e2d_core.ledger import (
    GENESIS,
    ChainLink,
    chain_hash,
    payload_hash,
    verify,
    verify_chain,
    verify_inclusion,
)


async def append(clinic_id: uuid.UUID, n: int, kind: str = "test") -> None:
    ctx = TenantContext(clinic_id=clinic_id, role="system")
    for i in range(n):
        async with tenant_session(ctx) as db:
            await append_audit_event(db, clinic_id, kind, {"i": i, "note": "synthetic"})


async def chain(engine: AsyncEngine, clinic_id: uuid.UUID) -> list[AuditEvent]:
    async with AsyncSession(engine) as s:
        return list(
            (
                await s.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.clinic_id == clinic_id)
                    .order_by(AuditEvent.seq)
                )
            ).all()
        )


async def test_events_form_a_valid_chain(admin_engine: AsyncEngine) -> None:
    clinic_id = await fresh_clinic(admin_engine)
    await append(clinic_id, 4)
    events = await chain(admin_engine, clinic_id)
    assert len(events) == 4
    links = [ChainLink(e.seq, clinic_id, e.payload_hash, e.prev_hash, e.chain_hash) for e in events]
    assert verify_chain(links).ok
    assert all(e.payload_hash == payload_hash(e.payload) for e in events)


async def test_concurrent_writers_keep_the_chain_linear(admin_engine: AsyncEngine) -> None:
    clinic_id = await fresh_clinic(admin_engine)
    ctx = TenantContext(clinic_id=clinic_id, role="system")

    async def one(i: int) -> None:
        async with tenant_session(ctx) as db:
            await append_audit_event(db, clinic_id, "concurrent", {"i": i})

    await asyncio.gather(*(one(i) for i in range(12)))
    events = await chain(admin_engine, clinic_id)
    links = [ChainLink(e.seq, clinic_id, e.payload_hash, e.prev_hash, e.chain_hash) for e in events]
    check = verify_chain(links)
    assert check.ok
    assert check.checked == 12


async def fresh_clinic(engine: AsyncEngine) -> uuid.UUID:
    """A clinic with no audit events at all, so its chain starts at the genesis hash."""
    async with AsyncSession(engine) as s, s.begin():
        clinic = Clinic(name=f"Ledger {uuid.uuid4().hex[:6]}", city="X", state="Y")
        s.add(clinic)
        await s.flush()
        return clinic.id


async def test_first_event_links_to_genesis(admin_engine: AsyncEngine) -> None:
    clinic_id = await fresh_clinic(admin_engine)
    await append(clinic_id, 1)
    (event,) = await chain(admin_engine, clinic_id)
    assert event.prev_hash == GENESIS
    assert event.chain_hash == chain_hash(GENESIS, event.payload_hash, event.seq, clinic_id)


async def test_checkpoints_are_signed_and_linked(admin_engine: AsyncEngine) -> None:
    clinic_id = await fresh_clinic(admin_engine)
    await append(clinic_id, 3)
    first = await build_checkpoint(clinic_id)
    assert first.created
    assert first.head.tree_size == 3
    assert first.head.prev_root_hex is None

    again = await build_checkpoint(clinic_id)
    assert not again.created
    assert again.checkpoint.id == first.checkpoint.id

    await append(clinic_id, 2)
    second = await build_checkpoint(clinic_id)
    assert second.created
    assert second.head.tree_size == 5
    assert second.head.prev_root_hex == first.head.root_hex

    async with AsyncSession(admin_engine) as s:
        pubkey = await s.scalar(select(Clinic.signer_pubkey).where(Clinic.id == clinic_id))
    assert pubkey is not None
    assert verify(second.head, second.checkpoint.signature, pubkey)
    assert tree_head(clinic_id, second.checkpoint) == second.head


async def test_inclusion_proofs_verify_against_signed_head(admin_engine: AsyncEngine) -> None:
    clinic_id = await fresh_clinic(admin_engine)
    await append(clinic_id, 7)
    result = await build_checkpoint(clinic_id)
    root = bytes.fromhex(result.head.root_hex)
    for event in await chain(admin_engine, clinic_id):
        p = await inclusion_proof(clinic_id, event.seq)
        assert verify_inclusion(p.leaf_hash, p.leaf_index, p.head.tree_size, p.proof, root)

    await append(clinic_id, 1)
    newest = (await chain(admin_engine, clinic_id))[-1]
    with pytest.raises(LookupError, match="newer"):
        await inclusion_proof(clinic_id, newest.seq)


async def test_edited_payload_blocks_the_next_checkpoint(admin_engine: AsyncEngine) -> None:
    clinic_id = await fresh_clinic(admin_engine)
    await append(clinic_id, 4)
    await build_checkpoint(clinic_id)
    victim = (await chain(admin_engine, clinic_id))[1]

    async with admin_engine.begin() as conn:
        await conn.execute(
            text("ALTER TABLE audit_events DISABLE TRIGGER audit_events_append_only")
        )
        await conn.execute(
            text("UPDATE audit_events SET payload = '{\"i\": 99}' WHERE seq = :s"),
            {"s": victim.seq},
        )
        await conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER audit_events_append_only"))

    await append(clinic_id, 1)
    with pytest.raises(LedgerIntegrityError, match="payload"):
        await build_checkpoint(clinic_id)


async def test_recomputed_chain_is_caught_by_the_previous_checkpoint(
    admin_engine: AsyncEngine,
) -> None:
    """A DBA edits a row and recomputes every hash after it. The chain checks out, but the old root
    no longer matches, so no new checkpoint is signed."""
    clinic_id = await fresh_clinic(admin_engine)
    await append(clinic_id, 4)
    await build_checkpoint(clinic_id)

    events = await chain(admin_engine, clinic_id)
    prev = GENESIS
    async with admin_engine.begin() as conn:
        await conn.execute(
            text("ALTER TABLE audit_events DISABLE TRIGGER audit_events_append_only")
        )
        for i, event in enumerate(events):
            payload = {"i": 42, "note": "rewritten"} if i == 1 else event.payload
            digest = payload_hash(payload)
            new_hash = chain_hash(prev, digest, event.seq, clinic_id)
            await conn.execute(
                text(
                    "UPDATE audit_events SET payload = CAST(:p AS jsonb), payload_hash = :d,"
                    " prev_hash = :prev, chain_hash = :h WHERE seq = :s"
                ),
                {
                    "p": json.dumps(payload),
                    "d": digest,
                    "prev": prev,
                    "h": new_hash,
                    "s": event.seq,
                },
            )
            prev = new_hash
        await conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER audit_events_append_only"))

    await append(clinic_id, 1)
    with pytest.raises(LedgerIntegrityError, match="changed since the last checkpoint"):
        await build_checkpoint(clinic_id)
