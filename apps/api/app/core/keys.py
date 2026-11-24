"""One key per use, derived from OTP_HMAC_KEY (ADR 0009).

Each use of the secret gets its own 32-byte key from HKDF-SHA256 (RFC 5869) with a fixed salt and
a versioned label as `info`. A key leaked or misused in one place says nothing about the others,
and a label can move to v2 without touching the rest. No new environment variables.

Rows written before the cutover were made with HMAC under the raw secret and carry no marker.
To check one, try the derived key first, then `legacy_hmac_hex` (ADR 0009).

Derived keys are never logged and never returned by an endpoint: this module only hands out
digests, and the one function that returns key bytes (`derived_key`) is for building a keyed hash.
"""

import hashlib
import hmac
from functools import lru_cache

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.config import get_settings

SALT = b"healthsaathi/keys"
KEY_BYTES = 32

EMAIL_CODE = "healthsaathi/email-code/v1"
INVITE = "healthsaathi/invite/v1"
IP_HASH = "healthsaathi/ip-hash/v1"
CONSENT_COMMITMENT = "healthsaathi/consent-commitment/v1"
RATE_COUNTERS = "healthsaathi/rate-counters/v2"

LABELS = (EMAIL_CODE, INVITE, IP_HASH, CONSENT_COMMITMENT, RATE_COUNTERS)


def hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int = KEY_BYTES) -> bytes:
    """RFC 5869 HKDF with SHA-256 (extract, then expand), from the `cryptography` library."""
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def _secret() -> bytes:
    return get_settings().otp_hmac_key.get_secret_value().encode()


@lru_cache(maxsize=len(LABELS) * 2)
def _derive(secret: bytes, label: str) -> bytes:
    return hkdf_sha256(secret, SALT, label.encode())


def derived_key(label: str) -> bytes:
    """The key for one use. Only for keying a hash; never log it or send it anywhere."""
    if label not in LABELS:
        raise ValueError("unknown key label")
    return _derive(_secret(), label)


def hmac_hex(label: str, message: bytes) -> str:
    """HMAC-SHA256 of `message` under the derived key for `label`, as hex."""
    return hmac.new(derived_key(label), message, hashlib.sha256).hexdigest()


def legacy_hmac_hex(message: bytes) -> str:
    """HMAC-SHA256 under the raw OTP_HMAC_KEY: how rows were hashed before the derived keys.
    Only for reading rows written before the cutover; never for writing new ones."""
    return hmac.new(_secret(), message, hashlib.sha256).hexdigest()
