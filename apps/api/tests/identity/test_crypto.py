import base64
import os

import pytest
from cryptography.exceptions import InvalidTag

from app.core.crypto import KeyRing
from app.core.errors import ProblemError
from app.identity import passwords
from app.identity.password_policy import check_password


def key(key_id: str) -> str:
    return f"{key_id}:{base64.b64encode(os.urandom(32)).decode()}"


def test_round_trip_with_aad() -> None:
    ring = KeyRing(key("k1"))
    blob = ring.encrypt(b"secret", aad=b"note:1")
    assert blob.startswith(b"k1:")
    assert ring.decrypt(blob, aad=b"note:1") == b"secret"
    with pytest.raises(InvalidTag):
        ring.decrypt(blob, aad=b"note:2")


def test_rotation_keeps_old_data_readable() -> None:
    old_key = key("k1")
    old = KeyRing(old_key).encrypt(b"old")
    ring = KeyRing(f"{key('k2')},{old_key}")
    assert ring.active_id == "k2"
    assert ring.decrypt(old) == b"old"
    assert ring.encrypt(b"new").startswith(b"k2:")


def test_unknown_key_and_bad_specs() -> None:
    with pytest.raises(ValueError):
        KeyRing(key("k1")).decrypt(KeyRing(key("k9")).encrypt(b"x"))
    with pytest.raises(ValueError):
        KeyRing("k1:" + base64.b64encode(b"short").decode())
    with pytest.raises(ValueError):
        KeyRing("")


def test_tampered_ciphertext_fails() -> None:
    ring = KeyRing(key("k1"))
    blob = bytearray(ring.encrypt(b"secret"))
    blob[-1] ^= 1
    with pytest.raises(InvalidTag):
        ring.decrypt(bytes(blob))


def test_password_hashing_uses_argon2id() -> None:
    hashed = passwords.hash_password("long enough password")
    assert hashed.startswith("$argon2id$")
    assert passwords.verify_password(hashed, "long enough password")
    assert not passwords.verify_password(hashed, "wrong password!")
    assert not passwords.verify_password("not-a-hash", "x")


@pytest.mark.parametrize("weak", ["short", "1234567890", "Password123", "qwertyuiop"])
def test_weak_passwords_are_rejected(weak: str) -> None:
    with pytest.raises(ProblemError):
        check_password(weak)


def test_password_equal_to_email_is_rejected() -> None:
    with pytest.raises(ProblemError):
        check_password("someone@clinic.test", "someone@clinic.test")
    check_password("a sensible pass phrase", "someone@clinic.test")


def test_totp_code_cannot_be_reused() -> None:
    import pyotp

    from app.identity import totp

    secret = totp.new_secret()
    now = 1_900_000_000.0
    code = pyotp.TOTP(secret).at(int(now))
    assert totp.verify(secret, code, "user-replay", now=now)
    assert not totp.verify(secret, code, "user-replay", now=now)
    # The next step's code still works.
    later = pyotp.TOTP(secret).at(int(now) + 30)
    assert totp.verify(secret, later, "user-replay", now=now + 30)
    assert not totp.verify(secret, "12345", "user-replay", now=now)


def test_default_limit_is_keyed_by_user() -> None:
    import uuid

    from starlette.requests import Request

    from app.core.ratelimit import user_or_ip
    from app.identity import tokens

    user = uuid.uuid4()
    token, _ = tokens.access_token(user, uuid.uuid4(), ["pwd", "otp"])

    def request(headers: dict[str, str]) -> Request:
        raw = [(k.encode(), v.encode()) for k, v in headers.items()]
        return Request({"type": "http", "headers": raw, "client": ("10.0.0.1", 1)})

    assert user_or_ip(request({"authorization": f"Bearer {token}"})) == f"user:{user}"
    assert user_or_ip(request({"authorization": "Bearer junk"})) == "ip:10.0.0.1"
    assert user_or_ip(request({})) == "ip:10.0.0.1"
