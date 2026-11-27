"""Record tamper demo (ADR 0011): which check catches a changed clinical record. Local only.

Uses the local anvil chain from `make contracts-local`, a throwaway local witness folder, and
synthetic records written through the API itself (in process), so the version writer, the access
policy and `record_and_explain()` all run as in production. Each case gets a fresh synthetic
clinic with a signed note, a released lab result and an allergy, anchored on anvil and published
to the witness, then one change by the database owner:

1. nothing changed (control);
2. one lab value changed in place;
3. the lab value changed, and the version's commitment, its audit leaf and every later hash
   recomputed to match (a careful database owner);
4. the lab result row deleted;
5. a new vital changed the same careful way as 3, but BEFORE the next anchor (inside the rewrite
   window). This one is not caught, and the demo says so.

After each change, three checks run: the API's integrity verdict, an independent check that does
what the browser verifier does (packages/receipt-verify: commitment, leaf hash, inclusion proof,
tree head signature, root on chain), and the anchor job plus the witness check on the next anchor.

    make contracts-local && make record-tamper-demo

Prints a PASS/FAIL table and writes experiments/results/record-integrity/tamper.json.
"""

import asyncio
import hashlib
import json
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.anchor import job
from app.anchor.evm import EvmAnchor
from app.anchor.local_witness import LocalWitness
from app.anchor.witness_check import heads_from_folder
from app.core.config import REPO_ROOT, get_settings
from app.core.db import get_engine
from app.db import models as m
from app.db.enums import AnchorBackend, RecordType, Role, Sex
from app.identity import service as identity
from app.identity import tokens
from app.ledger.checkpoint import LedgerIntegrityError, build_checkpoint
from app.ledger.records import commitment, content_of
from app.scripts import tamper_demo
from app.scripts.verify_witness import verify as verify_witness
from e2d_core.ledger import GENESIS, TreeHead, chain_hash, leaf_hash, payload_hash, verify
from e2d_core.ledger.jcs import canonicalize
from e2d_core.ledger.merkle import verify_inclusion

OUT = REPO_ROOT / "experiments" / "results" / "record-integrity" / "tamper.json"
PDF = b"%PDF-1.4 synthetic demo report"


# Database owner actions --------------------------------------------------------------------------


def owner_engine() -> Any:
    return create_async_engine(get_settings().migrator_database_url)


async def owner_sql(statements: list[tuple[str, dict[str, Any]]], tables: tuple[str, ...]) -> None:
    """Run statements as the table owner with the user triggers of `tables` switched off."""
    engine = owner_engine()
    try:
        async with engine.begin() as conn:
            for table in tables:
                await conn.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER USER"))
            for sql, params in statements:
                await conn.execute(text(sql), params)
            for table in tables:
                await conn.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER USER"))
    finally:
        await engine.dispose()


async def rewrite_leaf(clinic_id: uuid.UUID, seq: int, payload: dict[str, Any]) -> None:
    """Replace one audit payload and recompute the hash chain from there, so the chain on its own
    still verifies (what tamper_demo.tamper does, for one chosen event)."""
    engine = owner_engine()
    try:
        async with AsyncSession(engine) as s:
            events = (
                await s.scalars(
                    select(m.AuditEvent)
                    .where(m.AuditEvent.clinic_id == clinic_id)
                    .order_by(m.AuditEvent.seq)
                )
            ).all()
        prev = GENESIS
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER USER"))
            for event in events:
                if event.seq < seq:
                    prev = event.chain_hash
                    continue
                body = payload if event.seq == seq else event.payload
                digest = payload_hash(body)
                link = chain_hash(prev, digest, event.seq, clinic_id)
                await conn.execute(
                    text(
                        "UPDATE audit_events SET payload = CAST(:p AS jsonb), payload_hash = :d,"
                        " prev_hash = :prev, chain_hash = :h WHERE seq = :s"
                    ),
                    {"p": json.dumps(body), "d": digest, "prev": prev, "h": link, "s": event.seq},
                )
                prev = link
            await conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER USER"))
    finally:
        await engine.dispose()


