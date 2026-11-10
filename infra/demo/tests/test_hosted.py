import json
from pathlib import Path

import httpx
import polars as pl
import pytest

from hs_demo import hosted, load
from hs_demo.logins import render
from hs_demo.native import read_env_file

KEY = "0x" + "1" * 64


def test_env_is_built_from_the_domain() -> None:
    env = hosted.build_env("example.org", "demo@example.org", "team@gmail.com", KEY)
    assert env["APP_ENV"] == "prod"
    assert env["API_DOMAIN"] == "api.example.org"
    assert env["COOKIE_DOMAIN"] == ".example.org"
    assert json.loads(env["CORS_ORIGINS"]) == ["https://app.example.org"]
    assert env["PUBLIC_APP_URL"] == "https://app.example.org"
    assert env["ANCHOR_POSTER_PRIVATE_KEY"] == KEY
    assert env["DEMO_PATIENT_EMAIL"] == "team+{login}@gmail.com"
    # The app's login must never be the admin login: RLS relies on it.
    assert "hs_app:" in env["DATABASE_URL"] and "hsadmin:" in env["MIGRATOR_DATABASE_URL"]
    assert env["AZURE_SUFFIX"] in env["DATABASE_URL"]
    assert "ssl=require" in env["DATABASE_URL"]
    assert "sslmode=require" in env["BACKUP_DATABASE_URL"]


def test_two_environments_share_no_secrets() -> None:
    first = hosted.build_env("example.org", "a@example.org", "a@gmail.com", KEY)
    second = hosted.build_env("example.org", "a@example.org", "a@gmail.com", KEY)
    for key in ("JWT_SECRET", "OTP_HMAC_KEY", "DATA_KEYS", "LEDGER_SIGNING_SEED"):
        assert first[key] != second[key]


def test_vm_gets_no_setup_secrets_and_no_empty_values() -> None:
    env = hosted.build_env("example.org", "a@example.org", "a@gmail.com", KEY)
    on_vm = hosted.vm_values(env)
    assert not hosted.SETUP_ONLY & set(on_vm)
    assert "" not in on_vm.values()
    assert {"API_DOMAIN", "DATABASE_URL", "BACKUP_DATABASE_URL", "JWT_SECRET"} <= set(on_vm)


def test_env_file_round_trip_uses_unix_line_ends(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "prod.env"
    env = hosted.build_env("example.org", "a@example.org", "a@gmail.com", KEY)
    hosted.write_values(path, env)
    assert b"\r" not in path.read_bytes()
    assert read_env_file(path) == env


def test_init_refuses_to_replace_existing_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "prod.env"
    monkeypatch.setattr(hosted, "PROD_ENV", path)
    monkeypatch.setattr(hosted, "WALLETS", tmp_path / "wallets.json")
    args = ["init", "--domain", "example.org", "--sender", "a@b.org", "--inbox", "a@b.org"]
    hosted.main(args)
    before = path.read_bytes()
    with pytest.raises(SystemExit):
        hosted.main(args)
    assert path.read_bytes() == before
    wallets = json.loads((tmp_path / "wallets.json").read_text(encoding="utf-8"))
    assert len(wallets["admins"]) == 3
    assert read_env_file(path)["ANCHOR_POSTER_PRIVATE_KEY"] == wallets["poster"]["key"]


def test_init_rejects_a_bad_domain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hosted, "PROD_ENV", tmp_path / "prod.env")
    with pytest.raises(SystemExit):
        hosted.main(["init", "--domain", "not a domain", "--sender", "a@b.c", "--inbox", "a@b.c"])


