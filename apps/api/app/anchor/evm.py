"""AuditAnchor and ClinicRegistry on an EVM chain (Polygon Amoy, a local anvil, or Besu)."""

import uuid
from dataclasses import dataclass
from typing import Any

from eth_account import Account
from web3 import AsyncHTTPProvider, AsyncWeb3

ZERO32 = b"\x00" * 32

AUDIT_ANCHOR_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "anchor",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "clinicId", "type": "bytes32"},
            {"name": "treeSize", "type": "uint64"},
            {"name": "root", "type": "bytes32"},
            {"name": "prevRoot", "type": "bytes32"},
            {"name": "sthDigest", "type": "bytes32"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "latest",
        "stateMutability": "view",
        "inputs": [{"name": "clinicId", "type": "bytes32"}],
        "outputs": [{"name": "treeSize", "type": "uint64"}, {"name": "root", "type": "bytes32"}],
    },
    {
        "type": "event",
        "name": "Anchored",
        "anonymous": False,
        "inputs": [
            {"name": "clinicId", "type": "bytes32", "indexed": True},
            {"name": "treeSize", "type": "uint64", "indexed": False},
            {"name": "root", "type": "bytes32", "indexed": False},
            {"name": "sthDigest", "type": "bytes32", "indexed": False},
            {"name": "timestamp", "type": "uint256", "indexed": False},
        ],
    },
]

CLINIC_REGISTRY_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "registerClinic",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "clinicId", "type": "bytes32"},
            {"name": "keyHash", "type": "bytes32"},
            {"name": "poster", "type": "address"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "signerKeyHash",
        "stateMutability": "view",
        "inputs": [{"name": "clinicId", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
    {
        "type": "function",
        "name": "posterOf",
        "stateMutability": "view",
        "inputs": [{"name": "clinicId", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "address"}],
    },
]


def clinic_bytes32(clinic_id: uuid.UUID) -> bytes:
    """On-chain clinic id: the 16 UUID bytes followed by 16 zero bytes."""
    return clinic_id.bytes + bytes(16)


@dataclass(frozen=True)
class TxResult:
    tx_hash: str
    block_number: int | None
    succeeded: bool


class EvmAnchor:
    def __init__(
        self,
        rpc_url: str,
        anchor_address: str,
        poster_key: str | None,
        registry_address: str | None = None,
        tx_timeout: float = 120,
    ) -> None:
        self.w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url))
        self.anchor = self.w3.eth.contract(
            address=AsyncWeb3.to_checksum_address(anchor_address), abi=AUDIT_ANCHOR_ABI
        )
        self.registry = (
            self.w3.eth.contract(
                address=AsyncWeb3.to_checksum_address(registry_address), abi=CLINIC_REGISTRY_ABI
            )
            if registry_address
            else None
        )
        self.account = Account.from_key(poster_key) if poster_key else None
        self.tx_timeout = tx_timeout

    async def close(self) -> None:
        await self.w3.provider.disconnect()

    async def chain_id(self) -> int:
        return int(await self.w3.eth.chain_id)

    async def latest(self, clinic_id: uuid.UUID) -> tuple[int, bytes]:
        size, root = await self.anchor.functions.latest(clinic_bytes32(clinic_id)).call()
        return int(size), bytes(root)

    async def _send(self, account: Any, call: Any) -> str:
        tx = await call.build_transaction(
            {
                "from": account.address,
                "nonce": await self.w3.eth.get_transaction_count(account.address, "pending"),
                "chainId": await self.chain_id(),
            }
        )
        signed = account.sign_transaction(tx)
        tx_hash = await self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return "0x" + bytes(tx_hash).hex()

    async def send_anchor(
        self, clinic_id: uuid.UUID, tree_size: int, root: bytes, prev_root: bytes, digest: bytes
    ) -> str:
        if self.account is None:
            raise RuntimeError("ANCHOR_POSTER_PRIVATE_KEY is not set")
        call = self.anchor.functions.anchor(
            clinic_bytes32(clinic_id), tree_size, root, prev_root, digest
        )
        return await self._send(self.account, call)

    async def confirm_registration(
        self, admin_key: str, clinic_id: uuid.UUID, key_hash: bytes, poster: str
    ) -> str:
        """One registry admin's confirmation; the clinic is registered after the second one."""
        if self.registry is None:
            raise RuntimeError("CLINIC_REGISTRY_ADDRESS is not set")
        call = self.registry.functions.registerClinic(
            clinic_bytes32(clinic_id), key_hash, AsyncWeb3.to_checksum_address(poster)
        )
        return await self._send(Account.from_key(admin_key), call)

    async def wait(self, tx_hash: str) -> TxResult:
        receipt = await self.w3.eth.wait_for_transaction_receipt(
            tx_hash,  # type: ignore[arg-type]
            timeout=self.tx_timeout,
        )
        return TxResult(tx_hash, int(receipt["blockNumber"]), receipt["status"] == 1)

    async def receipt(self, tx_hash: str) -> TxResult | None:
        """The receipt of an earlier transaction, or None if it is not mined yet."""
        try:
            receipt = await self.w3.eth.get_transaction_receipt(tx_hash)  # type: ignore[arg-type]
        except Exception:  # web3 raises TransactionNotFound
            return None
        return TxResult(tx_hash, int(receipt["blockNumber"]), receipt["status"] == 1)

    async def signer_key_hash(self, clinic_id: uuid.UUID) -> bytes | None:
        if self.registry is None:
            return None
        value = await self.registry.functions.signerKeyHash(clinic_bytes32(clinic_id)).call()
        return bytes(value)