async def forge_version(
    clinic_id: uuid.UUID, record_type: RecordType, record_id: uuid.UUID, update_sql: str
) -> None:
    """Change a record row, then make its newest version agree: recompute the commitment with the
    stored salt, write it to record_versions and to the audit leaf, and recompute the chain."""
    model, table = {
        RecordType.LAB_RESULT: (m.LabResult, "lab_results"),
        RecordType.VITAL: (m.Vital, "vitals"),
    }[record_type]
    await owner_sql([(update_sql, {"id": record_id})], (table,))
    engine = owner_engine()
    try:
        async with AsyncSession(engine) as s:
            row = await s.get(model, record_id)
            version = await s.scalar(
                select(m.RecordVersion)
                .where(m.RecordVersion.record_id == record_id)
                .order_by(m.RecordVersion.version.desc())
                .limit(1)
            )
            assert row is not None and version is not None
            digest = commitment(version.salt, content_of(record_type, row))
            leaf = await s.scalar(
                select(m.AuditEvent.payload).where(m.AuditEvent.seq == version.audit_seq)
            )
            assert leaf is not None
            seq, version_id = version.audit_seq, version.id
    finally:
        await engine.dispose()
    await owner_sql(
        [
            (
                "UPDATE record_versions SET commitment = :c WHERE id = :id",
                {"c": digest, "id": version_id},
            )
        ],
        ("record_versions",),
    )
    await rewrite_leaf(clinic_id, seq, {**leaf, "commitment": digest.hex()})


# Synthetic clinic through the API ----------------------------------------------------------------


@dataclass
class Actor:
    user_id: uuid.UUID
    headers: dict[str, str]


async def actor(clinic_id: uuid.UUID, role: Role) -> Actor:
    engine = owner_engine()
    try:
        async with AsyncSession(engine) as s, s.begin():
            user = m.User(
                email=f"{role.value}.{uuid.uuid4().hex[:10]}@tamper-demo.test",
                name=f"Demo {role.value}",
            )
            s.add(user)
            await s.flush()
            s.add(m.Membership(clinic_id=clinic_id, user_id=user.id, role=role))
            user_id = user.id
    finally:
        await engine.dispose()
    kind: identity.SessionKind = "email" if role == Role.PATIENT else "mfa"
    issued = await identity.create_session(
        user_id, kind, identity.DeviceInfo("record-tamper-demo", "demo")
    )
    token, _ = tokens.access_token(
        user_id, issued.session_id, identity.AMR[kind], clinic_id, role.value
    )
    return Actor(user_id, {"authorization": f"Bearer {token}"})


@dataclass
class Clinic:
    clinic_id: uuid.UUID
    doctor: Actor
    lab: Actor
    patient: uuid.UUID
    encounter: str
    records: dict[str, tuple[RecordType, str]] = field(default_factory=dict)


async def ok(resp: Any) -> Any:
    if resp.status_code >= 300:
        raise RuntimeError(
            f"{resp.request.method} {resp.request.url}: {resp.status_code} {resp.text}"
        )
    return resp.json()


