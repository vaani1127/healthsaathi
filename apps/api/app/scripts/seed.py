"""Load two synthetic demo clinics. Safe to run more than once.

All names are made up and end in "Demo". Emails use the reserved `.test` domain. The demo password
comes from SEED_DEMO_PASSWORD; if it is not set, a random one is generated and printed once.

Writes go through the normal app_rw role with a tenant context, so the seed also exercises the
row level security policies.
"""

import asyncio
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import TenantContext, get_engine, tenant_session
from app.db.enums import Role, Sex
from app.db.models import (
    Clinic,
    ConsentNotice,
    Membership,
    Patient,
    Schedule,
    Service,
    StaffProfile,
    User,
)
from app.identity.passwords import hash_password

EMAIL_DOMAIN = "demo.healthsaathi.test"
PATIENTS_PER_CLINIC = 25
PATIENT_ACCOUNTS_PER_CLINIC = 3

FIRST_NAMES = (
    "Asha", "Ravi", "Meena", "Arjun", "Kavita", "Sanjay", "Pooja", "Vikram", "Lata", "Imran",
    "Neha", "Gurpreet", "Farah", "Deepak", "Anita", "Rohit", "Sunita", "Manoj", "Priya", "Karan",
    "Rekha", "Tarun", "Nisha", "Harish", "Zoya",
)  # fmt: skip

CONSENT_TEXT_EN = (
    "This clinic records your health information to treat you, to bill you, and to keep a log of "
    "every staff member who opens your record. You can see that log and withdraw consent at any "
    "time from the patient portal. Withdrawing consent does not delete records the clinic must "
    "keep by law."
)
CONSENT_TEXT_HI = (
    "यह क्लिनिक आपके इलाज, बिलिंग और आपका रिकॉर्ड खोलने वाले हर स्टाफ सदस्य का लॉग रखने के लिए "
    "आपकी स्वास्थ्य जानकारी दर्ज करता है। आप यह लॉग देख सकते हैं और पेशेंट पोर्टल से कभी भी सहमति वापस ले "
    "सकते हैं। सहमति वापस लेने से वे रिकॉर्ड नहीं हटते जिन्हें क्लिनिक को कानून के अनुसार रखना होता है।"
)

SERVICES = (
    ("Consultation", 30000),
    ("Follow-up consultation", 20000),
    ("Complete blood count", 25000),
    ("Random blood sugar", 8000),
    ("Dressing", 10000),
)


@dataclass(frozen=True)
class ClinicSpec:
    code: str
    name: str
    city: str
    state: str


CLINICS = (
    ClinicSpec("a", "Saathi Demo Clinic A", "Jaipur", "Rajasthan"),
    ClinicSpec("b", "Saathi Demo Clinic B", "Lucknow", "Uttar Pradesh"),
)

STAFF = (
    (Role.CLINIC_ADMIN, "admin", "Admin"),
    (Role.RECEPTION, "reception", "Reception"),
    (Role.NURSE, "nurse", "Nurse"),
    (Role.DOCTOR, "doctor1", "Doctor One"),
    (Role.DOCTOR, "doctor2", "Doctor Two"),
    (Role.LAB_TECH, "lab", "Lab Tech"),
)


@dataclass
class SeedReport:
    created_users: int = 0
    password: str | None = None


async def get_or_create_user(
    session: AsyncSession, email: str, name: str, password_hash: str, report: SeedReport
) -> User:
    user = await session.scalar(select(User).where(func.lower(User.email) == email.lower()))
    if user is None:
        user = User(email=email, name=name, password_hash=password_hash)
        session.add(user)
        await session.flush()
        report.created_users += 1
    return user


async def ensure_membership(
    session: AsyncSession, clinic_id: uuid.UUID, user_id: uuid.UUID, role: Role
) -> None:
    exists = await session.scalar(
        select(Membership.id).where(
            Membership.clinic_id == clinic_id,
            Membership.user_id == user_id,
            Membership.role == role,
        )
    )
    if exists is None:
        session.add(Membership(clinic_id=clinic_id, user_id=user_id, role=role))


async def seed_clinic(
    spec: ClinicSpec, platform_admin: User, password_hash: str, report: SeedReport
) -> None:
    async with tenant_session(
        TenantContext(user_id=platform_admin.id, role=Role.PLATFORM_ADMIN)
    ) as session:
        clinic = await session.scalar(select(Clinic).where(Clinic.name == spec.name))
        if clinic is None:
            clinic = Clinic(name=spec.name, city=spec.city, state=spec.state)
            session.add(clinic)
            await session.flush()
        clinic_id = clinic.id

    ctx = TenantContext(clinic_id=clinic_id, user_id=platform_admin.id, role=Role.PLATFORM_ADMIN)
    async with tenant_session(ctx) as session:
        staff: dict[str, User] = {}
        for role, key, label in STAFF:
            user = await get_or_create_user(
                session,
                f"{key}.{spec.code}@{EMAIL_DOMAIN}",
                f"{label} {spec.code.upper()} Demo",
                password_hash,
                report,
            )
            staff[key] = user
            await ensure_membership(session, clinic_id, user.id, role)
            profile = await session.scalar(
                select(StaffProfile.id).where(
                    StaffProfile.clinic_id == clinic_id, StaffProfile.user_id == user.id
                )
            )
            if profile is None:
                session.add(
                    StaffProfile(
                        clinic_id=clinic_id,
                        user_id=user.id,
                        registration_no=f"DEMO-{spec.code.upper()}-{key.upper()}"
                        if role == Role.DOCTOR
                        else None,
                        specialization="General medicine" if role == Role.DOCTOR else None,
                        department="OPD",
                    )
                )

        reception = staff["reception"]
        await seed_patients(session, spec, clinic_id, reception.id, password_hash, report)
        await seed_schedules(session, clinic_id, [staff["doctor1"], staff["doctor2"]], reception.id)
        await seed_services(session, clinic_id)
        await seed_consent_notice(session, clinic_id)


