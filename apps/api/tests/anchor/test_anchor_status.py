import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from app.anchor import job
from app.anchor.local_witness import LocalWitness
from app.core.db import TenantContext, tenant_session
from app.ledger.checkpoint import build_checkpoint
from app.ledger.writer import append_audit_event
from app.scripts import anchor_status
from tests.helpers import make_clinic


async def test_status_reports_minutes_since_anchor_and_24h_counts(
    admin_engine: AsyncEngine, tmp_path: Path
) -> None:
    anchored = await make_clinic(admin_engine)
    quiet = await make_clinic(admin_engine)
    async with tenant_session(TenantContext(clinic_id=anchored, role="system")) as db:
        await append_audit_event(db, anchored, "test", {"i": 0})
    cp = await build_checkpoint(anchored, db_role=job.JOB_ROLE)
    await job.publish_witness(LocalWitness(tmp_path / "witness"), cp)

    report = await anchor_status.collect()
    mine = report["clinics"][str(anchored)]
    assert mine["anchors_24h"] == 1
    assert mine["backends"]["github"]["anchors_24h"] == 1
    assert 0 <= mine["minutes_since_last_anchor"] < 5
    assert report["clinics"][str(quiet)] == {
        "name": report["clinics"][str(quiet)]["name"],
        "minutes_since_last_anchor": None,
        "anchors_24h": 0,
        "backends": {},
    }


def test_summary_takes_the_newest_backend_and_adds_the_counts() -> None:
    now = datetime(2026, 11, 11, 6, 0, tzinfo=UTC)
    clinic = uuid.uuid4()
    rows = [
        (clinic, "A", "amoy", now - timedelta(minutes=20), 3),
        (clinic, "A", "ots", now - timedelta(minutes=50), 1),
    ]
    out = anchor_status.summary(now, rows)["clinics"][str(clinic)]
    assert out["minutes_since_last_anchor"] == 20.0
    assert out["anchors_24h"] == 4