async def clinic_with_records(api: AsyncClient, evm: EvmAnchor, admins: list[str]) -> Clinic:
    clinic_id = await tamper_demo.setup(evm, admins)
    reception, doctor = await actor(clinic_id, Role.RECEPTION), await actor(clinic_id, Role.DOCTOR)
    lab, nurse = await actor(clinic_id, Role.LAB_TECH), await actor(clinic_id, Role.NURSE)
    engine = owner_engine()
    try:
        async with AsyncSession(engine) as s, s.begin():
            patient = m.Patient(
                clinic_id=clinic_id,
                mrn=f"DEMO{uuid.uuid4().hex[:6].upper()}",
                name="Synthetic Patient Demo",
                sex=Sex.FEMALE,
                created_by=reception.user_id,
            )
            s.add(patient)
            await s.flush()
            patient_id = patient.id
    finally:
        await engine.dispose()
    appt = await ok(
        await api.post(
            "/api/v1/appointments",
            json={
                "patient_id": str(patient_id),
                "doctor_user_id": str(doctor.user_id),
                "slot_start": datetime.now(UTC).isoformat(),
            },
            headers=reception.headers,
        )
    )
    enc = await ok(
        await api.post(f"/api/v1/appointments/{appt['id']}/encounter", headers=doctor.headers)
    )
    c = Clinic(clinic_id, doctor, lab, patient_id, enc["id"])
    note = await ok(
        await api.post(
            f"/api/v1/patients/{patient_id}/notes",
            json={"encounter_id": c.encounter, "body": "Synthetic demo note: fever for 2 days."},
            headers=doctor.headers,
        )
    )
    await ok(await api.post(f"/api/v1/notes/{note['id']}/sign", headers=doctor.headers))
    allergy = await ok(
        await api.post(
            f"/api/v1/patients/{patient_id}/allergies",
            json={"substance": "Penicillin", "severity": "severe"},
            headers=nurse.headers,
        )
    )
    order = await ok(
        await api.post(
            f"/api/v1/patients/{patient_id}/lab-orders",
            json={"encounter_id": c.encounter, "tests": [{"code": "HB", "name": "Haemoglobin"}]},
            headers=doctor.headers,
        )
    )
    await ok(
        await api.patch(
            f"/api/v1/lab-orders/{order['id']}", json={"status": "collected"}, headers=lab.headers
        )
    )
    resulted = await ok(
        await api.post(
            f"/api/v1/lab-orders/{order['id']}/result",
            data={"values": json.dumps({"hb": {"value": 13.1, "unit": "g/dL"}})},
            files={"file": ("hb.pdf", PDF, "application/pdf")},
            headers=lab.headers,
        )
    )
    await ok(await api.post(f"/api/v1/lab-orders/{order['id']}/release", headers=lab.headers))
    c.records = {
        "note": (RecordType.NOTE, note["id"]),
        "allergy": (RecordType.ALLERGY, allergy["id"]),
        "lab_result": (RecordType.LAB_RESULT, resulted["result"]["id"]),
    }
    return c


async def add_vital(api: AsyncClient, c: Clinic, *, keep: bool = True) -> str:
    """A new vital through the API. With keep, it becomes the clinic's "vital" record."""
    vital = await ok(
        await api.post(
            f"/api/v1/patients/{c.patient}/vitals", json={"pulse": 78}, headers=c.doctor.headers
        )
    )
    if keep:
        c.records["vital"] = (RecordType.VITAL, vital["id"])
    return str(vital["id"])


async def anchor_and_publish(evm: EvmAnchor, witness: LocalWitness, clinic_id: uuid.UUID) -> str:
    cp = await build_checkpoint(clinic_id, db_role=job.JOB_ROLE)
    result = await job.anchor_on_chain(evm, AnchorBackend.AMOY, cp)
    await job.publish_witness(witness, cp)
    return result


# Checks ------------------------------------------------------------------------------------------


def browser_check(r: dict[str, Any], onchain: tuple[int, bytes]) -> tuple[bool, str]:
    """What packages/receipt-verify does (verifyRecordCheck, then checkRecordAnchor), in Python."""
    newest: dict[str, dict[str, Any]] = {}
    for v in r["versions"]:
        if v["record_id"] not in newest or v["version"] > newest[v["record_id"]]["version"]:
            newest[v["record_id"]] = v
    if not r["versions"]:
        return False, "no committed version"
    for v in r["versions"]:
        p = v["payload"]
        if p is None:
            return False, "a stored version has no leaf"
        digest = hashlib.sha256(canonicalize(p)).digest()
        if digest.hex() != v["payload_hash"] or leaf_hash(digest).hex() != v["leaf_hash"]:
            return False, "leaf does not match its hash"
        if p.get("record_id") != v["record_id"] or p.get("commitment") != v["commitment"]:
            return False, "leaf names another record or commitment"
        if v["proof"] is None:
            return False, "not in a signed checkpoint yet"
        head = TreeHead.from_json(v["sth"])
        if not verify_inclusion(
            bytes.fromhex(v["leaf_hash"]),
            v["leaf_index"],
            head.tree_size,
            [bytes.fromhex(x) for x in v["proof"]],
            bytes.fromhex(head.root_hex),
        ):
            return False, "inclusion proof does not reach the signed root"
        if not verify(head, bytes.fromhex(v["signature"]), bytes.fromhex(r["public_key"])):
            return False, "tree head signature is not the clinic's"
        if newest[v["record_id"]] is v:
            if v["content"] is None:
                return False, "the record row is gone"
            recomputed = hashlib.sha256(bytes.fromhex(v["salt"]) + canonicalize(v["content"]))
            if recomputed.hexdigest() != p["commitment"]:
                return False, "record differs from its committed version"
        size, root = onchain
        if size == head.tree_size and root.hex() != head.root_hex:
            return False, "the chain holds a different root"
    return True, "all checks pass and the root is on chain"


