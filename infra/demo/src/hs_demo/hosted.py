"""Hosted public demo (prod-demo in docs/RUNBOOK.md): the setup steps, all driven by `.env.prod`.

    python -m hs_demo.hosted init --domain D --sender S --inbox I   # make prod-init
    python -m hs_demo.hosted status          # make prod-status: what is set and what is missing
    python -m hs_demo.hosted set KEY VALUE   # store one value
    python -m hs_demo.hosted db              # make prod-db: migrate, create the app's login
    python -m hs_demo.hosted seed            # make prod-seed: load the synthetic demo clinics
    python -m hs_demo.hosted chain           # make prod-chain: deploy contracts, fund the admins
    python -m hs_demo.hosted register        # make prod-register: register clinics, first anchor
    python -m hs_demo.hosted vm-env          # make prod-vm-env: copy the settings to the VM
    python -m hs_demo.hosted cloudflare      # make prod-cloudflare: Pages project and app domain
    python -m hs_demo.hosted github          # make prod-github: secrets, variables, repositories
    python -m hs_demo.hosted activity        # make prod-activity: start the demo activity bot
    python -m hs_demo.hosted code EMAIL      # make prod-code: current authenticator code
    python -m hs_demo.hosted logins          # make prod-logins: print the login table

`.env.prod` and `.demo/hosted` are gitignored and hold secrets. Values are never printed.

The wallets are made here and only ever hold test POL. One person holding all three registry
admin keys is fine for a demo, but it is not the 2-of-3 control ADR 0006 describes for real use.
"""

import argparse
import asyncio
import json
import os
import re
import secrets
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
from eth_account import Account
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from web3.exceptions import ContractCustomError

from app.anchor import job
from app.anchor.evm import EvmAnchor
from app.core.dburl import asyncpg_url, engine_options
from app.scripts.register_clinic import confirm, registration
from hs_demo.init import new_secrets
from hs_demo.native import API_DIR, CONTRACTS, ROOT, read_env_file, tool

PROD_ENV = Path(os.environ.get("HOSTED_ENV", ROOT / ".env.prod"))
STATE = Path(os.environ.get("HOSTED_STATE", ROOT / ".demo" / "hosted"))
WALLETS = STATE / "wallets.json"
OWNER = "vaani1127"
REPO = f"{OWNER}/healthsaathi"
WITNESS_REPO = f"{OWNER}/healthsaathi-witness"
BACKUP_REPO = f"{OWNER}/healthsaathi-backups"
PAGES_PROJECT = "healthsaathi"
AMOY_RPC = "https://polygon-amoy-bor-rpc.publicnode.com"
EXPLORER = "https://amoy.polygonscan.com"
# Registering one clinic costs an admin up to about 0.006 POL at Amoy gas prices.
ADMIN_GAS = "0.015ether"
# ClinicRegistry.AlreadyConfirmed()
ALREADY_CONFIRMED = "0x5f8a9c0b"

# Needed for setup or by GitHub Actions, never on the VM.
SETUP_ONLY = {
    "AZURE_LOCATION",
    "AZURE_SUFFIX",
    "BACKUP_PASSPHRASE",
    "BACKUP_REPO_TOKEN",
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_API_TOKEN",
    "DB_ADMIN_PASSWORD",
    "DEMO_PATIENT_EMAIL",
    "DOMAIN",
    "HS_APP_PASSWORD",
    "SEED_DEMO_PASSWORD",
    "VM_HOST",
    "VM_USER",
}
# Each one is set by hand or by a step before the deploy can finish.
REQUIRED = {
    "BREVO_API_KEY": "Brevo, SMTP & API, API keys",
    "WITNESS_GITHUB_TOKEN": "GitHub fine-grained token, healthsaathi-witness only",
    "BACKUP_REPO_TOKEN": "GitHub fine-grained token, healthsaathi-backups only",
    "CLOUDFLARE_API_TOKEN": "Cloudflare token with Pages Edit",
    "CLOUDFLARE_ACCOUNT_ID": "Cloudflare dashboard, account home",
    "VM_HOST": "make prod-azure",
    "AZURE_BLOB_CONTAINER_SAS_URL": "make prod-azure",
    "AUDIT_ANCHOR_ADDRESS": "make prod-chain",
    "CLINIC_REGISTRY_ADDRESS": "make prod-chain",
}


