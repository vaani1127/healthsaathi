"""Load test for the recording overhead (SPEC 8, RQ5). Runs inside the locustio/locust image on the
VM against the benchmark copy of the API (hs_demo.loadtest), never against the live demo.

Every simulated user shares one signed-in session per role: a 6 digit code is accepted once, so
many users cannot each sign in as the same demo account. The mix touches both recording paths:

- reception lists patients (each listed patient is one recorded access, the batch path),
- the nurse opens a chart (one recorded access, the single path),
- /auth/me reads no patient data and is the control.

The doctor's chart is left out: with recording on, an unexplained doctor read asks for a typed
reason, and with recording off it does not, so the two runs would not do the same work.
"""

import json
import os
import random
import threading
import time
from typing import Any

import pyotp
import requests
from locust import HttpUser, between, task

with open(os.environ.get("LOAD_LOGINS", "/load/logins.json"), encoding="utf-8") as _file:
    CONFIG = json.load(_file)
_tokens: dict[str, str] = {}
_patients: list[str] = []
_lock = threading.Lock()


def _sign_in(host: str, login: dict[str, str]) -> str:
    session = requests.Session()
    resp = session.post(
        f"{host}/api/v1/auth/login",
        json={"email": login["email"], "password": CONFIG["password"]},
        timeout=30,
    )
    resp.raise_for_status()
    challenge = resp.json()["challenge_token"]
    code = pyotp.TOTP(login["totp_secret"]).now()
    resp = session.post(
        f"{host}/api/v1/auth/totp/verify",
        json={"challenge_token": challenge, "code": code},
        timeout=30,
    )
    resp.raise_for_status()
    body: dict[str, Any] = resp.json()
    if body.get("clinic_id") is None:
        auth = {"Authorization": f"Bearer {body['access_token']}"}
        membership = session.get(f"{host}/api/v1/auth/me", headers=auth, timeout=30).json()[
            "memberships"
        ][0]
        body = session.post(
            f"{host}/api/v1/auth/select-clinic",
            json={"clinic_id": membership["clinic_id"], "role": membership["role"]},
            headers=auth,
            timeout=30,
        ).json()
    return str(body["access_token"])


def _setup(host: str) -> None:
    with _lock:
        if _tokens:
            return
        clinic = CONFIG["clinics"][0]
        for role in ("reception", "nurse"):
            _tokens[role] = _sign_in(host, clinic[role])
            time.sleep(13)  # login is limited to 5 a minute per address (when limits are on)
        page = requests.get(
            f"{host}/api/v1/patients",
            params={"limit": 25},
            headers={"Authorization": f"Bearer {_tokens['reception']}"},
            timeout=30,
        )
        page.raise_for_status()
        _patients.extend(p["id"] for p in page.json()["items"])


class ClinicStaff(HttpUser):
    wait_time = between(0.5, 1.5)

    def on_start(self) -> None:
        _setup(self.host or "")

    def _auth(self, role: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {_tokens[role]}"}

    @task(3)
    def reception_lists_patients(self) -> None:
        self.client.get(
            "/api/v1/patients?limit=25", headers=self._auth("reception"), name="patient list"
        )

    @task(5)
    def nurse_opens_chart(self) -> None:
        patient = random.choice(_patients)  # noqa: S311 - load mix, not security
        self.client.get(
            f"/api/v1/patients/{patient}/chart", headers=self._auth("nurse"), name="nurse chart"
        )

    @task(2)
    def control_without_patient_data(self) -> None:
        self.client.get("/api/v1/auth/me", headers=self._auth("nurse"), name="auth me (control)")
