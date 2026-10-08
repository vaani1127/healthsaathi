"""Create a fresh clinic with staff for browser tests and write their logins to a JSON file.

Only runs when APP_ENV is local or test. Every staff user has a known password and an enabled
TOTP secret, so tests can sign in without the enrolment screens. Names are synthetic.

    uv run python -m app.scripts.e2e_fixture path/to/fixture.json
"""

import asyncio
import json
import secrets
import sys
from datetime import UTC, datetime, time
from pathlib import Path

from app.core.config import get_settings
from app.core.db import TenantContext, get_engine, tenant_session
from app.db.enums import Role
from app.db.models import Clinic, ConsentNotice, Membership, MfaSecret, Schedule, Service, User
from app.identity import totp
from app.identity.passwords import hash_password
from app.scripts.seed import CONSENT_TEXT_EN, CONSENT_TEXT_HI, SERVICES

ROLES = (Role.CLINIC_ADMIN, Role.RECEPTION, Role.NURSE, Role.DOCTOR, Role.LAB_TECH)


async def build(path: Path) -> dict[str, object]:
    tag = secrets.token_hex(3)
    password = secrets.token_urlsafe(12)
    async with tenant_session(TenantContext(role=Role.PLATFORM_ADMIN)) as db:
        clinic = Clinic(name=f"E2E Demo Clinic {tag}", city="Jaipur", state="Rajasthan")
        db.add(clinic)
        await db.flush()
        clinic_id = clinic.id

    users = []
    ctx = TenantContext(clinic_id=clinic_id, role=Role.PLATFORM_ADMIN)
    async with tenant_session(ctx) as db:
        for role in ROLES:
            secret = totp.new_secret()
            email = f"{role.value}.{tag}@e2e.healthsaathi.test"
            user = User(
                email=email,
                name=f"{role.value.replace('_', ' ').title()} {tag.upper()} Demo",
                password_hash=hash_password(password),
            )
            db.add(user)
            await db.flush()
            db.add(Membership(clinic_id=clinic_id, user_id=user.id, role=role))
            db.add(
                MfaSecret(
                    user_id=user.id,
                    totp_secret_enc=totp.encrypt_secret(secret, str(user.id)),
                    enabled_at=datetime.now(UTC),
                )
            )
            if role == Role.DOCTOR:
                for weekday in range(7):
                    db.add(
                        Schedule(
                            clinic_id=clinic_id,
                            doctor_user_id=user.id,
                            weekday=weekday,
                            start_time=time(0, 0),
                            end_time=time(23, 59),
                            slot_minutes=15,
                            created_by=user.id,
                        )
                    )
            users.append(
                {
                    "role": role.value,
                    "email": email,
                    "name": user.name,
                    "user_id": str(user.id),
                    "totp_secret": secret,
                }
            )
        for name, price in SERVICES:
            db.add(Service(clinic_id=clinic_id, name=name, price_paise=price))
        db.add(
            ConsentNotice(
                clinic_id=clinic_id,
                version=1,
                text_en=CONSENT_TEXT_EN,
                text_hi=CONSENT_TEXT_HI,
                purposes=["treatment", "billing", "access_audit"],
                published_at=datetime.now(UTC),
            )
        )

    fixture: dict[str, object] = {"clinic_id": str(clinic_id), "password": password, "users": users}
    await asyncio.to_thread(_write, path, fixture)
    return fixture


def _write(path: Path, fixture: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fixture, indent=2), encoding="utf-8")


async def main(argv: list[str]) -> int:
    if get_settings().env not in ("local", "test"):
        print("e2e_fixture only runs with APP_ENV=local or test")
        return 2
    if len(argv) != 2:
        print("usage: python -m app.scripts.e2e_fixture <output.json>")
        return 2
    await build(Path(argv[1]))
    await get_engine().dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv)))