@dataclass
class CaseResult:
    case: int
    name: str
    action: str
    record: str
    expected_caught: bool
    server_verdict: str
    server_reason: str | None
    browser: str
    browser_detail: str
    next_anchor: str
    witness: str
    caught: bool = False
    as_expected: bool = False


async def run_checks(
    api: AsyncClient,
    evm: EvmAnchor,
    witness: LocalWitness,
    c: Clinic,
    key: str,
) -> tuple[str, str | None, str, str, str, str]:
    record_type, record_id = c.records[key]
    r = await ok(
        await api.get(
            f"/api/v1/records/{record_type.value}/{record_id}/integrity", headers=c.doctor.headers
        )
    )
    browser_ok, detail = browser_check(r, await evm.latest(c.clinic_id))
    # The next anchor: a new event arrives and the anchor job checkpoints and anchors it.
    await add_vital(api, c, keep=False)
    try:
        nxt = await anchor_and_publish(evm, witness, c.clinic_id)
        next_anchor = f"anchored ({nxt})"
    except LedgerIntegrityError as exc:
        next_anchor = f"refused: {exc}"
    heads = await verify_witness(c.clinic_id, heads_from_folder(witness.folder, c.clinic_id))
    failed = [h for h in heads if not h.ok]
    witness_result = (
        f"{len(failed)} of {len(heads)} published heads no longer match"
        if failed
        else f"all {len(heads)} published heads match"
    )
    return (
        r["verdict"],
        r["reason"],
        "PASS" if browser_ok else "FAIL",
        detail,
        next_anchor,
        witness_result,
    )


CASES: list[tuple[int, str, str, str, bool]] = [
    (1, "control", "nothing changed", "lab_result", False),
    (2, "row changed", "one lab value changed in place by the database owner", "lab_result", True),
    (
        3,
        "row, commitment and log rewritten",
        "lab value changed, commitment, audit leaf and every later hash recomputed",
        "lab_result",
        True,
    ),
    (4, "row deleted", "the lab result row deleted by the database owner", "lab_result", True),
    (
        5,
        "rewritten before its first anchor",
        "a new vital changed like case 3, before any anchor covered it",
        "vital",
        False,
    ),
]


