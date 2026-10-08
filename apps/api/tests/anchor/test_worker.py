from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app import worker
from app.anchor import job
from app.core.config import get_settings
from app.core.db import TenantContext, tenant_session
from app.db import models as m
from app.ledger.checkpoint import build_checkpoint
from app.ledger.writer import append_audit_event
from tests.helpers import make_clinic


async def test_overdue_follows_the_latest_checkpoint(admin_engine: AsyncEngine) -> None:
    clinic = await make_clinic(admin_engine)
    async with tenant_session(TenantContext(clinic_id=clinic, role="system")) as db:
        await append_audit_event(db, clinic, "test", {"i": 0})
    await build_checkpoint(clinic)
    # Other tests sign checkpoints with chosen times, so compare against the newest one there is.
    async with AsyncSession(admin_engine) as s:
        last = max(
            t
            for t in (
                await s.scalar(select(func.max(m.MerkleCheckpoint.signed_at))),
                await s.scalar(select(func.max(m.AnchorReceipt.submitted_at))),
            )
            if t is not None
        )
    hour = timedelta(hours=1)
    assert await worker.anchor_is_overdue(hour, now=last + 2 * hour) is True
    assert await worker.anchor_is_overdue(hour, now=last + hour) is True
    assert await worker.anchor_is_overdue(hour, now=last + hour / 2) is False


async def test_fallback_runs_only_when_enabled_and_overdue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    calls: list[str] = []

    async def fake_run(backends: job.Backends | None = None) -> list[job.ClinicRun]:
        calls.append("run")
        return []

    monkeypatch.setattr(job, "run", fake_run)
    monkeypatch.setattr(job, "backends_from_settings", lambda: job.Backends())

    monkeypatch.setattr(settings, "anchor_fallback_minutes", 0)
    assert await worker.anchor_fallback_tick() is None

    monkeypatch.setattr(settings, "anchor_fallback_minutes", 30)

    async def recent(after: timedelta) -> bool:
        return False

    monkeypatch.setattr(worker, "anchor_is_overdue", recent)
    assert await worker.anchor_fallback_tick() is None
    assert calls == []

    async def late(after: timedelta) -> bool:
        return True

    monkeypatch.setattr(worker, "anchor_is_overdue", late)
    assert await worker.anchor_fallback_tick() == []
    assert calls == ["run"]
