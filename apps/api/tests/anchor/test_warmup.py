"""Startup warm-up (app.anchor.warmup) and the append-only extension of the audit status check
(app.anchor.receipts.verified_tree), ADR 0011."""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.anchor import receipts, warmup
from app.core.db import TenantContext, tenant_session
from app.db import models as m
from app.db.enums import AnchorBackend, AnchorStatus, Role
from app.ledger import tree_cache
from app.ledger.checkpoint import build_checkpoint, load_tree
from app.ledger.writer import append_audit_events
from app.main import create_app
from tests.helpers import make_actor, make_clinic, make_patient


async def fill(clinic_id: uuid.UUID, n: int, start: int = 0) -> None:
    async with tenant_session(TenantContext(clinic_id=clinic_id, role="system")) as db:
        await append_audit_events(
            db, clinic_id, "test", [{"type": "test", "i": i} for i in range(start, start + n)]
        )


async def verified(clinic_id: uuid.UUID) -> tuple[object, str | None, int]:
    async with tenant_session(TenantContext(clinic_id=clinic_id, role="system")) as db:
        return await receipts.verified_tree(db, clinic_id)


async def tamper(admin_engine: AsyncEngine, clinic_id: uuid.UUID, offset: int) -> None:
    """The database owner changes the payload of the clinic's event at position `offset`."""
    async with AsyncSession(admin_engine) as s, s.begin():
        await s.execute(text("ALTER TABLE audit_events DISABLE TRIGGER audit_events_append_only"))
        await s.execute(
            text(
                "UPDATE audit_events SET payload = jsonb_set(payload, '{i}', '999')"
                " WHERE seq = (SELECT seq FROM audit_events WHERE clinic_id = :c"
                " ORDER BY seq OFFSET :o LIMIT 1)"
            ),
            {"c": clinic_id, "o": offset},
        )
        await s.execute(text("ALTER TABLE audit_events ENABLE TRIGGER audit_events_append_only"))


@pytest.fixture(autouse=True)
def empty_caches() -> None:
    tree_cache.clear()
    receipts.clear_verified()


async def test_warmup_fills_both_caches(admin_engine: AsyncEngine) -> None:
    clinic = await make_clinic(admin_engine)
    await fill(clinic, 30)
    await build_checkpoint(clinic)
    await fill(clinic, 5, start=30)  # after the checkpoint
    tree_cache.clear()
    receipts.clear_verified()

    seconds = await warmup.warm_caches([clinic])

    assert seconds[str(clinic)] is not None
    assert tree_cache._entries[clinic].size == 35
    cached = receipts._verified[clinic]
    assert cached.chain_ok and cached.log is not None and cached.log.entry.size == 35
    assert receipts.check_stats == {"full": 1, "appended": 0}


async def test_requests_after_warmup_fetch_no_leaves_and_run_no_full_check(
    client: AsyncClient, admin_engine: AsyncEngine
) -> None:
    clinic = await make_clinic(admin_engine)
    reception = await make_actor(admin_engine, clinic, Role.RECEPTION)
    nurse = await make_actor(admin_engine, clinic, Role.NURSE)
    admin = await make_actor(admin_engine, clinic, Role.CLINIC_ADMIN)
    patient_actor = await make_actor(admin_engine, clinic, Role.PATIENT)
    patient = await make_patient(admin_engine, clinic, reception.user_id, patient_actor.user_id)
    vital = await client.post(
        f"/api/v1/patients/{patient}/vitals", json={"pulse": 70}, headers=nurse.headers
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
                tx_ref="0x" + "ab" * 32,
                submitted_at=datetime.now(UTC),
            )
        )
    tree_cache.clear()
    receipts.clear_verified()

    await warmup.warm_caches([clinic])
    tree_cache.stats.update(fetches=0, rows=0)
    receipts.check_stats.update(full=0, appended=0)

    r = await client.get("/api/v1/audit/status", headers=admin.headers)
    assert r.status_code == 200 and r.json()["chain_ok"] is True
    assert receipts.check_stats == {"full": 0, "appended": 0}

    r = await client.get(
        f"/api/v1/records/vital/{vital.json()['id']}/integrity", headers=nurse.headers
    )
    assert r.status_code == 200 and r.json()["verdict"] == "match", r.text
    r = await client.get(f"/api/v1/access-events/{event_id}/receipt", headers=patient_actor.headers)
    assert r.status_code == 200, r.text
    assert tree_cache.stats["fetches"] == 0

    # Those requests appended access events: the next status checks only the new rows.
    r = await client.get("/api/v1/audit/status", headers=admin.headers)
    assert r.status_code == 200 and r.json()["chain_ok"] is True
    assert receipts.check_stats == {"full": 0, "appended": 1}


async def test_appended_rows_are_checked_alone_and_match_the_full_check(
    admin_engine: AsyncEngine,
) -> None:
    clinic = await make_clinic(admin_engine)
    await fill(clinic, 10)
    await verified(clinic)
    await fill(clinic, 7, start=10)
    tree, problem, events = await verified(clinic)
    assert problem is None and events == 17
    assert receipts.check_stats == {"full": 1, "appended": 1}
    async with tenant_session(TenantContext(clinic_id=clinic, role="system")) as db:
        full, _ = await load_tree(db, clinic)
    assert tree.root() == full.root()  # type: ignore[attr-defined]


async def test_a_changed_appended_row_fails_the_appended_check(admin_engine: AsyncEngine) -> None:
    clinic = await make_clinic(admin_engine)
    await fill(clinic, 10)
    await verified(clinic)
    await fill(clinic, 5, start=10)
    await tamper(admin_engine, clinic, 12)
    tree, problem, _ = await verified(clinic)
    assert tree is None and problem is not None and "was changed" in problem
    assert receipts.check_stats == {"full": 1, "appended": 1}


async def test_an_edit_to_a_verified_row_forces_the_full_check(admin_engine: AsyncEngine) -> None:
    clinic = await make_clinic(admin_engine)
    await fill(clinic, 10)
    await verified(clinic)
    await tamper(admin_engine, clinic, 3)
    await fill(clinic, 2, start=10)  # appending as well does not hide the edit
    tree, problem, _ = await verified(clinic)
    assert tree is None and problem is not None and "was changed" in problem
    assert receipts.check_stats == {"full": 2, "appended": 0}


async def test_a_warmup_error_does_not_stop_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(_: uuid.UUID) -> None:
        raise RuntimeError("warm-up broke")

    monkeypatch.setattr(warmup, "enabled_in_tests", True)
    monkeypatch.setattr(warmup, "warm_clinic", broken)
    app = create_app()
    async with app.router.lifespan_context(app):
        assert warmup.task is not None
        seconds = await asyncio.wait_for(warmup.task, 10)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            assert (await ac.get("/api/v1/ready")).status_code == 200
    assert seconds and all(v is None for v in seconds.values())


async def test_ready_answers_while_warmup_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(_: uuid.UUID) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(warmup, "enabled_in_tests", True)
    monkeypatch.setattr(warmup, "warm_clinic", slow)
    app = create_app()
    transport = ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://test") as ac,
    ):
        await asyncio.wait_for(started.wait(), 10)
        r = await ac.get("/api/v1/ready")
        assert r.status_code == 200 and r.json()["status"] == "ok"
        assert warmup.task is not None and not warmup.task.done()
        release.set()
        seconds = await asyncio.wait_for(warmup.task, 10)
    assert seconds and all(v is not None for v in seconds.values())


def test_warmup_is_skipped_in_tests_unless_turned_on() -> None:
    assert warmup.start_warmup() is None
