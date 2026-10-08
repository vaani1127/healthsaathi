"""Anchor job (SPEC 6): checkpoint every clinic with new audit events and publish the signed tree
heads to the configured witnesses.

Safe to run twice at once: a session-level advisory lock lets only one run proceed. Every step is
recorded in anchor_receipts, so a failed or interrupted run is picked up by the next one. Runs as
the anchor_job database role.

    uv run python -m app.anchor.job
"""

import asyncio
import json
import logging
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.anchor.evm import ZERO32, EvmAnchor
from app.anchor.github import GitHubWitness
from app.anchor.ots import OtsStamper
from app.core.config import get_settings
from app.core.db import TenantContext, get_engine, tenant_session
from app.db.enums import AnchorBackend, AnchorStatus
from app.db.models import AnchorReceipt, Clinic
from app.ledger.checkpoint import (
    CheckpointResult,
    LedgerIntegrityError,
    build_checkpoint,
    clinic_signing_key,
    load_tree,
)
from e2d_core.ledger import public_key_bytes

logger = logging.getLogger(__name__)

JOB_LOCK_KEY = 0x48534E43  # "HSNC": one anchor run at a time across every process
OTS_INTERVAL = timedelta(hours=1)
JOB_ROLE = "anchor_job"


@dataclass
class Backends:
    evm: EvmAnchor | None = None
    evm_name: AnchorBackend = AnchorBackend.AMOY
    github: GitHubWitness | None = None
    ots: OtsStamper | None = None


def backends_from_settings() -> Backends:
    s = get_settings()
    evm = None
    if s.anchor_rpc_url and s.audit_anchor_address:
        evm = EvmAnchor(
            s.anchor_rpc_url,
            s.audit_anchor_address,
            s.anchor_poster_private_key.get_secret_value() if s.anchor_poster_private_key else None,
            s.clinic_registry_address,
            tx_timeout=s.anchor_tx_timeout_seconds,
        )
    github = None
    if s.witness_repo and s.witness_github_token:
        github = GitHubWitness(
            s.witness_repo, s.witness_github_token.get_secret_value(), s.witness_branch
        )
    return Backends(
        evm=evm,
        evm_name=AnchorBackend(s.anchor_chain),
        github=github,
        ots=OtsStamper(s.ots_calendars) if s.ots_enabled else None,
    )


@dataclass
class ClinicRun:
    clinic_id: str
    tree_size: int
    new_checkpoint: bool
    results: dict[str, str] = field(default_factory=dict)


def _ctx(clinic_id: uuid.UUID | None = None) -> TenantContext:
    return TenantContext(clinic_id=clinic_id, role="system", db_role=JOB_ROLE)


async def _now(db: AsyncSession) -> datetime:
    value = await db.scalar(text("SELECT clock_timestamp()"))
    assert isinstance(value, datetime)
    return value


async def _receipt(
    db: AsyncSession, checkpoint_id: uuid.UUID, backend: AnchorBackend
) -> AnchorReceipt | None:
    return await db.scalar(
        select(AnchorReceipt).where(
            AnchorReceipt.checkpoint_id == checkpoint_id, AnchorReceipt.backend == backend
        )
    )


async def _save(
    clinic_id: uuid.UUID,
    checkpoint_id: uuid.UUID,
    backend: AnchorBackend,
    status: AnchorStatus,
    tx_ref: str | None = None,
    block_ref: str | None = None,
    proof: bytes | None = None,
) -> None:
    async with tenant_session(_ctx(clinic_id)) as db:
        row = await _receipt(db, checkpoint_id, backend)
        now = await _now(db)
        if row is None:
            row = AnchorReceipt(
                clinic_id=clinic_id, checkpoint_id=checkpoint_id, backend=backend, submitted_at=now
            )
            db.add(row)
        row.status = status
        row.tx_ref = tx_ref if tx_ref is not None else row.tx_ref
        row.block_ref = block_ref if block_ref is not None else row.block_ref
        row.proof = proof if proof is not None else row.proof
        if status == AnchorStatus.CONFIRMED and row.confirmed_at is None:
            row.confirmed_at = now