def say(message: str) -> None:
    print(f"[hosted] {message}", flush=True)


def read_values() -> dict[str, str]:
    values = read_env_file(PROD_ENV)
    if not values:
        raise SystemExit(f"{PROD_ENV.name} is missing; run make prod-init first")
    return values


def write_values(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{k}={v}\n" for k, v in values.items())
    path.write_bytes(body.encode())  # LF on every system: the VM reads this file
    path.chmod(0o600)


def set_value(key: str, value: str) -> None:
    write_values(PROD_ENV, {**read_values(), key: value})


def plus_address(inbox: str) -> str:
    local, _, domain = inbox.partition("@")
    if not local or "." not in domain:
        raise SystemExit("--inbox must be an email address")
    return f"{local}+{{login}}@{domain}"


def wallet() -> dict[str, str]:
    account = Account.create()
    return {"address": account.address, "key": "0x" + account.key.hex().removeprefix("0x")}


def new_wallets() -> dict[str, Any]:
    return {"poster": wallet(), "admins": [wallet() for _ in range(3)]}


def read_wallets() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(WALLETS.read_text(encoding="utf-8"))
    return data


def build_env(domain: str, sender: str, inbox: str, poster_key: str) -> dict[str, str]:
    suffix = secrets.token_hex(3)
    host = f"healthsaathi-{suffix}.postgres.database.azure.com:5432/healthsaathi"
    admin = secrets.token_hex(24)
    app_login = secrets.token_hex(24)
    generated = new_secrets()
    generated["ANCHOR_POSTER_PRIVATE_KEY"] = poster_key
    return {
        "APP_ENV": "prod",
        "LOG_LEVEL": "INFO",
        "DOMAIN": domain,
        "AZURE_LOCATION": "centralindia",
        "AZURE_SUFFIX": suffix,
        "DB_ADMIN_PASSWORD": admin,
        "HS_APP_PASSWORD": app_login,
        "DATABASE_URL": f"postgresql+asyncpg://hs_app:{app_login}@{host}?ssl=require",
        "MIGRATOR_DATABASE_URL": f"postgresql+asyncpg://hsadmin:{admin}@{host}?ssl=require",
        "BACKUP_DATABASE_URL": f"postgresql://hsadmin:{admin}@{host}?sslmode=require",
        "API_DOMAIN": f"api.{domain}",
        "COOKIE_DOMAIN": f".{domain}",
        "CORS_ORIGINS": json.dumps([f"https://app.{domain}"]),
        "PUBLIC_APP_URL": f"https://app.{domain}",
        "EMAIL_BACKEND": "brevo",
        "BREVO_API_KEY": "",
        "EMAIL_FROM": sender,
        "VAPID_SUBJECT": f"mailto:{sender}",
        "DEMO_PATIENT_EMAIL": plus_address(inbox),
        "ANCHOR_CHAIN": "amoy",
        "ANCHOR_RPC_URL": AMOY_RPC,
        "ANCHOR_EXPLORER_URL": EXPLORER,
        "AUDIT_ANCHOR_ADDRESS": "",
        "CLINIC_REGISTRY_ADDRESS": "",
        "WITNESS_REPO": WITNESS_REPO,
        "WITNESS_GITHUB_TOKEN": "",
        "OTS_ENABLED": "true",
        "BACKUP_PASSPHRASE": secrets.token_urlsafe(32),
        "BACKUP_REPO_TOKEN": "",
        "CLOUDFLARE_API_TOKEN": "",
        "CLOUDFLARE_ACCOUNT_ID": "",
        **generated,
    }


def vm_values(values: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in values.items() if k not in SETUP_ONLY and v != ""}


def init(args: argparse.Namespace) -> None:
    if PROD_ENV.exists():
        raise SystemExit(f"{PROD_ENV.name} already exists; it holds the only copy of its secrets")
    if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", args.domain):
        raise SystemExit("--domain must look like example.org")
    wallets = new_wallets()
    WALLETS.parent.mkdir(parents=True, exist_ok=True)
    WALLETS.write_text(json.dumps(wallets, indent=2), encoding="utf-8")
    WALLETS.chmod(0o600)
    env = build_env(args.domain, args.sender, args.inbox, wallets["poster"]["key"])
    write_values(PROD_ENV, env)
    say(f"wrote {PROD_ENV.name} and {WALLETS}")
    say(f"fund this address with test POL: {wallets['poster']['address']}")


def status() -> None:
    values = read_values()
    missing = [(k, how) for k, how in REQUIRED.items() if not values.get(k)]
    say(f"domain {values['DOMAIN']}, {len(REQUIRED) - len(missing)} of {len(REQUIRED)} values set")
    for key, how in missing:
        print(f"  missing {key}: {how}")
    print(f"  poster wallet: {read_wallets()['poster']['address']}")
    try:
        ready = httpx.get(f"https://{values['API_DOMAIN']}/api/v1/ready", timeout=10)
        print(f"  api: HTTP {ready.status_code}")
    except httpx.HTTPError as exc:
        print(f"  api: not reachable ({type(exc).__name__})")


def run_env(values: dict[str, str]) -> dict[str, str]:
    """The environment of a setup process on this computer."""
    return {
        **os.environ,
        **values,
        "DEMO_STATE_DIR": str(STATE),
        "DEMO_WEB_URL": values["PUBLIC_APP_URL"],
        # Seeding books appointments through the normal services and must not send email.
        "EMAIL_BACKEND": "console",
        "MODEL_DIR": str(STATE / "models"),
        "SIM_PROFILES_DIR": str(ROOT / "sim" / "profiles"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    }


def run(args: list[str], cwd: Path, env: dict[str, str]) -> None:
    subprocess.run(args, cwd=cwd, env=env, check=True)  # noqa: S603


async def create_app_login(admin_url: str, password: str) -> None:
    """docs/RUNBOOK.md, Database set-up. Without INHERIT, hs_app can do nothing until the app's
    SET LOCAL ROLE, so row level security always applies."""
    url = asyncpg_url(admin_url)
    engine = create_async_engine(url, **engine_options(url))
    try:
        async with engine.begin() as db:
            exists = await db.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = 'hs_app'"))
            verb = "ALTER" if exists else "CREATE"
            # Hex from build_env. Role statements cannot take bind parameters.
            await db.execute(text(f"{verb} ROLE hs_app LOGIN PASSWORD '{password}'"))
            await db.execute(text("GRANT app_rw, anchor_job TO hs_app WITH INHERIT FALSE"))
            name = await db.scalar(text("SELECT current_database()"))
            await db.execute(text(f'GRANT CONNECT ON DATABASE "{name}" TO hs_app'))
    finally:
        await engine.dispose()


def database() -> None:
    values = read_values()
    say("running migrations")
    run([sys.executable, "-m", "alembic", "upgrade", "head"], API_DIR, run_env(values))
    asyncio.run(create_app_login(values["MIGRATOR_DATABASE_URL"], values["HS_APP_PASSWORD"]))
    say("database ready; login hs_app can only switch into app_rw and anchor_job")


def seed() -> None:
    run([sys.executable, "-m", "hs_demo.seed"], API_DIR, run_env(read_values()))


def chain() -> None:
    values = read_values()
    wallets = read_wallets()
    poster, admins = wallets["poster"], wallets["admins"]
    rpc = values["ANCHOR_RPC_URL"]
    if not values.get("AUDIT_ANCHOR_ADDRESS"):
        say("deploying the contracts")
        env = {
            **os.environ,
            "DEPLOY_NETWORK": "amoy",
            **{f"REGISTRY_ADMIN_{i}": a["address"] for i, a in enumerate(admins, start=1)},
        }
        script = ["script", "script/Deploy.s.sol", "--rpc-url", rpc, "--broadcast"]
        run([tool("forge"), *script, "--private-key", poster["key"]], CONTRACTS, env)
        deployed = json.loads((CONTRACTS / "deployments" / "amoy.json").read_text(encoding="utf-8"))
        set_value("AUDIT_ANCHOR_ADDRESS", deployed["AuditAnchor"])
        set_value("CLINIC_REGISTRY_ADDRESS", deployed["ClinicRegistry"])
    # Registering clinics costs the two admins gas; the faucet only funds the poster.
    send = [tool("cast"), "send", "--value", ADMIN_GAS, "--rpc-url", rpc, "--json"]
    for admin in admins[:2]:
        run([*send, "--private-key", poster["key"], admin["address"]], ROOT, dict(os.environ))
    say("contracts ready and the registry admins funded")


async def is_registered(evm: EvmAnchor, clinic: uuid.UUID) -> bool:
    current = await evm.signer_key_hash(clinic)
    return bool(current and any(current))


async def register_clinics() -> None:
    from app.core.config import get_settings
    from app.core.db import get_engine

    s = get_settings()
    if not (s.anchor_rpc_url and s.audit_anchor_address and s.clinic_registry_address):
        raise SystemExit("run make prod-chain first")
    logins_data = json.loads((STATE / "logins.json").read_text(encoding="utf-8"))
    wallets = read_wallets()
    evm = EvmAnchor(s.anchor_rpc_url, s.audit_anchor_address, None, s.clinic_registry_address)
    try:
        for value in logins_data["clinic_ids"]:
            clinic = uuid.UUID(value)
            reg = registration(clinic, wallets["poster"]["address"])
            for admin in wallets["admins"][:2]:
                if await is_registered(evm, clinic):
                    break
                try:
                    await confirm(evm, admin["key"], reg)
                except ContractCustomError as exc:
                    # A rerun after a failure: this admin confirmed already, ask the next one.
                    if exc.data != ALREADY_CONFIRMED:
                        raise
            say(f"clinic {clinic} registered")
    finally:
        await evm.close()
    runs = await job.run()
    say(f"first anchor run: {runs}")
    await get_engine().dispose()


def register() -> None:
    os.environ.update(run_env(read_values()))
    asyncio.run(register_clinics())


def ssh_options() -> list[str]:
    # Quoted: ssh reads a space in this option as a list of files ("My Workspace").
    known_hosts = f'UserKnownHostsFile="{(STATE / "known_hosts").as_posix()}"'
    return ["-i", str(STATE / "deploy_key"), "-o", known_hosts, "-o", "StrictHostKeyChecking=yes"]


def vm_env() -> None:
    values = read_values()
    target = f"{values['VM_USER']}@{values['VM_HOST']}"
    upload = STATE / "vm.env"
    write_values(upload, vm_values(values))
    env = dict(os.environ)
    try:
        run(
            [tool("scp"), *ssh_options(), str(upload), f"{target}:/opt/healthsaathi/.env"],
            ROOT,
            env,
        )
        run([tool("ssh"), *ssh_options(), target, "chmod 600 /opt/healthsaathi/.env"], ROOT, env)
    finally:
        upload.unlink()
    say("settings copied to the VM")


# The container runs as this user (infra/api.Dockerfile); only it may read the logins file.
CONTAINER_UID = 10001
ACTIVITY_ROLES = ("reception", "nurse", "doctor")


def activity_config(logins_data: dict[str, Any], mean_gap_minutes: float = 6) -> dict[str, Any]:
    """The demo activity bot's settings: one reception, nurse and doctor login per clinic."""
    clinics = []
    for code in sorted(logins_data["clinics"]):
        users = [u for u in logins_data["users"] if u["clinic"] == code]
        clinics.append(
            {
                role: next(
                    {"email": u["email"], "totp_secret": u["totp_secret"]}
                    for u in users
                    if u["role"] == role
                )
                for role in ACTIVITY_ROLES
            }
        )
    return {
        "api_url": "http://api:8000",
        "password": logins_data["password"],
        "mean_gap_minutes": mean_gap_minutes,
        "clinics": clinics,
    }


def activity() -> None:
    values = read_values()
    data = json.loads((STATE / "logins.json").read_text(encoding="utf-8"))
    upload = STATE / "activity.json"
    upload.write_bytes(json.dumps(activity_config(data), indent=2).encode())
    target = f"{values['VM_USER']}@{values['VM_HOST']}"
    remote = "/opt/healthsaathi/activity.json"
    env = dict(os.environ)
    try:
        run([tool("scp"), *ssh_options(), str(upload), f"{target}:{remote}"], ROOT, env)
        fix = f"sudo chown {CONTAINER_UID} {remote} && sudo chmod 400 {remote}"
        run([tool("ssh"), *ssh_options(), target, fix], ROOT, env)
    finally:
        upload.unlink()
    say("activity bot settings copied; the next deploy starts the bot")


def cloudflare() -> None:
    values = read_values()
    account = values["CLOUDFLARE_ACCOUNT_ID"]
    base = f"https://api.cloudflare.com/client/v4/accounts/{account}/pages/projects"
    headers = {"Authorization": f"Bearer {values['CLOUDFLARE_API_TOKEN']}"}
    with httpx.Client(headers=headers, timeout=30) as client:
        if client.get(f"{base}/{PAGES_PROJECT}").status_code == 404:
            body = {"name": PAGES_PROJECT, "production_branch": "main"}
            client.post(base, json=body).raise_for_status()
            say(f"created the Pages project {PAGES_PROJECT}")
        project = client.get(f"{base}/{PAGES_PROJECT}").raise_for_status().json()["result"]
        domain = f"app.{values['DOMAIN']}"
        known = client.get(f"{base}/{PAGES_PROJECT}/domains").raise_for_status().json()["result"]
        if domain not in [d["name"] for d in known]:
            client.post(f"{base}/{PAGES_PROJECT}/domains", json={"name": domain}).raise_for_status()
        # When the domain's DNS is on Cloudflare too (and the token may edit it), add the records.
        zones = client.get(
            "https://api.cloudflare.com/client/v4/zones", params={"name": values["DOMAIN"]}
        ).json()
        if not zones.get("result"):
            say(f"add DNS: {domain} CNAME {project['subdomain']}, {values['API_DOMAIN']} A VM_HOST")
            return
        records = (
            f"https://api.cloudflare.com/client/v4/zones/{zones['result'][0]['id']}/dns_records"
        )
        # The API record is not proxied, so Caddy on the VM gets its own certificate.
        wanted = [
            {"type": "CNAME", "name": domain, "content": project["subdomain"], "proxied": True},
            {
                "type": "A",
                "name": values["API_DOMAIN"],
                "content": values["VM_HOST"],
                "proxied": False,
            },
        ]
        for record in wanted:
            found = client.get(records, params={"name": record["name"]}).json()["result"]
            if found:
                client.put(f"{records}/{found[0]['id']}", json=record).raise_for_status()
            else:
                client.post(records, json=record).raise_for_status()
            say(f"DNS {record['type']} {record['name']} -> {record['content']}")


def gh(args: list[str], stdin: str | None = None, *, check: bool = True) -> int:
    # Bytes, not text: text mode on Windows writes CRLF line ends, and ssh cannot load a private
    # key that has them.
    done = subprocess.run(  # noqa: S603
        [tool("gh"), *args],
        input=stdin.encode() if stdin is not None else None,
        capture_output=True,
        check=False,
    )
    if check and done.returncode != 0:
        raise SystemExit(f"gh {args[0]} {args[1]} failed: {done.stderr.decode().strip()}")
    return done.returncode


def github_settings(values: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    known_hosts = STATE / "known_hosts"
    deploy_key = STATE / "deploy_key"
    api_url = f"https://{values['API_DOMAIN']}"
    stored = {
        "VM_HOST": values.get("VM_HOST", ""),
        "VM_USER": values.get("VM_USER", ""),
        "VM_SSH_KEY": deploy_key.read_bytes().decode() if deploy_key.exists() else "",
        "VM_KNOWN_HOSTS": known_hosts.read_bytes().decode() if known_hosts.exists() else "",
        "BACKUP_PASSPHRASE": values["BACKUP_PASSPHRASE"],
        "BACKUP_REPO_TOKEN": values.get("BACKUP_REPO_TOKEN", ""),
        "ANCHOR_API_URL": api_url,
        "ANCHOR_TRIGGER_TOKEN": values["ANCHOR_TRIGGER_TOKEN"],
        "CLOUDFLARE_API_TOKEN": values.get("CLOUDFLARE_API_TOKEN", ""),
        "CLOUDFLARE_ACCOUNT_ID": values.get("CLOUDFLARE_ACCOUNT_ID", ""),
    }
    public = {
        "CF_PAGES_PROJECT": PAGES_PROJECT,
        "PROD_API_URL": api_url,
        "BACKUP_REPO": BACKUP_REPO,
        "VITE_CHAIN_RPC_URL": values["ANCHOR_RPC_URL"],
        "VITE_AUDIT_ANCHOR_ADDRESS": values.get("AUDIT_ANCHOR_ADDRESS", ""),
        "VITE_CLINIC_REGISTRY_ADDRESS": values.get("CLINIC_REGISTRY_ADDRESS", ""),
        "VITE_EXPLORER_URL": values["ANCHOR_EXPLORER_URL"],
    }
    return stored, public


def github() -> None:
    values = read_values()
    for repo, flag in ((WITNESS_REPO, "--public"), (BACKUP_REPO, "--private")):
        if gh(["repo", "view", repo], check=False) != 0:
            gh(["repo", "create", repo, flag, "--add-readme"])
            say(f"created {repo}")
    gh(["api", "-X", "PUT", f"repos/{REPO}/environments/production"])
    stored, public = github_settings(values)
    for name, value in stored.items():
        if value:
            gh(["secret", "set", name, "--repo", REPO], stdin=value)
    for name, value in public.items():
        if value:
            gh(["variable", "set", name, "--repo", REPO, "--body", value])
    empty = sorted(k for k, v in {**stored, **public}.items() if not v)
    say(f"GitHub settings stored; still empty: {', '.join(empty) or 'nothing'}")


def logins(argv: list[str]) -> None:
    os.environ["DEMO_STATE_DIR"] = str(STATE)
    from hs_demo.logins import current_code, render

    if argv:
        print(current_code(argv[0]))
    else:
        print(render(json.loads((STATE / "logins.json").read_text(encoding="utf-8"))))


COMMANDS = {
    "status": status,
    "db": database,
    "seed": seed,
    "chain": chain,
    "register": register,
    "vm-env": vm_env,
    "cloudflare": cloudflare,
    "github": github,
    "activity": activity,
}


def main(argv: list[str]) -> int:
    command = argv[0] if argv else ""
    if command == "init":
        parser = argparse.ArgumentParser(prog="hosted init")
        parser.add_argument("--domain", required=True)
        parser.add_argument("--sender", required=True, help="sender verified in Brevo")
        parser.add_argument("--inbox", required=True, help="real inbox for demo patient codes")
        init(parser.parse_args(argv[1:]))
    elif command == "set" and len(argv) == 3:
        set_value(argv[1], argv[2])
    elif command == "code" and len(argv) == 2:
        logins(argv[1:])
    elif command == "logins":
        logins([])
    elif command in COMMANDS:
        COMMANDS[command]()
    else:
        raise SystemExit(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
