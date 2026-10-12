import json
import subprocess
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.anchor import job
from app.anchor.local_witness import LocalWitness
from app.core.config import get_settings
from app.core.db import TenantContext, tenant_session
from app.db import models as m
from app.db.enums import AnchorBackend, AnchorStatus, Role
from app.ledger.writer import append_audit_event
from tests.helpers import make_actor, make_clinic


def commits(folder: Path) -> int:
    out = subprocess.run(  # noqa: S603 - fixed git arguments
        ["git", "-c", "safe.directory=*", "-C", str(folder), "rev-list", "--count", "HEAD"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return int(out.stdout.strip())


async def test_publish_commits_once_per_tree_head(tmp_path: Path) -> None:
    witness = LocalWitness(tmp_path / "witness")
    doc = {"sth": {"tree_size": 3}, "signature": "ab"}
    first = await witness.publish("clinic-1", 3, doc)
    again = await witness.publish("clinic-1", 3, doc)
    assert first == again and len(first) == 40
    assert commits(witness.folder) == 1
    saved = json.loads((witness.folder / "witness/clinic-1/3.json").read_text(encoding="utf-8"))
    assert saved == doc

    await witness.publish("clinic-1", 5, {"sth": {"tree_size": 5}})
    assert commits(witness.folder) == 2


def test_anvil_is_stored_as_a_self_hosted_chain() -> None:
    assert job.chain_backend("anvil") == AnchorBackend.BESU
    assert job.chain_backend("amoy") == AnchorBackend.AMOY


def test_witness_dir_wins_over_github(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "witness_dir", str(tmp_path))
    monkeypatch.setattr(settings, "anchor_rpc_url", None)
    assert isinstance(job.backends_from_settings().github, LocalWitness)
    monkeypatch.setattr(settings, "witness_dir", None)
    monkeypatch.setattr(settings, "witness_repo", None)
    assert job.backends_from_settings().github is None


async def test_job_publishes_to_the_folder_and_status_shows_every_backend(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clinic_id = await make_clinic(admin_engine)
    ctx = TenantContext(clinic_id=clinic_id, role="system")
    for i in range(3):
        async with tenant_session(ctx) as db:
            await append_audit_event(db, clinic_id, "test", {"i": i})
    folder = tmp_path / "witness"
    await job.run(job.Backends(github=LocalWitness(folder)))

    async with AsyncSession(admin_engine) as s:
        row = await s.scalar(
            select(m.AnchorReceipt).where(
                m.AnchorReceipt.clinic_id == clinic_id,
                m.AnchorReceipt.backend == AnchorBackend.GITHUB,
            )
        )
    assert row is not None and row.status == AnchorStatus.CONFIRMED
    assert (folder / f"witness/{clinic_id}/3.json").exists()

    settings = get_settings()
    monkeypatch.setattr(settings, "witness_dir", str(folder))
    monkeypatch.setattr(settings, "anchor_rpc_url", None)
    monkeypatch.setattr(settings, "anchor_chain", "anvil")
    monkeypatch.setattr(settings, "ots_enabled", False)
    admin = await make_actor(admin_engine, clinic_id, Role.CLINIC_ADMIN)
    body = (await client.get("/api/v1/audit/status", headers=admin.headers)).json()
    states = {b["key"]: b for b in body["backends"]}
    assert states["chain"] == {
        "key": "chain",
        "kind": "anvil",
        "configured": False,
        "status": None,
        "tx_ref": None,
        "block_ref": None,
        "link": None,
        "last_confirmed_at": None,
        "last_confirmed_size": None,
    }
    assert states["witness"]["kind"] == "local"
    assert states["witness"]["configured"] is True
    assert states["witness"]["status"] == "confirmed"
    assert states["witness"]["tx_ref"] == row.tx_ref
    assert states["witness"]["link"] is None, "a local folder has no web link"
    assert states["witness"]["last_confirmed_size"] == 3
    assert states["ots"]["configured"] is False

    other = await make_actor(admin_engine, await make_clinic(admin_engine), Role.CLINIC_ADMIN)
    theirs = (await client.get("/api/v1/audit/status", headers=other.headers)).json()
    assert {b["key"]: b["status"] for b in theirs["backends"]} == {
        "chain": None,
        "witness": None,
        "ots": None,
    }, "another clinic does not see this clinic's anchors"