async def case(api: AsyncClient, evm: EvmAnchor, admins: list[str], number: int) -> CaseResult:
    _, name, action, key, expected = CASES[number - 1]
    c = await clinic_with_records(api, evm, admins)
    with tempfile.TemporaryDirectory() as folder:
        witness = LocalWitness(Path(folder))
        await anchor_and_publish(evm, witness, c.clinic_id)
        result_id = uuid.UUID(c.records["lab_result"][1])
        if number == 2:
            await owner_sql(
                [
                    (
                        "UPDATE lab_results SET values = jsonb_set(values, '{hb,value}', '9.4')"
                        " WHERE id = :id",
                        {"id": result_id},
                    )
                ],
                ("lab_results",),
            )
        elif number == 3:
            await forge_version(
                c.clinic_id,
                RecordType.LAB_RESULT,
                result_id,
                "UPDATE lab_results SET values = jsonb_set(values, '{hb,value}', '9.4')"
                " WHERE id = :id",
            )
        elif number == 4:
            await owner_sql(
                [("DELETE FROM lab_results WHERE id = :id", {"id": result_id})], ("lab_results",)
            )
        elif number == 5:
            vital_id = uuid.UUID(await add_vital(api, c))
            await forge_version(
                c.clinic_id,
                RecordType.VITAL,
                vital_id,
                "UPDATE vitals SET pulse = 140 WHERE id = :id",
            )
            await anchor_and_publish(evm, witness, c.clinic_id)
        server, reason, browser, detail, next_anchor, witness_result = await run_checks(
            api, evm, witness, c, key
        )
    caught = (
        server != "match"
        or browser == "FAIL"
        or next_anchor.startswith("refused")
        or "no longer match" in witness_result
    )
    return CaseResult(
        number,
        name,
        action,
        key,
        expected,
        server,
        reason,
        browser,
        detail,
        next_anchor,
        witness_result,
        caught,
        caught == expected,
    )


def table(results: list[CaseResult]) -> str:
    def word(ok: bool) -> str:
        return "PASS" if ok else "FAIL"

    rows = [
        (
            "#",
            "case",
            "API check",
            "browser check",
            "next anchor",
            "witness",
            "caught",
            "as expected",
        )
    ]
    for r in results:
        rows.append(
            (
                str(r.case),
                r.name,
                word(r.server_verdict == "match") + f" ({r.server_verdict})",
                r.browser,
                "FAIL (refused)" if r.next_anchor.startswith("refused") else "PASS",
                "FAIL" if "no longer match" in r.witness else "PASS",
                "yes" if r.caught else "NO",
                "yes" if r.as_expected else "NO",
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = ["  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)) for row in rows]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


async def run() -> int:
    s = get_settings()
    if s.env not in ("local", "test"):
        print("record_tamper_demo only runs with APP_ENV=local or test")
        return 2
    try:
        deployment = json.loads(
            await asyncio.to_thread(tamper_demo.DEPLOYMENT.read_text, encoding="utf-8")
        )
    except FileNotFoundError:
        print("No local deployment found. Run `make contracts-local` first.")
        return 2
    from app.main import create_app

    evm = EvmAnchor(
        "http://127.0.0.1:8545", deployment["AuditAnchor"], None, deployment["ClinicRegistry"]
    )
    results: list[CaseResult] = []
    try:
        if await evm.chain_id() != tamper_demo.ANVIL_CHAIN_ID:
            print("The chain at 127.0.0.1:8545 is not a local anvil chain.")
            return 2
        transport = ASGITransport(app=create_app())
        async with AsyncClient(transport=transport, base_url="http://demo") as api:
            for number in range(1, len(CASES) + 1):
                results.append(await case(api, evm, deployment["admins"], number))
                print(f"case {number} done", flush=True)
    finally:
        await evm.close()
        await get_engine().dispose()
    print()
    print(table(results))
    print()
    for r in results:
        print(f"{r.case}. {r.action}")
        print(f"   API: {r.server_verdict}{f' ({r.server_reason})' if r.server_reason else ''}")
        print(f"   browser: {r.browser} ({r.browser_detail})")
        print(f"   next anchor: {r.next_anchor}; witness: {r.witness}")
    print()
    print(
        "Case 5 is a known limit: a change made before the next anchor is committed with it, so "
        "no check can tell it apart from the real record."
    )
    report = {
        "run_at": datetime.now(UTC).isoformat(),
        "setup": "local anvil chain and a local git witness; per case a fresh synthetic clinic "
        "with a signed note, a released lab result and an allergy written through the API, "
        "anchored and published once before the change",
        "browser_check": "app.scripts.record_tamper_demo.browser_check, the same steps as "
        "packages/receipt-verify verifyRecordCheck and checkRecordAnchor",
        "cases": [asdict(r) for r in results],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes((json.dumps(report, indent=2) + "\n").encode())
    print(f"written: {OUT.relative_to(REPO_ROOT)}")
    return 0 if all(r.as_expected for r in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
