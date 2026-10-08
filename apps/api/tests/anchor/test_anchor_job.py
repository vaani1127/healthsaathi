import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from opentimestamps.core.notary import BitcoinBlockHeaderAttestation, PendingAttestation
from opentimestamps.core.timestamp import Timestamp
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.anchor import job
from app.anchor.evm import EvmAnchor, clinic_bytes32
from app.anchor.github import GitHubWitness
from app.anchor.ots import OtsStamper
from app.core.db import TenantContext, tenant_session
from app.db import models as m
from app.db.enums import AnchorBackend, AnchorStatus
from app.ledger.checkpoint import clinic_signing_key
from app.ledger.writer import append_audit_event
from app.scripts import register_clinic
from e2d_core.ledger import GENESIS, chain_hash, payload_hash, public_key_bytes
from tests.anchor.chain import (
    Chain,
    anvil_available,
    create_contract,
    deploy,
    funded_wallet,
    register,
    start_anvil,
)

pytestmark = pytest.mark.skipif(not anvil_available(), reason="anvil and forge are not installed")


@pytest.fixture(scope="module")
def anvil() -> Iterator[Chain]:
    chain = start_anvil()
    yield chain
    chain.stop()


@pytest.fixture(scope="module")
async def deployed(anvil: Chain) -> Chain:
    await deploy(anvil)
    return anvil


class FakeCalendar:
    def __init__(self, url: str, ready: bool = False) -> None:
        self.url = url
        self.ready = ready
        self.submitted: list[bytes] = []

    def submit(self, digest: bytes, timeout: float | None = None) -> Timestamp:
        self.submitted.append(digest)
        stamp = Timestamp(digest)
        stamp.attestations.add(PendingAttestation(self.url))
        return stamp

    def get_timestamp(self, commitment: bytes, timeout: float | None = None) -> Timestamp:
        if not self.ready:
            raise RuntimeError("not ready")
        stamp = Timestamp(commitment)
        stamp.attestations.add(BitcoinBlockHeaderAttestation(900_000))
        return stamp


class FakeGitHub:
    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "PUT":
            if path in self.files:
                return httpx.Response(422)
            body = json.loads(request.content)
            import base64

            self.files[path] = json.loads(base64.b64decode(body["content"]))
            return httpx.Response(201, json={"commit": {"sha": f"sha{len(self.files)}"}})
        return httpx.Response(200, json={"sha": "existing"})


async def new_clinic(engine: AsyncEngine) -> uuid.UUID:
    async with AsyncSession(engine) as s, s.begin():
        clinic = m.Clinic(name=f"Anchor {uuid.uuid4().hex[:6]}", city="X", state="Y")
        s.add(clinic)
        await s.flush()
        return clinic.id


async def add_events(clinic_id: uuid.UUID, n: int) -> None:
    ctx = TenantContext(clinic_id=clinic_id, role="system")
    for i in range(n):
        async with tenant_session(ctx) as db:
            await append_audit_event(db, clinic_id, "test", {"i": i})


async def receipts(engine: AsyncEngine, clinic_id: uuid.UUID) -> dict[str, list[m.AnchorReceipt]]:
    async with AsyncSession(engine) as s:
        rows = (
            await s.scalars(select(m.AnchorReceipt).where(m.AnchorReceipt.clinic_id == clinic_id))
        ).all()
    out: dict[str, list[m.AnchorReceipt]] = {}
    for r in rows:
        out.setdefault(r.backend.value, []).append(r)
    return out


@pytest.fixture
async def clinic(admin_engine: AsyncEngine, deployed: Chain) -> AsyncIterator[uuid.UUID]:
    clinic_id = await new_clinic(admin_engine)
    key_hash = hashlib.sha256(public_key_bytes(clinic_signing_key(clinic_id))).digest()
    await register(deployed, clinic_bytes32(clinic_id), key_hash)
    yield clinic_id


