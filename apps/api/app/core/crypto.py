"""AES-256-GCM for data at rest (clinical note bodies, TOTP secrets).

Stored format: `key_id` (ASCII) + b":" + 12-byte nonce + ciphertext with tag. The key id prefix lets
keys be rotated: new data uses the first key in DATA_KEYS, old data stays readable while its key is
still listed.
"""

import base64
import os
from functools import lru_cache

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings

NONCE_BYTES = 12


class KeyRing:
    def __init__(self, spec: str) -> None:
        self._keys: dict[str, AESGCM] = {}
        self.active_id = ""
        for item in spec.split(","):
            item = item.strip()
            if not item:
                continue
            key_id, _, encoded = item.partition(":")
            key = base64.b64decode(encoded)
            if not key_id or len(key) != 32 or not key_id.isalnum():
                raise ValueError("DATA_KEYS entries must be key_id:base64 of 32 bytes")
            self._keys[key_id] = AESGCM(key)
            self.active_id = self.active_id or key_id
        if not self._keys:
            raise ValueError("DATA_KEYS is empty")

    def encrypt(self, plaintext: bytes, aad: bytes = b"") -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = self._keys[self.active_id].encrypt(nonce, plaintext, aad)
        return self.active_id.encode() + b":" + nonce + ciphertext

    def decrypt(self, blob: bytes, aad: bytes = b"") -> bytes:
        key_id, sep, rest = blob.partition(b":")
        if not sep:
            raise ValueError("missing key id")
        key = self._keys.get(key_id.decode())
        if key is None:
            raise ValueError("unknown key id")
        return key.decrypt(rest[:NONCE_BYTES], rest[NONCE_BYTES:], aad)


@lru_cache
def get_keyring() -> KeyRing:
    return KeyRing(get_settings().data_keys.get_secret_value())


def generate_key_spec(key_id: str = "k1") -> str:
    return f"{key_id}:{base64.b64encode(os.urandom(32)).decode()}"
