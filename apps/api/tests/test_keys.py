"""Derived keys (ADR 0009): the HKDF call is RFC 5869, each label has its own key, nothing leaks."""

import hashlib
import hmac
import logging

import pytest

from app.core import keys
from app.core.config import get_settings


def rfc5869_sha256(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    """HKDF written out with the hmac module, separate from keys.py: extract, then expand."""
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm, block, counter = b"", b"", 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


# RFC 5869, appendix A.1 (test case 1, SHA-256).
RFC_IKM = bytes([0x0B] * 22)
RFC_SALT = bytes(range(0x00, 0x0D))
RFC_INFO = bytes(range(0xF0, 0xFA))
RFC_OKM = bytes.fromhex(
    "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865"
)


def test_the_hkdf_call_matches_rfc5869_test_case_1() -> None:
    assert keys.hkdf_sha256(RFC_IKM, RFC_SALT, RFC_INFO, 42) == RFC_OKM


def test_the_separate_implementation_matches_rfc5869_test_case_1() -> None:
    assert rfc5869_sha256(RFC_IKM, RFC_SALT, RFC_INFO, 42) == RFC_OKM


def test_every_label_and_its_version() -> None:
    assert keys.LABELS == (
        "healthsaathi/email-code/v1",
        "healthsaathi/invite/v1",
        "healthsaathi/ip-hash/v1",
        "healthsaathi/consent-commitment/v1",
        "healthsaathi/rate-counters/v2",
        "healthsaathi/password-reset/v1",
    )


@pytest.mark.parametrize("label", keys.LABELS)
def test_each_label_gets_the_independently_computed_key(label: str) -> None:
    secret = get_settings().otp_hmac_key.get_secret_value().encode()
    expected = rfc5869_sha256(secret, b"healthsaathi/keys", label.encode(), 32)
    assert keys.derived_key(label) == expected
    message = b"some message"
    assert keys.hmac_hex(label, message) == hmac.new(expected, message, hashlib.sha256).hexdigest()


def test_every_derived_key_differs_from_the_others_and_the_secret() -> None:
    secret = get_settings().otp_hmac_key.get_secret_value().encode()
    derived = [keys.derived_key(label) for label in keys.LABELS]
    assert len(set(derived)) == len(keys.LABELS)
    assert all(len(k) == 32 for k in derived)
    assert secret not in derived
    assert all(k not in secret for k in derived)


def test_legacy_hash_is_the_raw_secret_hmac() -> None:
    secret = get_settings().otp_hmac_key.get_secret_value().encode()
    assert keys.legacy_hmac_hex(b"m") == hmac.new(secret, b"m", hashlib.sha256).hexdigest()
    assert all(keys.legacy_hmac_hex(b"m") != keys.hmac_hex(label, b"m") for label in keys.LABELS)


def test_unknown_label_is_refused() -> None:
    with pytest.raises(ValueError):
        keys.derived_key("healthsaathi/something-else/v1")


def test_no_key_bytes_in_logs_or_reprs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    derived = [keys.derived_key(label) for label in keys.LABELS]
    for label in keys.LABELS:
        keys.hmac_hex(label, b"m")
    keys.legacy_hmac_hex(b"m")
    secret = get_settings().otp_hmac_key.get_secret_value()
    forms = [secret]
    for key in derived:
        forms += [key.hex(), repr(key), key.decode("latin-1")]
    logged = caplog.text
    reprs = " ".join(repr(value) for value in vars(keys).values())
    for form in forms:
        assert form not in logged
        assert form not in reprs
