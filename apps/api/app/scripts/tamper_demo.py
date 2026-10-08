"""Show that rewriting the audit log is caught by the anchored root (local development only).

1. Creates a demo clinic with synthetic audit events, registers it on the local anvil chain, signs a
   checkpoint and anchors it.
2. Verifies the log against the anchored root: PASS.
3. As the database owner, changes one audit row and recomputes every hash after it, so the hash
   chain on its own still looks fine.
4. Verifies again: FAIL, because the rebuilt Merkle root no longer equals the root on chain. The
   anchor job also refuses to anchor the rewritten log.

    make contracts-local   # anvil with the contracts deployed
    make tamper-demo
"""

import asyncio
import json
import secrets
import sys
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.anchor import job
from app.anchor.evm import EvmAnchor, clinic_bytes32
from app.core.config import REPO_ROOT, get_settings
from app.core.db import TenantContext, get_engine, tenant_session
from app.db.enums import AnchorBackend, Role
from app.db.models import AuditEvent, Clinic
from app.ledger.checkpoint import (
    LedgerIntegrityError,
    build_checkpoint,
    clinic_signing_key,
    load_tree,
)
from app.ledger.writer import append_audit_event
from app.scripts.register_clinic import registration
from e2d_core.ledger import GENESIS, chain_hash, payload_hash

DEPLOYMENT = REPO_ROOT / "contracts" / "deployments" / "anvil.json"
ANVIL_CHAIN_ID = 31337
EVENTS = 6
TAMPERED_INDEX = 2


def say(step: str, message: str) -> None:
    print(f"[{step}] {message}", flush=True)


async def verify(evm: EvmAnchor, clinic_id: uuid.UUID) -> bool:
    size, root = await evm.latest(clinic_id)
    try:
        async with tenant_session(TenantContext(clinic_id=clinic_id, role="system")) as db:
            tree, _ = await load_tree(db, clinic_id)
    except LedgerIntegrityError as exc:
        say("verify", f"FAIL: {exc}")
        return False
    ok = size > 0 and tree.size >= size and tree.root(size) == root
    say("verify", f"anchored size {size}, root {root.hex()[:16]}...")
    say("verify", f"log root at that size {tree.root(size).hex()[:16]}...")
    say("verify", "PASS: the log matches the anchored root" if ok else "FAIL: the log was changed")
    return ok


async def setup(evm: EvmAnchor, admins: list[str]) -> uuid.UUID:
    async with tenant_session(TenantContext(role=Role.PLATFORM_ADMIN)) as db:
        clinic = Clinic(name=f"Tamper demo {secrets.token_hex(3)}", city="Demo", state="Demo")
        db.add(clinic)
        await db.flush()
        clinic_id = clinic.id
    ctx = TenantContext(clinic_id=clinic_id, role="system")
    for i in range(EVENTS):
        async with tenant_session(ctx) as db:
            await append_audit_event(
                db, clinic_id, "demo", {"type": "demo", "i": i, "note": f"synthetic event {i}"}
            )
    say("setup", f"clinic {clinic_id} with {EVENTS} audit events")

    # A throwaway poster wallet funded by anvil, registered by two unlocked anvil admins.
    poster = evm.w3.eth.account.create()
    await evm.w3.provider.make_request("anvil_setBalance", [poster.address, hex(10**20)])
    evm.account = poster
    reg = registration(clinic_id, poster.address)
    assert evm.registry is not None
    for admin in admins[:2]:
        tx = await evm.registry.functions.registerClinic(
            clinic_bytes32(clinic_id), bytes.fromhex(reg.key_hash[2:]), poster.address
        ).transact({"from": admin})
        await evm.w3.eth.wait_for_transaction_receipt(tx)
    say("setup", "clinic key and poster registered in ClinicRegistry")
    clinic_signing_key(clinic_id)  # fails early if LEDGER_SIGNING_SEED is missing
    return clinic_id


async def anchor(evm: EvmAnchor, clinic_id: uuid.UUID) -> str:
    cp = await build_checkpoint(clinic_id, db_role=job.JOB_ROLE)
    return await job.anchor_on_chain(evm, AnchorBackend.AMOY, cp)


async def tamper(clinic_id: uuid.UUID) -> None:
    """What a database administrator could do: edit a row and recompute the hash chain."""
    engine = create_async_engine(get_settings().migrator_database_url)
    try:
        async with AsyncSession(engine) as s:
            events = (
                await s.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.clinic_id == clinic_id)
                    .order_by(AuditEvent.seq)
                )
            ).all()
        prev = GENESIS
        async with engine.begin() as conn:
            await conn.execute(
                text("ALTER TABLE audit_events DISABLE TRIGGER audit_events_append_only")
            )
            for i, event in enumerate(events):
                payload = event.payload
                if i == TAMPERED_INDEX:
                    payload = {**payload, "note": "this event was rewritten"}
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
            await conn.execute(
                text("ALTER TABLE audit_events ENABLE TRIGGER audit_events_append_only")
            )
    finally:
        await engine.dispose()
    say("tamper", f"rewrote audit event {TAMPERED_INDEX} and recomputed every hash after it")


async def run() -> int:
    s = get_settings()
    if s.env not in ("local", "test"):
        print("tamper_demo only runs with APP_ENV=local or test")
        return 2
    try:
        deployment = json.loads(await asyncio.to_thread(DEPLOYMENT.read_text, encoding="utf-8"))
    except FileNotFoundError:
        print("No local deployment found. Run `make contracts-local` first.")
        return 2
    rpc = "http://127.0.0.1:8545"
    evm = EvmAnchor(rpc, deployment["AuditAnchor"], None, deployment["ClinicRegistry"])
    try:
        if await evm.chain_id() != ANVIL_CHAIN_ID:
            print("The chain at 127.0.0.1:8545 is not a local anvil chain.")
            return 2
        clinic_id = await setup(evm, deployment["admins"])
        say("anchor", f"first checkpoint: {await anchor(evm, clinic_id)}")
        before = await verify(evm, clinic_id)

        await tamper(clinic_id)
        after = await verify(evm, clinic_id)

        async with tenant_session(TenantContext(clinic_id=clinic_id, role="system")) as db:
            await append_audit_event(db, clinic_id, "demo", {"type": "demo", "i": EVENTS})
        try:
            result = await anchor(evm, clinic_id)
            say("anchor", f"next checkpoint: {result}")
        except LedgerIntegrityError as exc:
            say("anchor", f"refused: {exc}")
    finally:
        await evm.close()
        await get_engine().dispose()
    print()
    print(f"before tampering: {'PASS' if before else 'FAIL'}")
    print(f"after tampering:  {'PASS' if after else 'FAIL'}")
    return 0 if before and not after else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
