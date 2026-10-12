import json
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.clinical.notes_crypto import decrypt_body
from app.core.db import TenantContext, tenant_session
from app.db import models as m
from app.db.enums import Role
from app.ledger.checkpoint import load_tree
from hs_demo import load as loader
from hs_demo.logins import current_code, render
from saathibench.config import load_config
from saathibench.run import simulate

TODAY = date(2026, 10, 13)
PROFILES = Path(__file__).resolve().parents[3] / "sim" / "profiles"
SMALL = """
name: demo-test
seed: 3
start_date: "2026-01-05"
days: 7
timezone: Asia/Kolkata
clinics:
  - {profile: solo_gp, count: 2}
attacks:
  campaigns_per_clinic_month: 30.0
  first_day: 1
  types: [1, 2, 10]
"""


def test_week_shift_keeps_weekdays_and_ends_last_week() -> None:
    for last in (date(2026, 1, 25), date(2026, 10, 12), date(2026, 10, 5)):
        delta = loader.week_shift(last, TODAY)
        assert delta.days % 7 == 0
        assert TODAY - timedelta(days=7) <= last + delta <= TODAY - timedelta(days=1)


def test_synthetic_columns() -> None:
    items = loader.prescription_items(
        '[{"drug": "Amlodipine 5 mg"}, {"drug": "Salbutamol inhaler"}]'
    )
    assert items[0] == {
        "drug": "Amlodipine",
        "strength": "5 mg",
        "dose": "1-0-0",
        "duration_days": 30,
        "instructions_en": "Every morning",
        "instructions_hi": "हर सुबह",
    }
    assert "strength" not in items[1] and items[1]["dose"] == "2 puffs SOS"
    tests = loader.lab_tests('["CBC", "Urine routine"]')
    assert tests == [
        {"code": "CBC", "name": "Complete blood count"},
        {"code": "URINE", "name": "Urine routine"},
    ]
    values = loader.lab_values("order-1", tests)
    assert values == loader.lab_values("order-1", tests), "values are reproducible per order"
    assert {"Hb", "WBC", "Platelets", "Protein", "Sugar", "Pus cells"} == set(values)


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    folder = tmp_path_factory.mktemp("demo-sim")
    config = folder / "small.yaml"
    config.write_text(SMALL, encoding="utf-8")
    simulate(load_config(config, PROFILES), folder / "run", workers=1)
    return folder / "run"


@pytest.fixture(scope="module")
async def loaded(demo_app: str, run: Path) -> loader.LoadReport:
    return await loader.load(run, "demo-password-123", TODAY, per_clinic=8)


async def test_load_writes_a_consistent_demo(demo_app: str, loaded: loader.LoadReport) -> None:
    report = loaded
    assert report.patients == 16
    assert set(report.clinics) == {"a", "b"}
    assert report.first_day is not None and report.last_day is not None
    assert TODAY - timedelta(days=7) <= report.last_day <= TODAY - timedelta(days=1)

    engine = create_async_engine(demo_app)
    try:
        async with AsyncSession(engine) as s:
            users = (await s.scalars(select(m.User))).all()
            assert all(u.email.endswith("@demo.healthsaathi.test") for u in users)
            staff = {lg.email for lg in report.logins if lg.role != "patient"}
            assert staff and all(u.password_hash for u in users if u.email in staff), (
                "staff can sign in with the demo password"
            )
            mfa = await s.scalar(select(func.count()).select_from(m.MfaSecret))
            assert mfa == len(staff)
            roles = set((await s.scalars(select(m.Membership.role))).all())
            assert {
                Role.CLINIC_ADMIN,
                Role.RECEPTION,
                Role.NURSE,
                Role.DOCTOR,
                Role.LAB_TECH,
            } <= roles
            assert Role.PATIENT in roles

            events = await s.scalar(select(func.count()).select_from(m.AccessEvent))
            explained = await s.scalar(select(func.count()).select_from(m.AccessExplanation))
            audited = await s.scalar(
                select(func.count()).select_from(m.AuditEvent).where(m.AuditEvent.kind == "access")
            )
            assert events == explained == audited == report.access_events > 0
            latest = await s.scalar(select(func.max(m.AccessEvent.at)))
            assert latest is not None and latest.date() <= TODAY

            note = await s.scalar(select(m.ClinicalNote).limit(1))
            assert note is not None and len(decrypt_body(note.id, note.body_enc)) > 20
            rx = await s.scalar(select(m.Prescription).limit(1))
            assert rx is not None and {"drug", "dose", "duration_days"} <= set(rx.items[0])
            consent = await s.scalar(select(m.Consent).limit(1))
            assert consent is not None and len(consent.salt_hash) == 64
            clinic_ids = (await s.scalars(select(m.Clinic.id))).all()
    finally:
        await engine.dispose()

    for clinic_id in clinic_ids:
        ctx = TenantContext(clinic_id=clinic_id, role="system")
        async with tenant_session(ctx) as db:
            tree, _ = await load_tree(db, clinic_id)  # raises if the hash chain is broken
            assert tree.size > 0
            # Row level security: this clinic's session sees only its own patients.
            others = await db.scalar(
                select(func.count()).select_from(m.Patient).where(m.Patient.clinic_id != clinic_id)
            )
            assert others == 0


async def test_payloads_match_the_product_format(demo_app: str, loaded: loader.LoadReport) -> None:
    engine = create_async_engine(demo_app)
    try:
        async with AsyncSession(engine) as s:
            row = (
                await s.execute(
                    text(
                        "SELECT a.payload, e.id FROM audit_events a JOIN access_events e"
                        " ON (a.payload ->> 'access_event_id')::uuid = e.id LIMIT 1"
                    )
                )
            ).first()
    finally:
        await engine.dispose()
    assert row is not None
    payload, event_id = row
    assert payload["type"] == "access" and payload["access_event_id"] == str(event_id)
    assert {"role", "resource", "action", "decision", "template", "strength"} <= set(payload)
    assert uuid.UUID(payload["patient_id"])


def test_login_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = {
        "password": "Saathi-1234abcd",
        "clinics": {"a": "Saathi Demo Clinic A"},
        "first_day": "2026-09-20",
        "last_day": "2026-10-11",
        "patients": 50,
        "access_events": 10,
        "users": [
            {
                "clinic": "a",
                "role": "doctor",
                "name": "Dr Demo",
                "email": "doctor1.a@demo.healthsaathi.test",
                "totp_secret": "JBSWY3DPEHPK3PXP",
            },
            {
                "clinic": "a",
                "role": "patient",
                "name": "Demo Patient",
                "email": "patient1.a@demo.healthsaathi.test",
                "totp_secret": None,
            },
        ],
        "cases": [
            {
                "clinic": "Saathi Demo Clinic A",
                "kind": "forgery",
                "user": "Dr Demo",
                "patient": "Demo Patient",
                "at": "01 Oct 10:00",
                "alert_day": "2026-10-01",
                "rank": 2,
                "break_glass": False,
            }
        ],
    }
    text_out = render(data)
    assert "doctor1.a@demo.healthsaathi.test" in text_out and "JBSWY3DPEHPK3PXP" in text_out
    assert "alert on 2026-10-01, rank 2" in text_out
    assert "—" not in text_out
    (tmp_path / "logins.json").write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("DEMO_STATE_DIR", str(tmp_path))
    code = current_code("DOCTOR1.a@demo.healthsaathi.test")
    assert len(code) == 6 and code.isdigit()
    with pytest.raises(SystemExit):
        current_code("patient1.a@demo.healthsaathi.test")
