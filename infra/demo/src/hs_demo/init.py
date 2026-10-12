"""Prepare the demo state folder before anything else starts (make demo).

- `demo.env`: fresh random secrets on the first run, kept on later runs. The anchor poster wallet
  is a random key that only ever holds test ETH on the local anvil chain.
- The contract addresses that the deploy step wrote to `anvil.json`, added to `demo.env`.
- `web/config.js`: the runtime settings the web app reads (local chain, dev mailbox).

    DEMO_STATE_DIR=/state python -m hs_demo.init
"""

import json
import os
import secrets
import sys
from pathlib import Path

from eth_account import Account

from app.core.crypto import generate_key_spec
from app.notify.push import generate_vapid_keys

CHAIN_RPC_FOR_BROWSER = "http://localhost:8545"
SECRET_KEYS = (
    "JWT_SECRET",
    "OTP_HMAC_KEY",
    "DATA_KEYS",
    "LEDGER_SIGNING_SEED",
    "ANCHOR_TRIGGER_TOKEN",
    "ANCHOR_POSTER_PRIVATE_KEY",
    "SEED_DEMO_PASSWORD",
    "VAPID_PUBLIC_KEY",
    "VAPID_PRIVATE_KEY",
)


def new_secrets() -> dict[str, str]:
    vapid_public, vapid_private = generate_vapid_keys()
    return {
        "VAPID_PUBLIC_KEY": vapid_public,
        "VAPID_PRIVATE_KEY": vapid_private,
        "JWT_SECRET": secrets.token_urlsafe(48),
        "OTP_HMAC_KEY": secrets.token_urlsafe(48),
        "DATA_KEYS": generate_key_spec(),
        "LEDGER_SIGNING_SEED": secrets.token_urlsafe(48),
        "ANCHOR_TRIGGER_TOKEN": secrets.token_urlsafe(32),
        "ANCHOR_POSTER_PRIVATE_KEY": Account.create().key.hex(),
        # Readable on purpose: people type it. Random per installation, printed by make demo.
        "SEED_DEMO_PASSWORD": "Saathi-" + secrets.token_hex(4),
    }


def read_env(path: Path) -> dict[str, str]:
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                values[key] = value
    return values


def write_env(path: Path, values: dict[str, str]) -> None:
    body = "# Local demo only. Created by hs_demo.init; delete the demo volume to start over.\n"
    body += "".join(f"{k}={v}\n" for k, v in values.items())
    path.write_bytes(body.encode())
    path.chmod(0o600)


def prepare(state: Path) -> dict[str, str]:
    state.mkdir(parents=True, exist_ok=True)
    env_path = state / "demo.env"
    values = read_env(env_path)
    if not all(k in values for k in SECRET_KEYS):
        values = {**new_secrets(), **values}
    deployment = state / "anvil.json"
    if deployment.exists():
        data = json.loads(deployment.read_text(encoding="utf-8"))
        values["AUDIT_ANCHOR_ADDRESS"] = data["AuditAnchor"]
        values["CLINIC_REGISTRY_ADDRESS"] = data["ClinicRegistry"]
    write_env(env_path, values)

    web = state / "web"
    web.mkdir(exist_ok=True)
    config = {
        "apiUrl": "",
        "devMailbox": True,
        **(
            {
                "chainRpcUrl": CHAIN_RPC_FOR_BROWSER,
                "auditAnchorAddress": values["AUDIT_ANCHOR_ADDRESS"],
                "clinicRegistryAddress": values["CLINIC_REGISTRY_ADDRESS"],
            }
            if "AUDIT_ANCHOR_ADDRESS" in values
            else {}
        ),
    }
    (web / "config.js").write_bytes(
        f"window.__HS_CONFIG__ = {json.dumps(config, indent=2)};\n".encode()
    )
    for folder in ("outbox", "blobs", "models"):
        (state / folder).mkdir(exist_ok=True)
    return values


def main() -> int:
    state = Path(os.environ.get("DEMO_STATE_DIR", "/state"))
    values = prepare(state)
    chain = "with" if "AUDIT_ANCHOR_ADDRESS" in values else "without"
    print(f"[demo-init] state ready in {state} ({chain} local chain addresses)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
