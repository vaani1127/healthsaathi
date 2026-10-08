"""A throwaway anvil chain with the project's contracts deployed, for anchoring tests."""

import json
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eth_account import Account
from web3 import AsyncHTTPProvider, AsyncWeb3

CONTRACTS = Path(__file__).resolve().parents[4] / "contracts"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _artifact(name: str) -> dict[str, Any]:
    path = CONTRACTS / "out" / f"{name}.sol" / f"{name}.json"
    if not path.exists():
        subprocess.run(["forge", "build"], cwd=CONTRACTS, check=True, capture_output=True)  # noqa: S607
    return dict(json.loads(path.read_text(encoding="utf-8")))


@dataclass
class Chain:
    process: subprocess.Popen[bytes]
    rpc_url: str
    w3: AsyncWeb3[Any]
    admins: list[str]
    registry: str = ""
    anchor: str = ""
    poster_key: str = ""
    poster: str = ""

    def stop(self) -> None:
        self.process.terminate()
        self.process.wait(timeout=10)


def anvil_available() -> bool:
    return shutil.which("anvil") is not None and shutil.which("forge") is not None


def start_anvil() -> Chain:
    port = _free_port()
    process = subprocess.Popen(  # noqa: S603
        ["anvil", "--port", str(port), "--silent"],  # noqa: S607
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.2)
    return Chain(process, url, AsyncWeb3(AsyncHTTPProvider(url)), admins=[])


async def create_contract(chain: Chain, name: str, *args: Any) -> str:
    art = _artifact(name)
    contract = chain.w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"]["object"])
    tx = await contract.constructor(*args).transact({"from": (await chain.w3.eth.accounts)[0]})
    receipt = await chain.w3.eth.wait_for_transaction_receipt(tx)
    return str(receipt["contractAddress"])


async def funded_wallet(chain: Chain) -> Any:
    """A fresh wallet funded by anvil, so no private key is written anywhere."""
    wallet = Account.create()
    await chain.w3.provider.make_request("anvil_setBalance", [wallet.address, hex(10**20)])
    return wallet


async def deploy(chain: Chain) -> None:
    w3 = chain.w3
    accounts = await w3.eth.accounts
    chain.admins = [str(a) for a in accounts[:3]]

    chain.registry = await create_contract(chain, "ClinicRegistry", chain.admins)
    chain.anchor = await create_contract(chain, "AuditAnchor", chain.registry)

    poster = await funded_wallet(chain)
    chain.poster_key = "0x" + bytes(poster.key).hex()
    chain.poster = str(poster.address)


async def register(chain: Chain, clinic_bytes: bytes, key_hash: bytes) -> None:
    art = _artifact("ClinicRegistry")
    registry = chain.w3.eth.contract(
        address=chain.w3.to_checksum_address(chain.registry), abi=art["abi"]
    )
    for admin in chain.admins[:2]:
        tx = await registry.functions.registerClinic(clinic_bytes, key_hash, chain.poster).transact(
            {"from": admin}
        )
        await chain.w3.eth.wait_for_transaction_receipt(tx)