@pytest.fixture(autouse=True)
async def close_providers() -> AsyncIterator[None]:
    made: list[EvmAnchor] = []
    original = EvmAnchor.__init__

    def track(self: EvmAnchor, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        made.append(self)

    EvmAnchor.__init__ = track  # type: ignore[method-assign]
    yield
    EvmAnchor.__init__ = original  # type: ignore[method-assign]
    for evm in made:
        await evm.close()


def backends(chain: Chain, github: FakeGitHub, calendar: FakeCalendar) -> job.Backends:
    return job.Backends(
        evm=EvmAnchor(chain.rpc_url, chain.anchor, chain.poster_key, chain.registry, tx_timeout=30),
        evm_name=AnchorBackend.AMOY,
        github=GitHubWitness(
            "demo/witness",
            "token",
            client=httpx.AsyncClient(transport=httpx.MockTransport(github.handler)),
        ),
        ots=OtsStamper([calendar]),
    )


async def test_anchor_job_end_to_end(
    admin_engine: AsyncEngine, deployed: Chain, clinic: uuid.UUID
) -> None:
    github, calendar = FakeGitHub(), FakeCalendar("https://calendar.test")
    b = backends(deployed, github, calendar)
    await add_events(clinic, 5)

    runs = await job.run(b)
    assert runs is not None
    mine = next(r for r in runs if r.clinic_id == str(clinic))
    assert mine.tree_size == 5
    assert mine.results == {"amoy": "confirmed", "github": "confirmed", "ots": "pending"}

    evm = b.evm
    assert evm is not None
    size, root = await evm.latest(clinic)
    async with AsyncSession(admin_engine) as s:
        checkpoint = await s.scalar(
            select(m.MerkleCheckpoint)
            .where(m.MerkleCheckpoint.clinic_id == clinic)
            .order_by(m.MerkleCheckpoint.tree_size.desc())
        )
    assert checkpoint is not None
    assert (size, root) == (5, checkpoint.root)
    assert (
        await evm.signer_key_hash(clinic)
        == hashlib.sha256(public_key_bytes(clinic_signing_key(clinic))).digest()
    )

    witness = github.files[f"/repos/demo/witness/contents/witness/{clinic}/5.json"]
    assert witness["sth"]["root_hex"] == checkpoint.root.hex()
    assert calendar.submitted == [bytes.fromhex(witness["sth_digest"])]

    rows = await receipts(admin_engine, clinic)
    assert rows["amoy"][0].status == AnchorStatus.CONFIRMED
    assert rows["amoy"][0].tx_ref and rows["amoy"][0].block_ref
    assert rows["ots"][0].status == AnchorStatus.PENDING

    # A second run changes nothing.
    again = await job.run(b)
    assert again is not None
    rerun = next(r for r in again if r.clinic_id == str(clinic))
    assert rerun.new_checkpoint is False
    assert rerun.results["amoy"] == "already confirmed"
    assert rerun.results["ots"] == "stamped less than an hour ago"

    # Once the calendar has a Bitcoin attestation, the pending proof is upgraded.
    calendar.ready = True
    await job.upgrade_ots(OtsStamper([calendar]))
    rows = await receipts(admin_engine, clinic)
    assert rows["ots"][0].status == AnchorStatus.CONFIRMED
    assert rows["ots"][0].block_ref == "900000"


async def test_new_events_extend_the_anchored_chain(
    admin_engine: AsyncEngine, deployed: Chain, clinic: uuid.UUID
) -> None:
    b = backends(deployed, FakeGitHub(), FakeCalendar("https://calendar.test"))
    await add_events(clinic, 2)
    await job.run(b)
    await add_events(clinic, 3)
    runs = await job.run(b)
    assert runs is not None
    mine = next(r for r in runs if r.clinic_id == str(clinic))
    assert mine.tree_size == 5
    assert mine.results["amoy"] == "confirmed"
    assert b.evm is not None
    assert (await b.evm.latest(clinic))[0] == 5


async def test_rewritten_history_is_not_anchored(
    admin_engine: AsyncEngine, deployed: Chain, clinic: uuid.UUID
) -> None:
    b = backends(deployed, FakeGitHub(), FakeCalendar("https://calendar.test"))
    await add_events(clinic, 4)
    await job.run(b)
    assert b.evm is not None
    anchored = await b.evm.latest(clinic)

    # A DBA rewrites one event and recomputes every hash after it.
    async with AsyncSession(admin_engine) as s:
        events = (
            await s.scalars(
                select(m.AuditEvent)
                .where(m.AuditEvent.clinic_id == clinic)
                .order_by(m.AuditEvent.seq)
            )
        ).all()
    prev = GENESIS
    async with admin_engine.begin() as conn:
        await conn.execute(
            text("ALTER TABLE audit_events DISABLE TRIGGER audit_events_append_only")
        )
        await conn.execute(text("DELETE FROM anchor_receipts WHERE clinic_id = :c"), {"c": clinic})
        await conn.execute(
            text("DELETE FROM merkle_checkpoints WHERE clinic_id = :c"), {"c": clinic}
        )
        for i, event in enumerate(events):
            payload = {"i": 99} if i == 1 else event.payload
            digest = payload_hash(payload)
            new_hash = chain_hash(prev, digest, event.seq, clinic)
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

    await add_events(clinic, 1)
    runs = await job.run(b)
    assert runs is not None
    mine = next(r for r in runs if r.clinic_id == str(clinic))
    # The local checkpoints were deleted too, so only the anchored root catches the rewrite.
    assert mine.results["amoy"].startswith("error")
    assert await b.evm.latest(clinic) == anchored


async def test_only_one_run_at_a_time(deployed: Chain) -> None:
    b = job.Backends()
    results = await asyncio.gather(job.run(b), job.run(b))
    assert results.count(None) <= 1
    assert any(r is not None for r in results)


async def test_register_clinic_script_needs_two_admins(
    admin_engine: AsyncEngine, deployed: Chain
) -> None:
    admins = [await funded_wallet(deployed) for _ in range(3)]
    registry = await create_contract(deployed, "ClinicRegistry", [a.address for a in admins])
    evm = EvmAnchor(deployed.rpc_url, deployed.anchor, None, registry)
    clinic_id = await new_clinic(admin_engine)
    reg = register_clinic.registration(clinic_id, deployed.poster)
    assert reg.clinic_id_bytes32 == "0x" + clinic_bytes32(clinic_id).hex()

    first = await register_clinic.confirm(evm, "0x" + bytes(admins[0].key).hex(), reg)
    assert first.registered_key_hash is None
    second = await register_clinic.confirm(evm, "0x" + bytes(admins[1].key).hex(), reg)
    assert second.registered_key_hash == reg.key_hash
    assert await evm.signer_key_hash(clinic_id) == bytes.fromhex(reg.key_hash[2:])