def test_github_settings_skip_values_not_set_yet(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(hosted, "STATE", tmp_path)
    env = hosted.build_env("example.org", "a@b.org", "a@b.org", KEY)
    stored, public = hosted.github_settings(env)
    assert stored["VM_SSH_KEY"] == ""
    assert stored["ANCHOR_API_URL"] == "https://api.example.org"
    assert public["PROD_API_URL"] == "https://api.example.org"
    assert public["VITE_AUDIT_ANCHOR_ADDRESS"] == ""


def test_patient_email_uses_the_real_inbox_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEMO_PATIENT_EMAIL", raising=False)
    assert load.patient_email("patient1.a") == "patient1.a@demo.healthsaathi.test"
    monkeypatch.setenv("DEMO_PATIENT_EMAIL", "team+{login}@gmail.com")
    assert load.patient_email("patient1.a") == "team+patient1.a@gmail.com"


def test_assign_emails_keeps_staff_on_the_reserved_domain(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DEMO_PATIENT_EMAIL", "team+{login}@gmail.com")
    tables = {
        "users": pl.DataFrame({"id": ["d", "p"], "name": ["Dr Demo", "Demo Patient"]}),
        "memberships": pl.DataFrame(
            {"clinic_id": ["c"], "role": ["doctor"], "created_at": [1], "user_id": ["d"]}
        ),
        "patients": pl.DataFrame({"clinic_id": ["c"], "user_id": ["p"], "mrn": ["1"]}),
    }
    logins = load.assign_emails(tables, {"c": "a"})
    assert logins["d"].email == "doctor.a@demo.healthsaathi.test"
    assert logins["p"].email == "team+patient1.a@gmail.com"


def test_hosted_login_table_has_no_local_addresses() -> None:
    data = {
        "web_url": "https://app.example.org",
        "password": "Saathi-1234abcd",
        "clinics": {},
        "first_day": "2026-09-20",
        "last_day": "2026-10-11",
        "patients": 50,
        "access_events": 10,
        "users": [{"clinic": "a", "role": "doctor", "email": "d@x.test", "totp_secret": "ABC"}],
        "cases": [],
    }
    text = render(data)
    assert "https://app.example.org" in text
    assert "localhost" not in text
    assert "dev mailbox" not in text.lower()
    assert "make prod-code" in text


def test_activity_config_takes_one_login_per_role_and_clinic() -> None:
    def user(clinic: str, role: str, n: int) -> dict[str, str | None]:
        secret = None if role == "patient" else f"S{clinic}{role}{n}"
        return {
            "clinic": clinic,
            "role": role,
            "email": f"{role}{n}.{clinic}@x.test",
            "totp_secret": secret,
        }

    data = {
        "password": "Saathi-1234abcd",
        "clinics": {"b": "Clinic B", "a": "Clinic A"},
        "users": [
            user(c, r, n)
            for c in "ab"
            for r in ("clinic_admin", "reception", "nurse", "doctor", "patient")
            for n in (1, 2)
        ],
    }
    config = hosted.activity_config(data)
    assert config["api_url"] == "http://api:8000"
    assert config["password"] == "Saathi-1234abcd"
    assert [c["doctor"]["email"] for c in config["clinics"]] == [
        "doctor1.a@x.test",
        "doctor1.b@x.test",
    ]
    assert all(set(c) == {"reception", "nurse", "doctor"} for c in config["clinics"])
    # Admins and patients are never handed to the bot.
    assert "admin" not in json.dumps(config) and "patient" not in json.dumps(config)


class FakeResponse:
    def __init__(self, body: object) -> None:
        self.body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.body


def test_poster_balance_reads_pol_or_says_why_not(monkeypatch: pytest.MonkeyPatch) -> None:
    address = "0x" + "1" * 40
    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse({"result": hex(26 * 10**15)}))
    assert hosted.poster_balance("https://rpc.test", address) == "0.0260 POL"
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: FakeResponse({"error": {"message": "limit"}})
    )
    assert hosted.poster_balance("https://rpc.test", address).startswith(
        "could not be read (RPC error"
    )

    def down(*a: object, **k: object) -> None:
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx, "post", down)
    assert hosted.poster_balance("https://rpc.test", address) == (
        "could not be read (ConnectError: no route)"
    )
    assert hosted.poster_balance("", address) == "could not be read (ANCHOR_RPC_URL is not set)"


def test_anchor_lines_per_clinic() -> None:
    report = {
        "clinics": {
            "a": {
                "name": "Clinic A",
                "minutes_since_last_anchor": 12.4,
                "anchors_24h": 30,
                "backends": {"amoy": {"anchors_24h": 25}, "ots": {"anchors_24h": 5}},
            },
            "b": {
                "name": "Clinic B",
                "minutes_since_last_anchor": None,
                "anchors_24h": 0,
                "backends": {},
            },
        }
    }
    assert hosted.anchor_lines(report) == [
        "Clinic A: last anchor 12 min ago, 30 anchors in the last 24 h (amoy 25, ots 5)",
        "Clinic B: never anchored, 0 anchors in the last 24 h",
    ]
    assert hosted.anchor_lines("could not be read (exit 1: No module named x)") == [
        "anchors: could not be read (exit 1: No module named x)"
    ]