async def _status(checkpoint_id: uuid.UUID, backend: AnchorBackend) -> AnchorReceipt | None:
    async with tenant_session(_ctx()) as db:
        return await _receipt(db, checkpoint_id, backend)


# Backends -----------------------------------------------------------------------------------------


async def anchor_on_chain(evm: EvmAnchor, name: AnchorBackend, cp: CheckpointResult) -> str:
    clinic_id = cp.checkpoint.clinic_id
    existing = await _status(cp.checkpoint.id, name)
    if existing is not None and existing.status == AnchorStatus.CONFIRMED:
        return "already confirmed"
    if existing is not None and existing.status == AnchorStatus.PENDING and existing.tx_ref:
        mined = await evm.receipt(existing.tx_ref)
        if mined is None:
            return "waiting for transaction"
        status = AnchorStatus.CONFIRMED if mined.succeeded else AnchorStatus.FAILED
        await _save(clinic_id, cp.checkpoint.id, name, status, block_ref=str(mined.block_number))
        return status.value

    onchain_size, onchain_root = await evm.latest(clinic_id)
    size, root = cp.checkpoint.tree_size, cp.checkpoint.root
    if onchain_size >= size:
        if onchain_size == size and onchain_root == root:
            await _save(clinic_id, cp.checkpoint.id, name, AnchorStatus.CONFIRMED)
            return "already on chain"
        return "chain is ahead"
    if onchain_size > 0:
        # The anchored root must still be a prefix of today's log; otherwise history was rewritten.
        async with tenant_session(_ctx(clinic_id)) as db:
            tree, _ = await load_tree(db, clinic_id)
        if tree.root(onchain_size) != onchain_root:
            await _save(clinic_id, cp.checkpoint.id, name, AnchorStatus.FAILED)
            logger.critical(
                "audit log does not match the anchored root",
                extra={"clinic_id": str(clinic_id), "anchored_size": onchain_size},
            )
            raise LedgerIntegrityError("audit log does not match the anchored root")

    tx_hash = await evm.send_anchor(
        clinic_id, size, root, onchain_root if onchain_size else ZERO32, cp.head.digest()
    )
    # Record the transaction before waiting, so an interrupted run can find it again.
    await _save(clinic_id, cp.checkpoint.id, name, AnchorStatus.PENDING, tx_ref=tx_hash)
    mined = await evm.wait(tx_hash)
    status = AnchorStatus.CONFIRMED if mined.succeeded else AnchorStatus.FAILED
    await _save(clinic_id, cp.checkpoint.id, name, status, block_ref=str(mined.block_number))
    return status.value


async def publish_witness(github: GitHubWitness, cp: CheckpointResult) -> str:
    clinic_id = cp.checkpoint.clinic_id
    existing = await _status(cp.checkpoint.id, AnchorBackend.GITHUB)
    if existing is not None and existing.status == AnchorStatus.CONFIRMED:
        return "already confirmed"
    document = {
        "sth": cp.head.to_json(),
        "signature": cp.checkpoint.signature.hex(),
        "public_key": public_key_bytes(clinic_signing_key(clinic_id)).hex(),
        "sth_digest": cp.head.digest().hex(),
    }
    sha = await github.publish(str(clinic_id), cp.head.tree_size, document)
    await _save(
        clinic_id, cp.checkpoint.id, AnchorBackend.GITHUB, AnchorStatus.CONFIRMED, tx_ref=sha
    )
    return "confirmed"


