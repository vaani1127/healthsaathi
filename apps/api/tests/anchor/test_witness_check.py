import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from app.anchor import job, witness_check
from app.anchor.local_witness import LocalWitness
from app.core.config import get_settings
from app.core.db import TenantContext, tenant_session
from app.db.enums import Role
from app.ledger.checkpoint import build_checkpoint, load_tree
from app.ledger.writer import append_audit_event
from app.scripts import tamper_demo, tamper_study
from tests.helpers import make_actor, make_clinic


@pytest.fixture(autouse=True)
def fresh_cache() -> Iterator[None]:
    witness_check._cache.clear()
    yield
    witness_check._cache.clear()


@pytest.fixture
def no_anchors(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "anchor_rpc_url", None)
    monkeypatch.setattr(settings, "ots_enabled", False)
    monkeypatch.setattr(settings, "witness_dir", None)
    monkeypatch.setattr(settings, "witness_repo", None)


async def add_events(clinic_id: uuid.UUID, n: int) -> None:
    for i in range(n):
        async with tenant_session(TenantContext(clinic_id=clinic_id, role="system")) as db:
            await append_audit_event(db, clinic_id, "test", {"i": i})


async def publish_round(clinic_id: uuid.UUID, witness: LocalWitness, events: int) -> None:
    await add_events(clinic_id, events)
    cp = await build_checkpoint(clinic_id, db_role=job.JOB_ROLE)
    await job.publish_witness(witness, cp)


async def test_audit_page_catches_a_rewrite_with_the_checkpoints_deleted(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_anchors: None,
) -> None:
    clinic_id = await make_clinic(admin_engine)
    witness = LocalWitness(tmp_path / "witness")
    for _ in range(2):
        await publish_round(clinic_id, witness, 4)
    monkeypatch.setattr(get_settings(), "witness_dir", str(witness.folder))
    admin = await make_actor(admin_engine, clinic_id, Role.CLINIC_ADMIN)

    before = (await client.get("/api/v1/audit/status", headers=admin.headers)).json()
    assert before["chain_ok"] is True and before["consistency_ok"] is True
    assert before["witness_ok"] is True and before["witness_size"] == 8
    assert before["problem"] is None

    # The attack: rewrite a row, recompute every hash after it, delete the stored checkpoints.
    await tamper_demo.tamper(clinic_id)
    await tamper_study.delete_stored_checkpoints(clinic_id)

    after = (await client.get("/api/v1/audit/status", headers=admin.headers)).json()
    # The chain looks fine and no stored checkpoint is left to compare with...
    assert after["chain_ok"] is True
    assert after["consistency_ok"] is None and after["sth"] is None
    # ...but the head on the witness no longer matches, so the page does not show a plain pass.
    assert after["witness_ok"] is False and after["witness_size"] == 8
    assert "witness" in after["problem"]

    other = await make_actor(admin_engine, await make_clinic(admin_engine), Role.CLINIC_ADMIN)
    theirs = (await client.get("/api/v1/audit/status", headers=other.headers)).json()
    assert theirs["witness_ok"] is None and theirs["problem"] is None, (
        "another clinic has nothing published and does not see this clinic's result"
    )


async def test_a_rewrite_alone_is_reported_by_both_checks(
    client: AsyncClient,
    admin_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_anchors: None,
) -> None:
    clinic_id = await make_clinic(admin_engine)
    witness = LocalWitness(tmp_path / "witness")
    await publish_round(clinic_id, witness, 4)
    monkeypatch.setattr(get_settings(), "witness_dir", str(witness.folder))
    admin = await make_actor(admin_engine, clinic_id, Role.CLINIC_ADMIN)
    await tamper_demo.tamper(clinic_id)
    body = (await client.get("/api/v1/audit/status", headers=admin.headers)).json()
    assert body["consistency_ok"] is False and body["witness_ok"] is False
    assert body["problem"] == "the log no longer matches the latest checkpoint"


