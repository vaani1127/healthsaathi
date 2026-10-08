from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.scripts.seed import EMAIL_DOMAIN, seed

COUNT_SQL = {
    "clinics": "SELECT count(*) FROM clinics WHERE name LIKE 'Saathi Demo Clinic %'",
    "patients": "SELECT count(*) FROM patients p JOIN clinics c ON c.id = p.clinic_id"
    " WHERE c.name LIKE 'Saathi Demo Clinic %'",
    "users": f"SELECT count(*) FROM users WHERE email LIKE '%@{EMAIL_DOMAIN}'",
    "notices": "SELECT count(*) FROM consent_notices n JOIN clinics c ON c.id = n.clinic_id"
    " WHERE c.name LIKE 'Saathi Demo Clinic %' AND n.version = 1",
    "services": "SELECT count(*) FROM services s JOIN clinics c ON c.id = s.clinic_id"
    " WHERE c.name LIKE 'Saathi Demo Clinic %'",
    "schedules": "SELECT count(*) FROM schedules s JOIN clinics c ON c.id = s.clinic_id"
    " WHERE c.name LIKE 'Saathi Demo Clinic %'",
    "roles": "SELECT count(DISTINCT m.role) FROM memberships m JOIN clinics c"
    " ON c.id = m.clinic_id WHERE c.name LIKE 'Saathi Demo Clinic %'",
}


async def counts(engine: AsyncEngine) -> dict[str, int]:
    async with engine.connect() as conn:
        return {k: int(await conn.scalar(text(sql)) or 0) for k, sql in COUNT_SQL.items()}


async def test_seed_creates_demo_data_and_is_idempotent(admin_engine: AsyncEngine) -> None:
    first = await seed(password="demo-password-for-tests")
    assert first.created_users > 0
    assert first.password is None

    after_first = await counts(admin_engine)
    assert after_first["clinics"] == 2
    assert after_first["patients"] == 50
    assert after_first["notices"] == 2
    assert after_first["services"] == 10
    assert after_first["schedules"] > 0
    assert after_first["roles"] == 6  # clinic_admin, reception, nurse, doctor, lab_tech, patient

    second = await seed(password="demo-password-for-tests")
    assert second.created_users == 0
    assert await counts(admin_engine) == after_first


async def test_seed_generates_a_password_when_none_given(monkeypatch: object) -> None:
    import os

    os.environ.pop("SEED_DEMO_PASSWORD", None)
    report = await seed()
    assert report.password is not None
    assert len(report.password) >= 12
