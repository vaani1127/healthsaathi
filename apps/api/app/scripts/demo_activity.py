"""Synthetic clinic activity for the hosted demo (prod-demo only, synthetic data only).

The anchoring study (SPEC 8) needs audit events spread over real anchor runs, and an idle demo
writes none. This bot signs in as the demo staff and works through the normal API, so every access
is recorded and explained exactly as a person's would be:

1. reception lists patients and registers a walk-in (appointment and queue token),
2. the nurse records vitals,
3. the doctor opens the chart, which the token explains.

Visits arrive at random (exponential gaps) during OPD hours in clinic time: Monday to Saturday
09:30-13:30 and 17:00-20:00, Sunday 09:30-12:30. Nothing happens outside those hours.

    python -m app.scripts.demo_activity /path/to/activity.json

The JSON file holds the demo password and, for each clinic, the reception, nurse and doctor logins
with their authenticator keys. It is written by `make prod-activity` and never committed.
"""

import asyncio
import json
import logging
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pyotp

from app.core.logging import configure_logging

logger = logging.getLogger("demo_activity")

TZ = ZoneInfo("Asia/Kolkata")
SESSIONS: dict[int, list[tuple[time, time]]] = {
    **{d: [(time(9, 30), time(13, 30)), (time(17, 0), time(20, 0))] for d in range(6)},
    6: [(time(9, 30), time(12, 30))],
}
ROLES = ("reception", "nurse", "doctor")
# Access tokens last 15 minutes; sign in again a little before that.
RELOGIN_AFTER = timedelta(minutes=13)
# Login is limited to 5 a minute per address, and every bot login comes from the same address.
LOGIN_SPACING_SECONDS = 15


def is_open(at: datetime) -> bool:
    local = at.astimezone(TZ)
    return any(start <= local.time() < end for start, end in SESSIONS[local.weekday()])


def next_open(at: datetime) -> datetime:
    """The start of the next OPD session at or after `at` (minute steps, at most a week ahead)."""
    probe = at.astimezone(TZ).replace(second=0, microsecond=0)
    for _ in range(7 * 24 * 60):
        if is_open(probe):
            return probe
        probe += timedelta(minutes=1)
    raise RuntimeError("no OPD session in the next week")


def vitals(rng: random.Random) -> dict[str, Any]:
    return {
        "bp_sys": rng.randint(105, 150),
        "bp_dia": rng.randint(65, 95),
        "pulse": rng.randint(62, 104),
        "temp_c": round(rng.uniform(36.4, 38.6), 1),
        "spo2": rng.randint(94, 100),
    }


@dataclass
class Login:
    email: str
    totp_secret: str


@dataclass
class Session:
    """One signed-in staff member, signing in again before the access token runs out."""

    client: httpx.AsyncClient
    login: Login
    password: str
    login_lock: asyncio.Lock
    token: str | None = None
    signed_in_at: datetime | None = None
    clinic_id: str | None = None
    role: str | None = None
    used_codes: set[str] = field(default_factory=set)

    async def headers(self) -> dict[str, str]:
        now = datetime.now(TZ)
        if (
            self.token is None
            or self.signed_in_at is None
            or now - self.signed_in_at > RELOGIN_AFTER
        ):
            await self.sign_in()
        return {"Authorization": f"Bearer {self.token}"}

    async def sign_in(self) -> None:
        async with self.login_lock:
            resp = await self.client.post(
                "/api/v1/auth/login",
                json={"email": self.login.email, "password": self.password},
            )
            resp.raise_for_status()
            challenge = resp.json()["challenge_token"]
            code = pyotp.TOTP(self.login.totp_secret).now()
            while code in self.used_codes:  # a code is accepted once; wait for the next one
                await asyncio.sleep(5)
                code = pyotp.TOTP(self.login.totp_secret).now()
            resp = await self.client.post(
                "/api/v1/auth/totp/verify", json={"challenge_token": challenge, "code": code}
            )
            resp.raise_for_status()
            self.used_codes = {code}
            body = resp.json()
            if body.get("clinic_id") is None:
                me = await self.client.get(
                    "/api/v1/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
                )
                me.raise_for_status()
                membership = me.json()["memberships"][0]
                resp = await self.client.post(
                    "/api/v1/auth/select-clinic",
                    json={"clinic_id": membership["clinic_id"], "role": membership["role"]},
                    headers={"Authorization": f"Bearer {body['access_token']}"},
                )
                resp.raise_for_status()
                body = resp.json()
            self.token = body["access_token"]
            self.clinic_id = body["clinic_id"]
            self.role = body["role"]
            self.signed_in_at = datetime.now(TZ)
            await asyncio.sleep(LOGIN_SPACING_SECONDS)
            logger.info("signed in", extra={"role": self.role})

    async def get(self, path: str, **kwargs: Any) -> Any:
        resp = await self.client.get(path, headers=await self.headers(), **kwargs)
        resp.raise_for_status()
        return resp.json()

    async def post(self, path: str, body: dict[str, Any]) -> Any:
        resp = await self.client.post(path, json=body, headers=await self.headers())
        resp.raise_for_status()
        return resp.json()


@dataclass
class Clinic:
    reception: Session
    nurse: Session
    doctor: Session
    doctor_user_id: str | None = None

    async def visit(self, rng: random.Random) -> str:
        patients = (await self.reception.get("/api/v1/patients", params={"limit": 25}))["items"]
        patient = rng.choice(patients)["id"]
        if self.doctor_user_id is None:
            self.doctor_user_id = (await self.doctor.get("/api/v1/auth/me"))["user_id"]
        await self.reception.post(
            "/api/v1/walkins", {"patient_id": patient, "doctor_user_id": self.doctor_user_id}
        )
        await self.nurse.get("/api/v1/queue")
        await self.nurse.post(f"/api/v1/patients/{patient}/vitals", vitals(rng))
        await self.doctor.get(f"/api/v1/patients/{patient}/chart")
        return str(patient)


async def run_clinic(clinic: Clinic, mean_gap_minutes: float, rng: random.Random) -> None:
    while True:
        now = datetime.now(TZ)
        if not is_open(now):
            start = next_open(now)
            await asyncio.sleep(max(60.0, (start - now).total_seconds()))
            continue
        try:
            await clinic.visit(rng)
            logger.info("visit done")
        except httpx.HTTPError as exc:
            logger.warning("visit failed", extra={"error": str(exc)[:200]})
            for session in (clinic.reception, clinic.nurse, clinic.doctor):
                session.token = None
        await asyncio.sleep(rng.expovariate(1 / (mean_gap_minutes * 60)))


def load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


async def main(path: Path) -> None:
    config = load(path)
    rng = random.Random()  # noqa: S311 - simulated arrivals, not security
    lock = asyncio.Lock()
    async with httpx.AsyncClient(base_url=config["api_url"], timeout=60) as client:

        def session(logins: dict[str, Any], role: str) -> Session:
            return Session(client, Login(**logins[role]), config["password"], lock)

        clinics = [
            Clinic(session(c, "reception"), session(c, "nurse"), session(c, "doctor"))
            for c in config["clinics"]
        ]
        await asyncio.gather(
            *(run_clinic(c, float(config.get("mean_gap_minutes", 6)), rng) for c in clinics)
        )


if __name__ == "__main__":
    configure_logging()
    asyncio.run(main(Path(sys.argv[1])))
