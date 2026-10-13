"""Load a SaathiBench run into the product database for the local demo.

The simulator writes the product's own table shapes (SPEC 7), so most rows are copied as they are.
This module only:

- keeps a small set of patients (every patient an attack or a break-glass access touched, then the
  most active ones) and every row that belongs to them,
- shifts every time by whole weeks so the data ends in the past week and weekdays still match,
- gives staff and patients demo emails, one demo password and staff authenticator secrets,
- fills the columns the simulator leaves out (note text, prescription doses, lab values, consent
  commitments), all synthetic,
- writes each access with the product's own explanation engine and audit chain, in time order,
  as `access.record_and_explain()` would have done at that moment.
"""

import hashlib
import json
import random
import re
import secrets
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
from sqlalchemy import Table, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.service import audit_payload
from app.clinical.notes_crypto import encrypt_body
from app.consent.service import commitment
from app.core.db import TenantContext, tenant_session
from app.db.base import Base
from app.db.enums import AccessAction, AccessDecision, ResourceType, Role
from app.db.models import AccessEvent, AuditEvent, Clinic, ConsentNotice
from app.db.models.audit import AUDIT_SEQ
from app.identity import totp
from app.identity.passwords import hash_password
from app.ledger.writer import lock_clinic
from app.scripts.seed import CONSENT_TEXT_EN, CONSENT_TEXT_HI, EMAIL_DOMAIN
from e2d_core.explain import AccessEvent as CoreAccessEvent
from e2d_core.explain import EvidenceQuery, explain
from e2d_core.ids import uuid7
from e2d_core.ledger import GENESIS, chain_hash, payload_hash
from e2d_core.repo.memory import COLUMNS as MEMORY_TABLES
from e2d_core.repo.memory import MemoryRepository
from saathibench.schema import TABLES

PATIENTS_PER_CLINIC = 25
PATIENT_LOGINS_PER_CLINIC = 3
CLINIC_NAMES = (
    ("Saathi Demo Clinic A", "Jaipur", "Rajasthan"),
    ("Saathi Demo Clinic B", "Lucknow", "Uttar Pradesh"),
)
ROLE_KEYS = {
    "clinic_admin": "admin",
    "reception": "reception",
    "nurse": "nurse",
    "doctor": "doctor",
    "lab_tech": "lab",
}
ROLE_ORDER = ("clinic_admin", "reception", "nurse", "doctor", "lab_tech")
# Dates that describe the person, not the activity, are not shifted.
UNSHIFTED = {"dob", "onset"}

# Tables that hang off a patient, in insert order, with the column that names the patient.
PATIENT_TABLES = (
    "consents",
    "appointments",
    "queue_tokens",
    "encounters",
    "vitals",
    "allergies",
    "conditions",
    "clinical_notes",
    "prescriptions",
    "lab_orders",
    "lab_results",
    "referrals",
    "care_team_assignments",
    "invoices",
    "payments",
    "break_glass_events",
)

LAB_TESTS: dict[str, tuple[str, str]] = {
    "CBC": ("CBC", "Complete blood count"),
    "Hb": ("HB", "Haemoglobin"),
    "HbA1c": ("HBA1C", "HbA1c"),
    "Lipid profile": ("LIPID", "Lipid profile"),
    "Urine routine": ("URINE", "Urine routine"),
    "Blood sugar fasting": ("FBS", "Blood sugar fasting"),
}

# Synthetic value ranges: (label, unit, low, high, decimals) per test code.
LAB_VALUES: dict[str, list[tuple[str, str, float, float, int]]] = {
    "CBC": [
        ("Hb", "g/dL", 10.5, 15.5, 1),
        ("WBC", "10^3/uL", 4.0, 11.5, 1),
        ("Platelets", "10^3/uL", 150, 420, 0),
    ],
    "HB": [("Hb", "g/dL", 9.5, 15.5, 1)],
    "HBA1C": [("HbA1c", "%", 5.2, 9.4, 1)],
    "LIPID": [
        ("Total cholesterol", "mg/dL", 150, 260, 0),
        ("LDL", "mg/dL", 80, 180, 0),
        ("HDL", "mg/dL", 32, 62, 0),
        ("Triglycerides", "mg/dL", 90, 240, 0),
    ],
    "FBS": [("Fasting glucose", "mg/dL", 82, 168, 0)],
}