async def seed_patients(
    session: AsyncSession,
    spec: ClinicSpec,
    clinic_id: uuid.UUID,
    created_by: uuid.UUID,
    password_hash: str,
    report: SeedReport,
) -> None:
    sexes = (Sex.FEMALE, Sex.MALE)
    for i in range(PATIENTS_PER_CLINIC):
        mrn = f"{spec.code.upper()}-{i + 1:04d}"
        existing = await session.scalar(
            select(Patient.id).where(Patient.clinic_id == clinic_id, Patient.mrn == mrn)
        )
        if existing is not None:
            continue
        name = f"{FIRST_NAMES[i % len(FIRST_NAMES)]} Demo"
        user_id = None
        if i < PATIENT_ACCOUNTS_PER_CLINIC:
            user = await get_or_create_user(
                session,
                f"patient{i + 1:02d}.{spec.code}@{EMAIL_DOMAIN}",
                name,
                password_hash,
                report,
            )
            user_id = user.id
            await ensure_membership(session, clinic_id, user.id, Role.PATIENT)
        session.add(
            Patient(
                clinic_id=clinic_id,
                user_id=user_id,
                mrn=mrn,
                name=name,
                dob=date(1950 + (i * 3) % 60, 1 + i % 12, 1 + i % 28),
                sex=sexes[i % 2],
                phone=f"+91-00000-{i:05d}",
                address=f"{i + 1} Demo Street, {spec.city}",
                created_by=created_by,
            )
        )


async def seed_schedules(
    session: AsyncSession, clinic_id: uuid.UUID, doctors: list[User], created_by: uuid.UUID
) -> None:
    sessions_per_day = ((time(9, 0), time(13, 0)), (time(17, 0), time(20, 0)))
    for doctor in doctors:
        has_schedule = await session.scalar(
            select(Schedule.id).where(
                Schedule.clinic_id == clinic_id, Schedule.doctor_user_id == doctor.id
            )
        )
        if has_schedule is not None:
            continue
        for weekday in range(6):  # Monday to Saturday
            for start, end in sessions_per_day:
                session.add(
                    Schedule(
                        clinic_id=clinic_id,
                        doctor_user_id=doctor.id,
                        weekday=weekday,
                        start_time=start,
                        end_time=end,
                        slot_minutes=15,
                        created_by=created_by,
                    )
                )


async def seed_services(session: AsyncSession, clinic_id: uuid.UUID) -> None:
    for name, price in SERVICES:
        exists = await session.scalar(
            select(Service.id).where(Service.clinic_id == clinic_id, Service.name == name)
        )
        if exists is None:
            session.add(Service(clinic_id=clinic_id, name=name, price_paise=price))


async def seed_consent_notice(session: AsyncSession, clinic_id: uuid.UUID) -> None:
    exists = await session.scalar(
        select(ConsentNotice.id).where(
            ConsentNotice.clinic_id == clinic_id, ConsentNotice.version == 1
        )
    )
    if exists is None:
        session.add(
            ConsentNotice(
                clinic_id=clinic_id,
                version=1,
                text_en=CONSENT_TEXT_EN,
                text_hi=CONSENT_TEXT_HI,
                purposes=["treatment", "billing", "access_audit"],
                published_at=datetime.now(UTC) - timedelta(days=1),
            )
        )


async def seed(password: str | None = None) -> SeedReport:
    report = SeedReport()
    password = password or os.environ.get("SEED_DEMO_PASSWORD")
    if not password:
        password = secrets.token_urlsafe(12)
        report.password = password
    password_hash = hash_password(password)

    async with tenant_session(TenantContext(role=Role.PLATFORM_ADMIN)) as session:
        platform_admin = await get_or_create_user(
            session,
            f"platform.admin@{EMAIL_DOMAIN}",
            "Platform Admin Demo",
            password_hash,
            report,
        )

    for spec in CLINICS:
        await seed_clinic(spec, platform_admin, password_hash, report)
    return report


async def main() -> None:
    report = await seed()
    await get_engine().dispose()
    if report.created_users == 0:
        print("Demo data already present. Nothing new was created.")
        return
    print(f"Created {report.created_users} demo users with emails ending in @{EMAIL_DOMAIN}.")
    if report.password:
        print(f"Generated demo password (shown only now): {report.password}")
    else:
        print("Demo password: the value of SEED_DEMO_PASSWORD.")


if __name__ == "__main__":
    asyncio.run(main())
