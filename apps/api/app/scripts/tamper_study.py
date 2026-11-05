"""Which check catches a database owner who rewrites the audit log (SPEC 8 attack demo, P16).

Local only: the anvil chain from `make contracts-local` and a throwaway local witness folder.
Two attacks, each on its own fresh synthetic clinic with three anchored and published checkpoints:

- "events": edit one audit row and recompute every hash after it (the hash chain looks fine);
- "events and checkpoints": the same, and also delete the clinic's stored checkpoints and anchor
  receipts, so the database holds no trace of the old roots.

After each attack (and once before it, as a control) four checks run, the way the system runs them:

1. the admin audit page (log against the stored checkpoints and the newest head on the witness),
2. the next checkpoint after a new event (consistency with the previous stored checkpoint),
3. the next anchor on the chain (log against the root on chain),
4. an auditor's witness check (`app.scripts.verify_witness`, log against the published heads).

    make contracts-local && make tamper-study

Writes experiments/results/anchoring/tamper/detection.json.
"""

import asyncio
import json
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.anchor import job
from app.anchor.evm import EvmAnchor
from app.anchor.local_witness import LocalWitness
from app.anchor.receipts import audit_status
from app.anchor.witness_check import heads_from_folder
from app.core.config import REPO_ROOT, get_settings
from app.core.db import TenantContext, get_engine, tenant_session
from app.db.enums import AnchorBackend
from app.ledger.checkpoint import LedgerIntegrityError, build_checkpoint, load_tree
from app.ledger.writer import append_audit_event
from app.scripts import tamper_demo
from app.scripts.verify_witness import verify

OUT = REPO_ROOT / "experiments" / "results" / "anchoring" / "tamper" / "detection.json"
ROUNDS = 3
EVENTS_PER_ROUND = 4
CHECKS = ("audit_page", "next_checkpoint", "chain", "witness")
ATTACKS = ("events", "events_and_checkpoints")


async def add_events(clinic_id: uuid.UUID, n: int, label: str) -> None:
    for i in range(n):
        async with tenant_session(TenantContext(clinic_id=clinic_id, role="system")) as db:
            await append_audit_event(
                db, clinic_id, "study", {"type": "study", "step": label, "i": i}
            )


async def anchor_round(evm: EvmAnchor, witness: LocalWitness, clinic_id: uuid.UUID) -> None:
    cp = await build_checkpoint(clinic_id, db_role=job.JOB_ROLE)
    await job.anchor_on_chain(evm, AnchorBackend.AMOY, cp)
    await job.publish_witness(witness, cp)


async def delete_stored_checkpoints(clinic_id: uuid.UUID) -> None:
    """What the database owner adds in the second attack: no stored roots left to compare with."""
    engine = create_async_engine(get_settings().migrator_database_url)
    try:
        async with engine.begin() as conn:
            for table in ("anchor_receipts", "merkle_checkpoints"):
                await conn.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER USER"))
                await conn.execute(
                    text(f"DELETE FROM {table} WHERE clinic_id = :c"),  # noqa: S608 - fixed names
                    {"c": clinic_id},
                )
                await conn.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER USER"))
    finally:
        await engine.dispose()


def outcome(detected: bool, detail: str) -> dict[str, Any]:
    return {"detected": detected, "detail": detail}


async def run_checks(evm: EvmAnchor, witness: LocalWitness, clinic_id: uuid.UUID) -> dict[str, Any]:
    results: dict[str, Any] = {}
    ctx = TenantContext(clinic_id=clinic_id, role="system", db_role=job.JOB_ROLE)
    async with tenant_session(ctx) as db:
        status = await audit_status(db, clinic_id, witness_folder=witness.folder)
    page_failed = (
        not status.chain_ok or status.consistency_ok is False or status.witness_ok is False
    )
    results["audit_page"] = outcome(
        page_failed, status.problem or "log matches stored checkpoints and the witness"
    )

    # Checks 2 and 3 run when the next event arrives and the anchor job checkpoints it.
    await add_events(clinic_id, 1, "after")
    try:
        cp = await build_checkpoint(clinic_id, db_role=job.JOB_ROLE)
        results["next_checkpoint"] = outcome(False, "new checkpoint signed")
        try:
            chain = await job.anchor_on_chain(evm, AnchorBackend.AMOY, cp)
            results["chain"] = outcome(False, f"anchored: {chain}")
        except LedgerIntegrityError as exc:
            results["chain"] = outcome(True, str(exc))
    except LedgerIntegrityError as exc:
        results["next_checkpoint"] = outcome(True, str(exc))
        # The job stops before anchoring; check the chain the way the anchor step would.
        size, root = await evm.latest(clinic_id)
        async with tenant_session(ctx) as db:
            tree, _ = await load_tree(db, clinic_id)
        mismatch = tree.root(size) != root
        results["chain"] = outcome(
            mismatch, "log differs from the root on chain" if mismatch else "matches"
        )

    checks = await verify(clinic_id, heads_from_folder(witness.folder, clinic_id))
    failed = [c for c in checks if not c.ok]
    results["witness"] = outcome(
        bool(failed),
        f"{len(failed)} of {len(checks)} published heads no longer match"
        if failed
        else f"all {len(checks)} published heads match",
    )
    return results


async def scenario(evm: EvmAnchor, admins: list[str], attack: str | None) -> dict[str, Any]:
    clinic_id = await tamper_demo.setup(evm, admins)
    with tempfile.TemporaryDirectory() as folder:
        witness = LocalWitness(Path(folder))
        for r in range(ROUNDS):
            await add_events(clinic_id, EVENTS_PER_ROUND, f"round-{r}")
            await anchor_round(evm, witness, clinic_id)
        if attack is not None:
            await tamper_demo.tamper(clinic_id)
            if attack == "events_and_checkpoints":
                await delete_stored_checkpoints(clinic_id)
        return await run_checks(evm, witness, clinic_id)


async def run() -> int:
    s = get_settings()
    if s.env not in ("local", "test"):
        print("tamper_study only runs with APP_ENV=local or test")
        return 2
    deployment = json.loads(tamper_demo.DEPLOYMENT.read_text(encoding="utf-8"))
    evm = EvmAnchor(
        "http://127.0.0.1:8545", deployment["AuditAnchor"], None, deployment["ClinicRegistry"]
    )
    try:
        if await evm.chain_id() != tamper_demo.ANVIL_CHAIN_ID:
            print("The chain at 127.0.0.1:8545 is not a local anvil chain.")
            return 2
        results = {"control": await scenario(evm, deployment["admins"], None)}
        for attack in ATTACKS:
            results[attack] = await scenario(evm, deployment["admins"], attack)
    finally:
        await evm.close()
        await get_engine().dispose()
    report = {
        "run_at": datetime.now(UTC).isoformat(),
        "setup": f"local anvil chain and local git witness; {ROUNDS} checkpoints of "
        f"{EVENTS_PER_ROUND} events anchored and published before the attack",
        "results": results,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes((json.dumps(report, indent=2) + "\n").encode())
    for name, checks in results.items():
        found = ", ".join(c for c in CHECKS if checks[c]["detected"]) or "nothing"
        print(f"{name}: detected by {found}")
    control_clean = not any(v["detected"] for v in results["control"].values())
    return 0 if control_clean else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