# Synthetic dosing for the simulator's medicines: dose, days, instructions in English and Hindi.
DOSING: dict[str, tuple[str, int, str, str]] = {
    "Paracetamol": ("1-1-1", 3, "After food, only if fever", "खाने के बाद, केवल बुखार होने पर"),
    "Amoxicillin": ("1-1-1", 5, "After food, finish the course", "खाने के बाद, पूरा कोर्स करें"),
    "Cetirizine": ("0-0-1", 5, "At bedtime", "सोते समय"),
    "Pantoprazole": ("1-0-0", 14, "Before breakfast", "नाश्ते से पहले"),
    "Amlodipine": ("1-0-0", 30, "Every morning", "हर सुबह"),
    "Metformin": ("1-0-1", 30, "After food", "खाने के बाद"),
    "Salbutamol inhaler": ("2 puffs SOS", 30, "When breathless", "साँस फूलने पर"),
    "Iron and folic acid": ("0-1-0", 30, "After lunch", "दोपहर के खाने के बाद"),
    "Alprazolam": ("0-0-1", 7, "At bedtime", "सोते समय"),
}

NOTE_TEXT = {
    "hypertension": "Follow-up for hypertension. BP reviewed, no chest pain or headache. "
    "Continue current medicine, reduce salt. Review in 4 weeks.",
    "type 2 diabetes": "Follow-up for type 2 diabetes. No low sugar episodes. Diet advice given. "
    "Continue metformin. Check HbA1c at next visit.",
    "asthma": "Asthma review. Night cough twice a week. Inhaler technique checked. "
    "Continue reliever as needed.",
}
ACUTE_NOTES = (
    "Fever and body ache for 2 days. Throat mildly red, chest clear. Rest and fluids advised.",
    "Cough and cold for 4 days. No breathlessness. Chest clear. Symptomatic treatment.",
    "Acidity and upper abdominal discomfort after meals. Soft abdomen. Diet advice given.",
    "Itchy rash on forearms for a week. No fever. Antihistamine started.",
    "Routine check. No complaints. Vitals normal. Advised yearly blood tests.",
)


@dataclass
class Login:
    clinic: str
    role: str
    name: str
    email: str
    totp_secret: str | None = None


@dataclass
class LoadReport:
    clinics: dict[str, str] = field(default_factory=dict)
    patients: int = 0
    access_events: int = 0
    attack_accesses: int = 0
    attack_types: dict[str, list[int]] = field(default_factory=dict)
    break_glass: int = 0
    # Ground truth from the simulator, used only to point the demo guide at the insider cases.
    attacks: dict[str, int] = field(default_factory=dict)
    logins: list[Login] = field(default_factory=list)
    first_day: date | None = None
    last_day: date | None = None


def read_run(run: Path) -> tuple[dict[str, pl.DataFrame], pl.DataFrame, pl.DataFrame]:
    tables = {p.name: pl.read_parquet(p) for p in sorted((run / "tables").iterdir())}
    # The simulator writes no folder for a table without rows.
    for name, schema in TABLES.items():
        tables.setdefault(name, pl.DataFrame(schema=schema))
    labels = pl.read_parquet(run / "labels" / "access_labels")
    campaigns = pl.read_parquet(run / "labels" / "campaigns")
    return tables, labels, campaigns


