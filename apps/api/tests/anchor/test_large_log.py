"""A large log (ADR 0011): with 100,000 audit events in one clinic, the integrity endpoint, a
patient receipt and the admin audit status answer correctly, and the API keeps answering health
checks within a second while they run (the full re-hash runs in a worker thread, proofs come from
stored leaf hashes). Timings are printed (run with -s); the CHANGELOG cites them."""

import asyncio
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from httpx import AsyncClient
from sqlalchemy import Text, cast, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.anchor.receipts import clear_verified
from app.core.db import TenantContext, tenant_session
from app.db import models as m
from app.db.enums import AnchorBackend, AnchorStatus, Role
from app.ledger import tree_cache
from app.ledger.checkpoint import _verify_rows, build_checkpoint
from app.ledger.writer import append_audit_events
from tests.helpers import make_actor, make_clinic, make_patient

EVENTS = 100_000
BATCH = 5_000


def synthetic(i: int) -> dict[str, Any]:
    return {
        "type": "access",
        "access_event_id": str(uuid.UUID(int=i)),
        "at": "2026-11-20T04:30:00.123456+00:00",
        "user_id": "01920000-0000-7000-8000-0000000000b2",
        "role": "doctor",
        "patient_id": "01920000-0000-7000-8000-0000000000a1",
        "resource": "vitals",
        "action": "view",
        "decision": "allow",
        "policy_version": "0" * 64,
        "template": "T_APPT",
        "strength": 0.875,
    }


async def timed_while_probing(client: AsyncClient, call: Any) -> tuple[Any, float, float]:
    """Run `call` while GET /health is sent every 50 ms. Returns (response, seconds, slowest
    health answer in seconds)."""
    done = asyncio.Event()
    slowest = 0.0

    async def probe() -> None:
        nonlocal slowest
        while not done.is_set():
            t = time.perf_counter()
            r = await client.get("/api/v1/health")
            assert r.status_code == 200
            slowest = max(slowest, time.perf_counter() - t)
            await asyncio.sleep(0.05)

    task = asyncio.create_task(probe())
    start = time.perf_counter()
    try:
        response = await call()
    finally:
        elapsed = time.perf_counter() - start
        done.set()
        await task
    return response, elapsed, slowest


async def test_a_100000_event_log_does_not_block_the_api(
    client: AsyncClient, admin_engine: AsyncEngine
) -> None:
    clinic = await make_clinic(admin_engine)
    reception = await make_actor(admin_engine, clinic, Role.RECEPTION)
    nurse = await make_actor(admin_engine, clinic, Role.NURSE)
    admin = await make_actor(admin_engine, clinic, Role.CLINIC_ADMIN)
    patient_actor = await make_actor(admin_engine, clinic, Role.PATIENT)
    patient = await make_patient(admin_engine, clinic, reception.user_id, patient_actor.user_id)

    ctx = TenantContext(clinic_id=clinic, role="system")
    for start in range(0, EVENTS, BATCH):
        async with tenant_session(ctx) as db:
            await append_audit_events(
                db, clinic, "access", [synthetic(i) for i in range(start, start + BATCH)]
            )
    vital = await client.post(
        f"/api/v1/patients/{patient}/vitals", json={"pulse": 72}, headers=nurse.headers
    )
    assert vital.status_code == 201, vital.text
    await client.get(f"/api/v1/patients/{patient}", headers=reception.headers)
    event_id = (
        await client.get(f"/api/v1/patients/{patient}/access-log", headers=patient_actor.headers)
    ).json()["items"][0]["id"]
    cp = await build_checkpoint(clinic)
    async with AsyncSession(admin_engine) as s, s.begin():
        s.add(
            m.AnchorReceipt(
                clinic_id=clinic,
                checkpoint_id=cp.checkpoint.id,
                backend=AnchorBackend.AMOY,
                status=AnchorStatus.CONFIRMED,
                tx_ref="0x" + "ef" * 32,
                submitted_at=datetime.now(UTC),
            )
        )
    assert cp.head.tree_size > EVENTS

    # What the old request path cost: the full re-hash, which used to run inside the event loop.
    async with tenant_session(ctx) as db:
        rows = (
            await db.execute(
                select(
                    m.AuditEvent.seq,
                    cast(m.AuditEvent.payload, Text),
                    m.AuditEvent.payload_hash,
                    m.AuditEvent.prev_hash,
                    m.AuditEvent.chain_hash,
                )
                .where(m.AuditEvent.clinic_id == clinic)
                .order_by(m.AuditEvent.seq)
            )
        ).all()
    plain = [(int(r[0]), str(r[1]), bytes(r[2]), bytes(r[3]), bytes(r[4])) for r in rows]
    t = time.perf_counter()
    _verify_rows(clinic, plain)
    rehash = time.perf_counter() - t

    tree_cache.clear()
    clear_verified()
    results: dict[str, tuple[float, float]] = {}

    r, took, slowest = await timed_while_probing(
        client,
        lambda: client.get(
            f"/api/v1/records/vital/{vital.json()['id']}/integrity", headers=nurse.headers
        ),
    )
    assert r.status_code == 200 and r.json()["verdict"] == "match", r.text
    results["integrity (cold cache)"] = (took, slowest)

    r, took, slowest = await timed_while_probing(
        client,
        lambda: client.get(
            f"/api/v1/access-events/{event_id}/receipt", headers=patient_actor.headers
        ),
    )
    assert r.status_code == 200, r.text
    results["receipt (warm cache)"] = (took, slowest)

    r, took, slowest = await timed_while_probing(
        client, lambda: client.get("/api/v1/audit/status", headers=admin.headers)
    )
    assert r.status_code == 200 and r.json()["chain_ok"] is True and r.json()["consistency_ok"]
    assert r.json()["events"] >= cp.head.tree_size  # the checks above add access events
    results["audit status (full check)"] = (took, slowest)

    r, took, slowest = await timed_while_probing(
        client, lambda: client.get("/api/v1/audit/status", headers=admin.headers)
    )
    assert r.status_code == 200 and r.json()["chain_ok"] is True
    results["audit status (unchanged log, cached)"] = (took, slowest)

    print(
        f"\n{cp.head.tree_size} events; full re-hash of the log (old request path): {rehash:.2f} s"
    )
    for name, (took, slow) in results.items():
        print(f"{name}: {took:.2f} s, slowest concurrent health answer {slow:.3f} s")
    for name, (_, slow) in results.items():
        assert slow < 1.0, f"{name}: health answered in {slow:.3f} s"
