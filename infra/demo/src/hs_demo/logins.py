"""The demo login table that `make demo` prints, and current authenticator codes for demo users."""

import json
import os
import sys
from pathlib import Path
from typing import Any

import pyotp

WEB = "http://localhost:8080"
API = "http://localhost:8081"
CHAIN = "http://localhost:8545"
ROLE_LABELS = {
    "clinic_admin": "Clinic admin",
    "reception": "Reception",
    "nurse": "Nurse",
    "doctor": "Doctor",
    "lab_tech": "Lab technician",
    "patient": "Patient",
}


def render(data: dict[str, Any]) -> str:
    lines = [
        "",
        "HealthSaathi local demo",
        "=======================",
        f"Web app            {WEB}",
        f"API and docs       {API}/docs",
        f"Dev mailbox        {WEB}/dev/mailbox   (email codes, local only)",
        f"Local chain        {CHAIN}   (anvil)",
        "",
        f"Staff password     {data['password']}   (the same for every staff login)",
        f"Demo data          {data['patients']} patients, {data['access_events']} record accesses "
        f"from {data['first_day']} to {data['last_day']}",
        "",
    ]
    width = max(len(u["email"]) for u in data["users"])
    for code, clinic in sorted(data["clinics"].items()):
        lines.append(f"{clinic}")
        lines.append(f"  {'Role':<15} {'Email':<{width}}  Authenticator key")
        for user in data["users"]:
            if user["clinic"] != code:
                continue
            key = user["totp_secret"] or "(patients sign in with an email code)"
            lines.append(f"  {ROLE_LABELS[user['role']]:<15} {user['email']:<{width}}  {key}")
        lines.append("")
    cases = data.get("cases") or []
    if cases:
        lines.append("Insider cases from the simulation (ground truth, for the demo guide only)")
        seen: set[tuple[str, str, str]] = set()
        for case in cases:
            key = (case["clinic"], case["kind"], case["user"])
            if key in seen:
                continue
            seen.add(key)
            where = (
                f"alert on {case['alert_day']}, rank {case['rank']}"
                if case["alert_day"]
                else "break-glass review queue"
            )
            lines.append(
                f"  {case['clinic'][-1]}  {case['kind']:<11} {case['user']} opened "
                f"{case['patient']}'s record on {case['at']}: {where}"
            )
        lines.append("")
    lines += [
        "Signing in",
        "  Staff: open the web app, sign in with the email and password above, then type the",
        "  6 digit code from an authenticator app. To set the app up once per login: in Google or",
        "  Microsoft Authenticator choose 'Enter a setup key', paste the key above, keep",
        "  'Time based'. Or print the current code without an app:",
        "      make demo-code EMAIL=doctor1.a@demo.healthsaathi.test",
        "  Patients: open /patient/login, type the email, and read the code in the dev mailbox.",
        "",
    ]
    return "\n".join(lines)


def current_code(email: str) -> str:
    state = Path(os.environ.get("DEMO_STATE_DIR", "/state"))
    data = json.loads((state / "logins.json").read_text(encoding="utf-8"))
    for user in data["users"]:
        if user["email"].lower() == email.lower() and user["totp_secret"]:
            return str(pyotp.TOTP(user["totp_secret"]).now())
    raise SystemExit(f"no staff login with the email {email}")


if __name__ == "__main__":
    if len(sys.argv) == 2:
        print(current_code(sys.argv[1]))
    else:
        state = Path(os.environ.get("DEMO_STATE_DIR", "/state"))
        print(render(json.loads((state / "logins.json").read_text(encoding="utf-8"))))
