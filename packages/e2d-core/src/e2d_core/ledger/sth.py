"""Signed tree heads (SPEC 6).

The signed bytes are JCS({clinic_id, tree_size, root_hex, prev_root_hex, timestamp, key_id}) and the
signature is Ed25519. key_id is the hex of the first 8 bytes of SHA-256(raw public key).

Clinic signing keys are derived from one secret seed with HKDF-SHA256 (info = clinic id and key
version), so only the seed has to be kept secret and backed up.
"""

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from e2d_core.ledger.jcs import canonicalize


@dataclass(frozen=True)
class TreeHead:
    clinic_id: str
    tree_size: int
    root_hex: str
    prev_root_hex: str | None
    timestamp: int  # milliseconds since the Unix epoch, UTC
    key_id: str

    def to_json(self) -> dict[str, Any]:
        return {
            "clinic_id": self.clinic_id,
            "tree_size": self.tree_size,
            "root_hex": self.root_hex,
            "prev_root_hex": self.prev_root_hex,
            "timestamp": self.timestamp,
            "key_id": self.key_id,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "TreeHead":
        return cls(
            clinic_id=str(data["clinic_id"]),
            tree_size=int(data["tree_size"]),
            root_hex=str(data["root_hex"]),
            prev_root_hex=data.get("prev_root_hex"),
            timestamp=int(data["timestamp"]),
            key_id=str(data["key_id"]),
        )

    def signing_bytes(self) -> bytes:
        return canonicalize(self.to_json())

    def digest(self) -> bytes:
        """SHA-256 of the signed bytes; this is what gets anchored on public witnesses."""
        return hashlib.sha256(self.signing_bytes()).digest()


def public_key_bytes(key: Ed25519PrivateKey | Ed25519PublicKey) -> bytes:
    public = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def key_id(public_key: bytes) -> str:
    return hashlib.sha256(public_key).digest()[:8].hex()


def derive_clinic_key(seed: bytes, clinic_id: uuid.UUID, version: int = 1) -> Ed25519PrivateKey:
    if len(seed) < 32:
        raise ValueError("seed must be at least 32 bytes")
    raw = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"healthsaathi-sth",
        info=f"clinic:{clinic_id}:v{version}".encode(),
    ).derive(seed)
    return Ed25519PrivateKey.from_private_bytes(raw)


def sign(head: TreeHead, key: Ed25519PrivateKey) -> bytes:
    if head.key_id != key_id(public_key_bytes(key)):
        raise ValueError("key_id does not match the signing key")
    return key.sign(head.signing_bytes())


def verify(head: TreeHead, signature: bytes, public_key: bytes) -> bool:
    if head.key_id != key_id(public_key):
        return False
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, head.signing_bytes())
    except (InvalidSignature, ValueError):
        return False
    return True