async def test_no_witness_configured_is_not_a_result(
    client: AsyncClient, admin_engine: AsyncEngine, no_anchors: None
) -> None:
    clinic_id = await make_clinic(admin_engine)
    await add_events(clinic_id, 2)
    admin = await make_actor(admin_engine, clinic_id, Role.CLINIC_ADMIN)
    body = (await client.get("/api/v1/audit/status", headers=admin.headers)).json()
    assert body["witness_ok"] is None and body["witness_size"] is None


class FakeGitHub:
    """The contents listing and raw files of a witness repository."""

    def __init__(self, files: dict[int, dict[str, object]], status: int = 200) -> None:
        self.files = files
        self.status = status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status)
        if request.url.host == "api.github.com":
            listing = [
                {"name": f"{size}.json", "download_url": f"https://raw.test/{size}.json"}
                for size in self.files
            ]
            return httpx.Response(200, json=[*listing, {"name": "README.md", "download_url": ""}])
        size = int(request.url.path.strip("/").removesuffix(".json"))
        return httpx.Response(200, json=self.files[size])


async def published_documents(
    clinic_id: uuid.UUID, tmp_path: Path, rounds: int
) -> dict[int, dict[str, object]]:
    witness = LocalWitness(tmp_path / "witness")
    for _ in range(rounds):
        await publish_round(clinic_id, witness, 3)
    folder = witness.folder / "witness" / str(clinic_id)
    return {int(f.stem): json.loads(f.read_text(encoding="utf-8")) for f in folder.glob("*.json")}


async def test_github_witness_checks_the_newest_head_and_caches_it(
    admin_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_anchors: None,
) -> None:
    clinic_id = await make_clinic(admin_engine)
    files = await published_documents(clinic_id, tmp_path, 3)
    assert sorted(files) == [3, 6, 9]
    settings = get_settings()
    monkeypatch.setattr(settings, "witness_repo", "demo/witness")
    monkeypatch.setattr(settings, "witness_github_token", SecretStr("read-token"))
    github = FakeGitHub(files)
    client = httpx.AsyncClient(transport=httpx.MockTransport(github.handler))
    async with tenant_session(TenantContext(clinic_id=clinic_id, role="system")) as db:
        tree, _ = await load_tree(db, clinic_id)

    result = await witness_check.check_newest(tree, clinic_id, client=client)
    assert result == witness_check.WitnessResult(True, 9, None)
    listing, raw = github.requests
    assert listing.url.path == f"/repos/demo/witness/contents/witness/{clinic_id}"
    assert listing.headers["Authorization"] == "Bearer read-token"
    assert raw.url.path == "/9.json"

    # The page refreshes every 30 seconds and published heads never change: no second request.
    await witness_check.check_newest(tree, clinic_id, client=client)
    assert len(github.requests) == 2
    await client.aclose()


async def test_github_problems_skip_the_check_instead_of_failing_it(
    admin_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, no_anchors: None
) -> None:
    clinic_id = await make_clinic(admin_engine)
    await add_events(clinic_id, 1)
    monkeypatch.setattr(get_settings(), "witness_repo", "demo/witness")
    async with tenant_session(TenantContext(clinic_id=clinic_id, role="system")) as db:
        tree, _ = await load_tree(db, clinic_id)

    for status, expected in (
        (404, witness_check.WitnessResult(None)),
        (503, witness_check.WitnessResult(None, reason="the witness could not be read")),
    ):
        witness_check._cache.clear()
        client = httpx.AsyncClient(transport=httpx.MockTransport(FakeGitHub({}, status).handler))
        assert await witness_check.check_newest(tree, clinic_id, client=client) == expected
        await client.aclose()


def test_newest_in_folder_ignores_other_files(tmp_path: Path) -> None:
    clinic_id = uuid.uuid4()
    folder = tmp_path / "witness" / str(clinic_id)
    folder.mkdir(parents=True)
    (folder / "notes.json").write_text("{}", encoding="utf-8")
    assert witness_check.newest_in_folder(tmp_path, clinic_id) is None
    assert witness_check.newest_in_folder(tmp_path, uuid.uuid4()) is None