async def stamp_ots(ots: OtsStamper, cp: CheckpointResult) -> str:
    clinic_id = cp.checkpoint.clinic_id
    async with tenant_session(_ctx(clinic_id)) as db:
        last = await db.scalar(
            select(AnchorReceipt.submitted_at)
            .where(AnchorReceipt.clinic_id == clinic_id, AnchorReceipt.backend == AnchorBackend.OTS)
            .order_by(AnchorReceipt.submitted_at.desc())
            .limit(1)
        )
        now = await _now(db)
    if last is not None and now - last < OTS_INTERVAL:
        return "stamped less than an hour ago"
    if await _status(cp.checkpoint.id, AnchorBackend.OTS) is not None:
        return "already stamped"
    proof = await asyncio.to_thread(ots.stamp, cp.head.digest())
    status = AnchorStatus.CONFIRMED if proof.bitcoin_height else AnchorStatus.PENDING
    await _save(
        clinic_id,
        cp.checkpoint.id,
        AnchorBackend.OTS,
        status,
        block_ref=str(proof.bitcoin_height) if proof.bitcoin_height else None,
        proof=proof.data,
    )
    return status.value


async def upgrade_ots(ots: OtsStamper) -> int:
    async with tenant_session(_ctx()) as db:
        pending = (
            await db.scalars(
                select(AnchorReceipt).where(
                    AnchorReceipt.backend == AnchorBackend.OTS,
                    AnchorReceipt.status == AnchorStatus.PENDING,
                )
            )
        ).all()
    upgraded = 0
    for row in pending:
        if row.proof is None:
            continue
        proof = await asyncio.to_thread(ots.upgrade, row.proof)
        if proof.bitcoin_height:
            await _save(
                row.clinic_id,
                row.checkpoint_id,
                AnchorBackend.OTS,
                AnchorStatus.CONFIRMED,
                block_ref=str(proof.bitcoin_height),
                proof=proof.data,
            )
            upgraded += 1
    return upgraded


# Run ----------------------------------------------------------------------------------------------


async def clinics_with_events() -> list[uuid.UUID]:
    async with tenant_session(_ctx()) as db:
        rows = await db.scalars(
            select(Clinic.id).where(
                text("EXISTS (SELECT 1 FROM audit_events a WHERE a.clinic_id = clinics.id)")
            )
        )
        return list(rows.all())


async def run(backends: Backends | None = None) -> list[ClinicRun] | None:
    """One anchoring pass. Returns None when another run holds the lock."""
    backends = backends or backends_from_settings()
    async with get_engine().connect() as lock_conn:
        got = await lock_conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": JOB_LOCK_KEY})
        if not got:
            logger.info("anchor job already running")
            return None
        try:
            runs = []
            for clinic_id in await clinics_with_events():
                runs.append(await _run_clinic(clinic_id, backends))
            if backends.ots is not None:
                await upgrade_ots(backends.ots)
            return runs
        finally:
            await lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": JOB_LOCK_KEY})
            await lock_conn.commit()


async def _run_clinic(clinic_id: uuid.UUID, backends: Backends) -> ClinicRun:
    try:
        cp = await build_checkpoint(clinic_id, db_role=JOB_ROLE)
    except LedgerIntegrityError as exc:
        logger.critical(
            "checkpoint refused", extra={"clinic_id": str(clinic_id), "error": str(exc)}
        )
        return ClinicRun(str(clinic_id), 0, False, {"checkpoint": f"refused: {exc}"})
    result = ClinicRun(str(clinic_id), cp.head.tree_size, cp.created)
    steps: list[tuple[str, Any]] = []
    if backends.evm is not None:
        steps.append(
            (backends.evm_name.value, anchor_on_chain(backends.evm, backends.evm_name, cp))
        )
    if backends.github is not None:
        steps.append(("github", publish_witness(backends.github, cp)))
    if backends.ots is not None:
        steps.append(("ots", stamp_ots(backends.ots, cp)))
    for name, step in steps:
        try:
            result.results[name] = await step
        except Exception as exc:
            # One witness failing must not stop the others or the app.
            logger.exception(
                "anchor step failed", extra={"clinic_id": str(clinic_id), "backend": name}
            )
            result.results[name] = f"error: {type(exc).__name__}"
    return result


async def main() -> int:
    backends = backends_from_settings()
    try:
        runs = await run(backends)
    finally:
        if backends.evm is not None:
            await backends.evm.close()
        await get_engine().dispose()
    print(
        json.dumps(
            [asdict(r) for r in runs] if runs is not None else {"skipped": "already running"}
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