def week_shift(last_day: date, today: date) -> timedelta:
    """Whole weeks that move `last_day` into the 7 days before `today`."""
    gap = (today - timedelta(days=1) - last_day).days
    return timedelta(days=7 * (gap // 7))


def shift_frame(frame: pl.DataFrame, delta: timedelta) -> pl.DataFrame:
    shifted = []
    for name, dtype in frame.schema.items():
        if name in UNSHIFTED:
            continue
        if isinstance(dtype, pl.Datetime) or dtype == pl.Date():
            shifted.append((pl.col(name) + delta).alias(name))
    return frame.with_columns(shifted) if shifted else frame


def choose_patients(
    tables: dict[str, pl.DataFrame], labels: pl.DataFrame, per_clinic: int
) -> dict[str, list[str]]:
    """Per clinic: patients an attack or a break-glass access touched, then patients with a portal
    account, then the most accessed ones."""
    events = tables["access_events"]
    attacked = (
        events.join(labels.filter(pl.col("is_attack")), left_on="id", right_on="access_event_id")
        .select("clinic_id", "patient_id")
        .unique()
    )
    glass = tables["break_glass_events"].select("clinic_id", "patient_id").unique()
    counts = events.group_by("clinic_id", "patient_id").len()
    portal = tables["patients"].filter(pl.col("user_id").is_not_null()).select("id")
    chosen: dict[str, list[str]] = {}
    for clinic_id in sorted(tables["clinics"]["id"].to_list()):
        picked: list[str] = []

        def add(ids: Iterable[str], limit: int, picked: list[str] = picked) -> None:
            for pid in ids:
                if len(picked) >= limit:
                    return
                if pid not in picked:
                    picked.append(pid)

        mine = counts.filter(pl.col("clinic_id") == clinic_id).sort(
            ["len", "patient_id"], descending=[True, False]
        )
        add(attacked.filter(pl.col("clinic_id") == clinic_id)["patient_id"].sort(), per_clinic)
        add(glass.filter(pl.col("clinic_id") == clinic_id)["patient_id"].sort(), per_clinic)
        with_portal = mine.filter(pl.col("patient_id").is_in(portal["id"].implode()))
        add(with_portal["patient_id"].head(PATIENT_LOGINS_PER_CLINIC), per_clinic)
        add(mine["patient_id"], per_clinic)
        chosen[clinic_id] = picked
    return chosen


def subset(tables: dict[str, pl.DataFrame], keep: set[str]) -> dict[str, pl.DataFrame]:
    """Every table cut down to the kept patients; staff tables stay whole."""
    out = dict(tables)
    ids = pl.Series(sorted(keep))
    out["patients"] = tables["patients"].filter(pl.col("id").is_in(ids.implode()))
    for name in PATIENT_TABLES:
        if "patient_id" in tables[name].columns:
            out[name] = tables[name].filter(pl.col("patient_id").is_in(ids.implode()))
    appointments = out["appointments"]["id"].implode()
    out["queue_tokens"] = tables["queue_tokens"].filter(
        pl.col("appointment_id").is_in(appointments)
    )
    out["payments"] = tables["payments"].filter(
        pl.col("invoice_id").is_in(out["invoices"]["id"].implode())
    )
    out["lab_results"] = tables["lab_results"].filter(
        pl.col("lab_order_id").is_in(out["lab_orders"]["id"].implode())
    )
    out["access_events"] = tables["access_events"].filter(pl.col("patient_id").is_in(ids.implode()))
    entities = pl.concat(
        [out[t]["id"] for t in ("appointments", "lab_orders", "referrals", "invoices")]
    ).implode()
    out["status_events"] = tables["status_events"].filter(pl.col("entity_id").is_in(entities))
    staff = tables["memberships"]["user_id"]
    patient_users = out["patients"]["user_id"].drop_nulls()
    users = pl.concat([staff, patient_users]).unique().implode()
    out["users"] = tables["users"].filter(pl.col("id").is_in(users))
    out["devices"] = tables["devices"].filter(pl.col("user_id").is_in(users))
    out["sessions"] = tables["sessions"].filter(pl.col("user_id").is_in(users))
    return out


# Row conversion ----------------------------------------------------------------------------------


def _rows(table: Table, frame: pl.DataFrame) -> list[dict[str, Any]]:
    """Polars rows to values the table's column types accept."""
    columns = [c for c in frame.columns if c in table.c]
    uuid_cols = {c for c in columns if table.c[c].type.python_type is uuid.UUID}
    rows = []
    for row in frame.select(columns).iter_rows(named=True):
        for name in uuid_cols:
            if row[name] is not None:
                row[name] = uuid.UUID(row[name])
        rows.append(row)
    return rows


def _strength(drug: str) -> tuple[str, str | None]:
    match = re.match(r"^(.*?)\s+(\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml))$", drug)
    return (match.group(1), match.group(2)) if match else (drug, None)


def prescription_items(raw: str) -> list[dict[str, Any]]:
    items = []
    for item in json.loads(raw):
        name, strength = _strength(str(item["drug"]))
        dose, days, en, hi = DOSING.get(name, ("1-0-1", 5, "After food", "खाने के बाद"))
        items.append(
            {
                "drug": name,
                **({"strength": strength} if strength else {}),
                "dose": dose,
                "duration_days": days,
                "instructions_en": en,
                "instructions_hi": hi,
            }
        )
    return items


def lab_tests(raw: str) -> list[dict[str, str]]:
    out = []
    for name in json.loads(raw):
        code, label = LAB_TESTS.get(name, (name.upper().replace(" ", "_")[:16], name))
        out.append({"code": code, "name": label})
    return out


def lab_values(order_id: str, tests: list[dict[str, str]]) -> dict[str, Any]:
    rng = random.Random(order_id)  # noqa: S311 - synthetic values, reproducible per order
    values: dict[str, Any] = {}
    for test in tests:
        if test["code"] == "URINE":
            values.update(
                {
                    "Protein": {"value": "Nil", "unit": ""},
                    "Sugar": {"value": "Nil", "unit": ""},
                    "Pus cells": {
                        "value": f"{rng.randint(1, 4)}-{rng.randint(5, 6)}",
                        "unit": "/hpf",
                    },
                }
            )
            continue
        for label, unit, low, high, decimals in LAB_VALUES.get(test["code"], []):
            value = round(rng.uniform(low, high), decimals)
            values[label] = {"value": int(value) if decimals == 0 else value, "unit": unit}
    return values


def note_text(note_id: str, conditions: list[str]) -> str:
    rng = random.Random(note_id)  # noqa: S311 - synthetic text choice
    for condition in conditions:
        if condition in NOTE_TEXT and rng.random() < 0.7:
            return NOTE_TEXT[condition]
    return rng.choice(ACUTE_NOTES)


# Loading ------------------------------------------------------------------------------------------


async def _insert(db: AsyncSession, name: str, rows: list[dict[str, Any]]) -> None:
    if rows:
        await db.execute(insert(Base.metadata.tables[name]), rows)


def _table(name: str) -> Table:
    return Base.metadata.tables[name]


def assign_emails(tables: dict[str, pl.DataFrame], codes: dict[str, str]) -> dict[str, Login]:
    """Demo email for each staff member and each patient account, by clinic and role."""
    logins: dict[str, Login] = {}
    names = dict(zip(tables["users"]["id"], tables["users"]["name"], strict=True))
    memberships = tables["memberships"].sort(["clinic_id", "role", "created_at", "user_id"])
    for clinic_id, code in codes.items():
        mine = memberships.filter(pl.col("clinic_id") == clinic_id)
        for role in ROLE_ORDER:
            users = mine.filter(pl.col("role") == role)["user_id"].to_list()
            for n, user_id in enumerate(users, start=1):
                key = ROLE_KEYS[role] + (str(n) if len(users) > 1 else "")
                logins[user_id] = Login(
                    code, role, names[user_id], f"{key}.{code}@{EMAIL_DOMAIN}", totp.new_secret()
                )
        patients = (
            tables["patients"]
            .filter((pl.col("clinic_id") == clinic_id) & pl.col("user_id").is_not_null())
            .sort("mrn")
        )
        n = 0
        for user_id in patients["user_id"]:
            if user_id in logins:
                continue  # a staff member who is also a patient keeps the staff email
            n += 1
            logins[user_id] = Login(
                code, "patient", names[user_id], f"patient{n}.{code}@{EMAIL_DOMAIN}"
            )
    return logins


async def load(
    run: Path, password: str, today: date, per_clinic: int = PATIENTS_PER_CLINIC
) -> LoadReport:
    tables, labels, campaigns = read_run(run)
    report = LoadReport()
    chosen = choose_patients(tables, labels, per_clinic)
    keep = {pid for ids in chosen.values() for pid in ids}
    tables = subset(tables, keep)
    last = tables["access_events"]["at"].max()
    assert isinstance(last, datetime)
    delta = week_shift(last.date(), today)
    tables = {name: shift_frame(frame, delta) for name, frame in tables.items()}
    labels_kept = labels.filter(
        pl.col("access_event_id").is_in(tables["access_events"]["id"].implode())
    )

    clinic_ids = sorted(tables["clinics"]["id"].to_list())
    codes = {cid: "ab"[i] for i, cid in enumerate(clinic_ids)}
    logins = assign_emails(tables, codes)
    password_hash = hash_password(password)

    # Users, devices, sessions (no clinic) -------------------------------------------------------
    users = tables["users"].with_columns(
        pl.col("id")
        .replace_strict({k: v.email for k, v in logins.items()}, default=None)
        .alias("demo_email")
    )
    async with tenant_session(TenantContext(role=Role.PLATFORM_ADMIN)) as db:
        user_rows = _rows(_table("users"), users)
        for row in user_rows:
            login = logins.get(str(row["id"]))
            row["email"] = login.email if login else f"user.{row['id'].hex[:12]}@{EMAIL_DOMAIN}"
            row["password_hash"] = password_hash if login and login.role != "patient" else None
        await _insert(db, "users", user_rows)
        await _insert(db, "devices", _rows(_table("devices"), tables["devices"]))
        session_rows = _rows(_table("sessions"), tables["sessions"])
        for row in session_rows:
            row["refresh_hash"] = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        await _insert(db, "sessions", session_rows)
        mfa = []
        for user_id, login in logins.items():
            if login.totp_secret:
                mfa.append(
                    {
                        "user_id": uuid.UUID(user_id),
                        "totp_secret_enc": totp.encrypt_secret(login.totp_secret, user_id),
                        "enabled_at": datetime.now(UTC),
                    }
                )
        await _insert(db, "mfa_secrets", mfa)
        for i, cid in enumerate(clinic_ids):
            name, city, state = CLINIC_NAMES[i % len(CLINIC_NAMES)]
            created = tables["clinics"].filter(pl.col("id") == cid)["created_at"][0]
            db.add(Clinic(id=uuid.UUID(cid), name=name, city=city, state=state, created_at=created))
            report.clinics[codes[cid]] = name

    # Clinic tables -------------------------------------------------------------------------------
    for cid in clinic_ids:
        await _load_clinic(cid, tables, logins)

    # Accesses, in time order, explained and chained -------------------------------------------
    events = tables["access_events"].sort(["clinic_id", "at", "id"])
    repo = MemoryRepository({k: v for k, v in tables.items() if k in MEMORY_TABLES})
    for cid in clinic_ids:
        await _load_accesses(cid, events.filter(pl.col("clinic_id") == cid), repo)

    attack_ids = labels_kept.filter(pl.col("is_attack"))
    report.attacks = {
        str(k): int(v) for k, v in attack_ids.select("access_event_id", "attack_type").iter_rows()
    }
    report.patients = len(keep)
    report.access_events = events.height
    report.attack_accesses = attack_ids.height
    kept_campaigns = campaigns.filter(
        pl.col("campaign_id").is_in(attack_ids["campaign_id"].drop_nulls().implode())
    )
    for cid in clinic_ids:
        report.attack_types[codes[cid]] = sorted(
            kept_campaigns.filter(pl.col("clinic_id") == cid)["attack_type"].to_list()
        )
    report.break_glass = tables["break_glass_events"].height
    report.logins = sorted(
        logins.values(),
        key=lambda x: (x.clinic, ROLE_ORDER.index(x.role) if x.role in ROLE_ORDER else 9, x.email),
    )
    report.first_day = tables["access_events"]["at"].min().date()  # type: ignore[union-attr]
    report.last_day = tables["access_events"]["at"].max().date()  # type: ignore[union-attr]
    return report


async def _load_clinic(cid: str, tables: dict[str, pl.DataFrame], logins: dict[str, Login]) -> None:
    clinic_id = uuid.UUID(cid)

    def mine(name: str) -> pl.DataFrame:
        return tables[name].filter(pl.col("clinic_id") == cid)

    ctx = TenantContext(clinic_id=clinic_id, role="system")
    async with tenant_session(ctx) as db:
        memberships = mine("memberships")
        await _insert(db, "memberships", _rows(_table("memberships"), memberships))
        # Patient accounts sign in through a patient membership, as portal access creates it.
        portal = mine("patients").filter(pl.col("user_id").is_not_null())
        await _insert(
            db,
            "memberships",
            [
                {
                    "clinic_id": clinic_id,
                    "user_id": uuid.UUID(u),
                    "role": Role.PATIENT,
                    "created_at": c,
                }
                for u, c in zip(portal["user_id"], portal["created_at"], strict=True)
            ],
        )
        for name in ("staff_profiles", "services", "schedules", "shifts"):
            await _insert(db, name, _rows(_table(name), mine(name)))

        consents = mine("consents")
        first_grant = consents["granted_at"].min() if consents.height else None
        for notice_id in consents["notice_id"].unique().sort():
            db.add(
                ConsentNotice(
                    id=uuid.UUID(notice_id),
                    clinic_id=clinic_id,
                    version=1,
                    text_en=CONSENT_TEXT_EN,
                    text_hi=CONSENT_TEXT_HI,
                    purposes=["treatment", "billing", "access_audit"],
                    published_at=first_grant,
                )
            )
        await _insert(db, "patients", _rows(_table("patients"), mine("patients")))
        await db.flush()
        consent_rows = _rows(_table("consents"), consents)
        for row in consent_rows:
            row["salt_hash"] = commitment(row["id"], row["patient_id"], row["notice_id"])
        await _insert(db, "consents", consent_rows)

        for name in (
            "appointments",
            "queue_tokens",
            "encounters",
            "vitals",
            "allergies",
            "conditions",
        ):
            await _insert(db, name, _rows(_table(name), mine(name)))

        conditions: dict[str, list[str]] = {}
        for pid, text in mine("conditions").select("patient_id", "text").iter_rows():
            conditions.setdefault(pid, []).append(text)
        notes = _rows(_table("clinical_notes"), mine("clinical_notes"))
        for row in notes:
            body = note_text(str(row["id"]), conditions.get(str(row["patient_id"]), []))
            row["body_enc"] = encrypt_body(row["id"], body)
        await _insert(db, "clinical_notes", notes)

        rx = mine("prescriptions")
        rx_rows = _rows(_table("prescriptions"), rx)
        for row, raw in zip(rx_rows, rx["items"], strict=True):
            row["items"] = prescription_items(raw)
        await _insert(db, "prescriptions", rx_rows)

        orders = mine("lab_orders")
        order_rows = _rows(_table("lab_orders"), orders)
        tests_by_order: dict[str, list[dict[str, str]]] = {}
        for row, raw in zip(order_rows, orders["tests"], strict=True):
            row["tests"] = lab_tests(raw)
            tests_by_order[str(row["id"])] = row["tests"]
        await _insert(db, "lab_orders", order_rows)
        results = _rows(_table("lab_results"), mine("lab_results"))
        for row in results:
            row["values"] = lab_values(
                str(row["lab_order_id"]), tests_by_order.get(str(row["lab_order_id"]), [])
            )
        await _insert(db, "lab_results", results)

        for name in ("referrals", "care_team_assignments", "invoices"):
            await _insert(db, name, _rows(_table(name), mine(name)))
        services = mine("services").sort("price_paise")
        items = []
        for inv in mine("invoices").iter_rows(named=True):
            match = services.filter(pl.col("price_paise") == inv["total_paise"])
            service = (match if match.height else services).row(0, named=True)
            items.append(
                {
                    "clinic_id": clinic_id,
                    "invoice_id": uuid.UUID(inv["id"]),
                    "service_id": uuid.UUID(service["id"]),
                    "qty": 1,
                    "price_paise": inv["total_paise"],
                }
            )
        await _insert(db, "invoice_items", items)
        for name in ("payments", "break_glass_events"):
            await _insert(db, name, _rows(_table(name), mine(name)))
        status = _rows(_table("status_events"), mine("status_events"))
        for row in status:
            row["id"] = uuid7()
        await _insert(db, "status_events", status)
    del logins  # emails were applied to the users table already


BATCH = 500


async def _load_accesses(cid: str, events: pl.DataFrame, repo: MemoryRepository) -> None:
    """Explain each access with the shared engine over the loaded rows (the in-memory backend,
    tested to give the same evidence as the SQL one), then write the access events, their
    explanations and the audit chain in batches. Per-access round trips would make a remote
    database (Neon) take most of an hour."""
    clinic_id = uuid.UUID(cid)
    timezone = "Asia/Kolkata"
    access_rows: list[dict[str, Any]] = []
    explanation_rows: list[dict[str, Any]] = []
    payloads: list[dict[str, Any]] = []
    for row in events.iter_rows(named=True):
        event = AccessEvent(
            id=uuid.UUID(row["id"]),
            clinic_id=clinic_id,
            user_id=uuid.UUID(row["user_id"]),
            role=Role(row["role"]),
            patient_id=uuid.UUID(row["patient_id"]),
            resource_type=ResourceType(row["resource_type"]),
            action=AccessAction(row["action"]),
            at=row["at"],
            session_id=uuid.UUID(row["session_id"]) if row["session_id"] else None,
            device_id=uuid.UUID(row["device_id"]) if row["device_id"] else None,
            ip_hash=row["ip_hash"],
            request_id=row["request_id"],
            policy_version=row["policy_version"],
            decision=AccessDecision(row["decision"]),
            break_glass_id=uuid.UUID(row["break_glass_id"]) if row["break_glass_id"] else None,
        )
        core = CoreAccessEvent(
            id=event.id,
            clinic_id=clinic_id,
            user_id=event.user_id,
            role=event.role.value,
            patient_id=event.patient_id,
            resource=event.resource_type.value,
            action=event.action.value,
            at=event.at,
            break_glass_id=event.break_glass_id,
        )
        bundle = repo.evidence_for_sync(
            EvidenceQuery(
                clinic_id,
                event.user_id,
                event.patient_id,
                event.at,
                timezone,
                role=event.role.value,
            )
        )
        explanation = explain(core, bundle, timezone=timezone)
        access_rows.append({c.name: getattr(event, c.name) for c in AccessEvent.__table__.columns})
        explanation_rows.append(
            {
                "access_event_id": event.id,
                "clinic_id": clinic_id,
                "template_code": explanation.template_code,
                "evidence": explanation.evidence_json(),
                "strength": explanation.strength,
                "forgery_flags": explanation.forgery_flags,
            }
        )
        payloads.append(audit_payload(event, explanation))

    ctx = TenantContext(clinic_id=clinic_id, role="system")
    async with tenant_session(ctx) as db:
        await lock_clinic(db, "audit-chain", clinic_id)
        prev = await db.scalar(
            select(AuditEvent.chain_hash)
            .where(AuditEvent.clinic_id == clinic_id)
            .order_by(AuditEvent.seq.desc())
            .limit(1)
        )
        prev = prev if prev is not None else GENESIS
        series = func.generate_series(1, len(payloads))
        seqs = (await db.scalars(select(AUDIT_SEQ.next_value()).select_from(series))).all()
        audit_rows = []
        for seq, payload in zip(sorted(seqs), payloads, strict=True):
            digest = payload_hash(payload)
            link = chain_hash(prev, digest, seq, clinic_id)
            audit_rows.append(
                {
                    "seq": seq,
                    "id": uuid7(),
                    "clinic_id": clinic_id,
                    "kind": "access",
                    "payload": payload,
                    "payload_hash": digest,
                    "prev_hash": prev,
                    "chain_hash": link,
                }
            )
            prev = link
        for table, rows in (
            ("access_events", access_rows),
            ("access_explanations", explanation_rows),
            ("audit_events", audit_rows),
        ):
            for i in range(0, len(rows), BATCH):
                await _insert(db, table, rows[i : i + BATCH])
