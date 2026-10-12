"""Seed the local demo database (make demo). Runs once; later runs only print the logins.

1. Simulate two clinics with SaathiBench (unchanged simulator, config `sim-demo.yaml`).
2. Load 50 of their patients and all their activity into the product tables (`hs_demo.load`).
3. Register both clinics' signing keys in ClinicRegistry on the local anvil chain.
4. Run the detector over every past day, as the worker would have done each day.
5. Book a few of today's appointments and issue queue tokens through the normal services.
6. Run the anchor job once, so receipts can be verified straight away.
7. Write the login table to the state folder.

    DEMO_STATE_DIR=/state python -m hs_demo.seed
"""

import asyncio
import json
import os
import sys
import tempfile
import uuid
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from eth_account import Account
from sqlalchemy import select

from app.access.service import RequestInfo
from app.anchor import job
from app.anchor.evm import EvmAnchor, clinic_bytes32
from app.clinic import schemas as clinic_schemas
from app.clinic import service as clinic_service
from app.core.config import get_settings
from app.core.db import TenantContext, get_engine, tenant_session
from app.db.enums import Role
from app.db.models import AccessEvent, Alert, Clinic, Membership, Patient, User
from app.detect import service as detect
from app.identity.deps import ClinicPrincipal
from app.scripts.register_clinic import registration
from app.startup import run_startup_tasks
from e2d_core.ids import uuid7
from hs_demo.load import LoadReport, load
from hs_demo.logins import render
from saathibench.config import load_config
from saathibench.run import simulate

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "sim-demo.yaml"
TZ = ZoneInfo("Asia/Kolkata")
TODAY_PER_CLINIC = 6
TOKENS_PER_CLINIC = 3


def state_dir() -> Path:
    return Path(os.environ.get("DEMO_STATE_DIR", "/state"))


def profiles_dir() -> Path:
    found = os.environ.get("SIM_PROFILES_DIR")
    if found:
        return Path(found)
    import saathibench

    # Repo layout: sim/src/saathibench -> sim/profiles
    return Path(saathibench.__file__).resolve().parents[2] / "profiles"


def say(message: str) -> None:
    print(f"[demo-seed] {message}", flush=True)


async def register_on_chain(clinic_ids: list[uuid.UUID]) -> bool:
    """Register each clinic with two of the three registry admins (unlocked anvil accounts) and
    give the poster wallet test ETH. Only for the local anvil chain."""
    s = get_settings()
    if not (s.anchor_rpc_url and s.clinic_registry_address and s.audit_anchor_address):
        say("no local chain configured; skipping on-chain registration")
        return False
    key = s.anchor_poster_private_key
    if key is None:
        raise SystemExit("ANCHOR_POSTER_PRIVATE_KEY is not set")
    poster = Account.from_key(key.get_secret_value()).address
    evm = EvmAnchor(s.anchor_rpc_url, s.audit_anchor_address, None, s.clinic_registry_address)
    try:
        await evm.w3.provider.make_request("anvil_setBalance", [poster, hex(10**21)])
        admins = await evm.w3.eth.accounts
        assert evm.registry is not None
        for clinic_id in clinic_ids:
            reg = registration(clinic_id, poster)
            for admin in admins[:2]:
                tx = await evm.registry.functions.registerClinic(
                    clinic_bytes32(clinic_id), bytes.fromhex(reg.key_hash[2:]), poster
                ).transact({"from": admin})
                await evm.w3.eth.wait_for_transaction_receipt(tx)
            say(f"clinic {clinic_id} registered on the local chain")
    finally:
        await evm.close()
    return True


async def detect_history(report: LoadReport) -> int:
    """Fit on the loaded history, then score each past day at its end, like the daily worker."""
    assert report.first_day is not None and report.last_day is not None
    end = datetime.combine(report.last_day, time(23, 59, 59), tzinfo=TZ).astimezone(UTC)
    await detect.fit_models(end)
    raised = 0
    day = report.first_day
    while day <= report.last_day:
        at = datetime.combine(day, time(23, 59, 59), tzinfo=TZ).astimezone(UTC)
        raised += sum((await detect.score_all(at)).values())
        day += timedelta(days=1)
    return raised


