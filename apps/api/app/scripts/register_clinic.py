"""Register a clinic's signing key and poster wallet in ClinicRegistry (SPEC 6.1).

Registration needs two of the three registry admins. Each admin runs this once with their own key:

    uv run python -m app.scripts.register_clinic <clinic-uuid>           # print the values
    export REGISTRY_ADMIN_PRIVATE_KEY=0x...  # in the shell only, never in .env
    uv run python -m app.scripts.register_clinic <clinic-uuid> --send

Without --send it prints the values and a `cast send` command, so an admin can confirm from a
hardware wallet or another machine instead.
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid
from dataclasses import asdict, dataclass

from eth_account import Account

from app.anchor.evm import EvmAnchor, clinic_bytes32
from app.core.config import get_settings
from app.ledger.checkpoint import clinic_signing_key
from e2d_core.ledger import public_key_bytes


@dataclass(frozen=True)
class Registration:
    clinic_id: str
    clinic_id_bytes32: str
    key_hash: str
    poster: str
    registered_key_hash: str | None = None


def registration(clinic_id: uuid.UUID, poster: str) -> Registration:
    key_hash = hashlib.sha256(public_key_bytes(clinic_signing_key(clinic_id))).digest()
    return Registration(
        clinic_id=str(clinic_id),
        clinic_id_bytes32="0x" + clinic_bytes32(clinic_id).hex(),
        key_hash="0x" + key_hash.hex(),
        poster=poster,
    )


def poster_address() -> str:
    key = get_settings().anchor_poster_private_key
    if key is None:
        raise SystemExit("ANCHOR_POSTER_PRIVATE_KEY is not set, so the poster address is unknown")
    return str(Account.from_key(key.get_secret_value()).address)


async def confirm(evm: EvmAnchor, admin_key: str, reg: Registration) -> Registration:
    tx = await evm.confirm_registration(
        admin_key, uuid.UUID(reg.clinic_id), bytes.fromhex(reg.key_hash[2:]), reg.poster
    )
    await evm.wait(tx)
    current = await evm.signer_key_hash(uuid.UUID(reg.clinic_id))
    registered = "0x" + current.hex() if current and any(current) else None
    return Registration(**{**asdict(reg), "registered_key_hash": registered})


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("clinic_id", type=uuid.UUID)
    parser.add_argument("--send", action="store_true", help="send this admin's confirmation")
    args = parser.parse_args(argv)

    reg = registration(args.clinic_id, poster_address())
    s = get_settings()
    if not args.send:
        print(json.dumps(asdict(reg), indent=2))
        print(
            "\ncast send <CLINIC_REGISTRY_ADDRESS> 'registerClinic(bytes32,bytes32,address)' "
            f"{reg.clinic_id_bytes32} {reg.key_hash} {reg.poster} --rpc-url <RPC> --interactive"
        )
        return 0

    admin_key = os.environ.get("REGISTRY_ADMIN_PRIVATE_KEY")
    if not admin_key:
        raise SystemExit("set REGISTRY_ADMIN_PRIVATE_KEY in the shell, not in .env")
    if not (s.anchor_rpc_url and s.audit_anchor_address and s.clinic_registry_address):
        raise SystemExit(
            "ANCHOR_RPC_URL, AUDIT_ANCHOR_ADDRESS and CLINIC_REGISTRY_ADDRESS are needed"
        )
    evm = EvmAnchor(s.anchor_rpc_url, s.audit_anchor_address, None, s.clinic_registry_address)
    try:
        done = await confirm(evm, admin_key, reg)
    finally:
        await evm.close()
    print(json.dumps(asdict(done), indent=2))
    if done.registered_key_hash is None:
        print("Confirmed. One more registry admin must run this to finish the registration.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