async def todays_queue() -> int:
    """A few of today's appointments per clinic, booked by reception through the normal service,
    so the reception, nurse and doctor screens have work when the demo opens."""
    booked = 0
    async with tenant_session(TenantContext(role=Role.PLATFORM_ADMIN)) as db:
        clinics = (await db.scalars(select(Clinic.id).order_by(Clinic.name))).all()
    now = datetime.now(UTC)
    start = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0) + timedelta(
        minutes=30
    )
    for clinic_id in clinics:
        async with tenant_session(TenantContext(clinic_id=clinic_id, role="system")) as db:
            members = (
                await db.execute(
                    select(Membership.user_id, Membership.role)
                    .where(Membership.clinic_id == clinic_id)
                    .order_by(Membership.created_at, Membership.user_id)
                )
            ).all()
            patients = (
                await db.scalars(
                    select(Patient.id)
                    .where(Patient.clinic_id == clinic_id, Patient.user_id.is_not(None))
                    .order_by(Patient.mrn)
                )
            ).all()
            others = (
                await db.scalars(
                    select(Patient.id)
                    .where(Patient.clinic_id == clinic_id, Patient.user_id.is_(None))
                    .order_by(Patient.mrn)
                )
            ).all()
        reception = next(u for u, r in members if r == Role.RECEPTION)
        doctors = [u for u, r in members if r == Role.DOCTOR]
        principal = ClinicPrincipal(
            user_id=reception,
            session_id=uuid7(),
            token_id="demo-seed",  # noqa: S106 - a label, not a secret
            amr=("pwd", "otp"),
            clinic_id=clinic_id,
            role=Role.RECEPTION,
        )
        request = RequestInfo(request_id="demo-seed")
        for i, patient_id in enumerate([*patients, *others][:TODAY_PER_CLINIC]):
            async with tenant_session(principal.tenant()) as db:
                appointment = await clinic_service.create_appointment(
                    db,
                    principal,
                    clinic_schemas.AppointmentCreate(
                        patient_id=patient_id,
                        doctor_user_id=doctors[i % len(doctors)],
                        slot_start=start + timedelta(minutes=15 * i),
                    ),
                    request,
                )
                booked += 1
                if i < TOKENS_PER_CLINIC:
                    await clinic_service.issue_token(
                        db,
                        principal,
                        clinic_schemas.TokenCreate(appointment_id=appointment.id),
                        request,
                    )
    return booked


CASE_KINDS = {1: "snooping", 2: "snooping", 8: "break_glass", 10: "forgery"}


async def insider_cases(report: LoadReport) -> list[dict[str, Any]]:
    """Where the simulated insider accesses ended up: an alert (with its day and rank) or the
    break-glass review queue. Printed for the demo guide; the app itself never sees labels."""
    ids = [uuid.UUID(k) for k in report.attacks]
    cases: list[dict[str, Any]] = []
    async with tenant_session(TenantContext(role=Role.PLATFORM_ADMIN)) as db:
        names = dict((await db.execute(select(Clinic.id, Clinic.name))).tuples().all())
    for clinic_id, clinic in names.items():
        async with tenant_session(TenantContext(clinic_id=clinic_id, role="system")) as db:
            rows = await db.execute(
                select(
                    AccessEvent.id,
                    AccessEvent.at,
                    AccessEvent.break_glass_id,
                    User.name,
                    Patient.name,
                    Alert.day,
                    Alert.rank_in_day,
                )
                .join(User, User.id == AccessEvent.user_id)
                .join(Patient, Patient.id == AccessEvent.patient_id)
                .outerjoin(Alert, Alert.access_event_id == AccessEvent.id)
                .where(AccessEvent.clinic_id == clinic_id, AccessEvent.id.in_(ids))
                .order_by(AccessEvent.at)
            )
            for event_id, at, glass, user, patient, alerted_day, rank in rows:
                # Outer join: there is no alert row for accesses that were not alerted.
                day: date | None = alerted_day
                kind = CASE_KINDS.get(report.attacks[str(event_id)], "other")
                if day is None and glass is None:
                    continue
                cases.append(
                    {
                        "clinic": clinic,
                        "kind": kind,
                        "user": user,
                        "patient": patient,
                        "at": at.astimezone(TZ).strftime("%d %b %H:%M"),
                        "alert_day": str(day) if day else None,
                        "rank": rank,
                        "break_glass": glass is not None,
                    }
                )
    return cases


def write_logins(report: LoadReport, password: str, cases: list[dict[str, Any]]) -> str:
    folder = state_dir()
    folder.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {
        "password": password,
        "clinics": report.clinics,
        "first_day": str(report.first_day),
        "last_day": str(report.last_day),
        "patients": report.patients,
        "access_events": report.access_events,
        "attack_accesses": report.attack_accesses,
        "attack_types": report.attack_types,
        "break_glass": report.break_glass,
        "users": [asdict(x) for x in report.logins],
        "cases": cases,
    }
    (folder / "logins.json").write_bytes(json.dumps(data, indent=2).encode())
    text = render(data)
    (folder / "logins.txt").write_bytes(text.encode())
    return text


async def main() -> int:
    marker = state_dir() / "logins.json"
    if marker.exists():
        say("already seeded")
        print(render(json.loads(marker.read_text(encoding="utf-8"))))
        return 0
    password = os.environ.get("SEED_DEMO_PASSWORD", "")
    if len(password) < 10:
        raise SystemExit("SEED_DEMO_PASSWORD must be at least 10 characters")
    await run_startup_tasks()
    today = datetime.now(TZ).date()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            say("simulating two clinics for three weeks (SaathiBench)")
            simulate(load_config(CONFIG, profiles_dir()), run, workers=1)
            say("loading 50 patients and their activity")
            report = await load(run, password, today)
        say(
            f"{report.access_events} accesses from {report.first_day} to {report.last_day}, "
            f"{report.attack_accesses} of them by simulated insiders"
        )
        async with tenant_session(TenantContext(role=Role.PLATFORM_ADMIN)) as db:
            clinic_ids = list((await db.scalars(select(Clinic.id))).all())
        await register_on_chain(clinic_ids)
        say(f"detector raised {await detect_history(report)} alerts over the past days")
        say(f"booked {await todays_queue()} appointments for today")
        runs = await job.run()
        say(f"anchor job: {json.dumps([asdict(r) for r in runs or []])}")
        print(write_logins(report, password, await insider_cases(report)))
    finally:
        await get_engine().dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
