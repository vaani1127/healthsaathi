import time

import pyotp

from app.core.crypto import get_keyring

ISSUER_NAME = "HealthSaathi"
STEP_SECONDS = 30


def new_secret() -> str:
    return pyotp.random_base32()


def encrypt_secret(secret: str, user_id: str) -> bytes:
    return get_keyring().encrypt(secret.encode(), aad=f"totp:{user_id}".encode())


def decrypt_secret(blob: bytes, user_id: str) -> str:
    return get_keyring().decrypt(blob, aad=f"totp:{user_id}".encode()).decode()


def provisioning_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER_NAME)


def matching_step(secret: str, code: str, now: float | None = None) -> int | None:
    """The time step the code belongs to, if it is the current step or one step either side.

    This only checks the code. Each step is accepted once per user: the caller claims the step in
    the database (`identity.service._claim_step`), so the rule holds across API workers and
    restarts.
    """
    code = code.strip().replace(" ", "")
    if not (code.isdigit() and len(code) == 6):
        return None
    totp = pyotp.TOTP(secret, interval=STEP_SECONDS)
    current = int((time.time() if now is None else now) // STEP_SECONDS)
    for step in (current - 1, current, current + 1):
        if totp.at(step * STEP_SECONDS) == code:
            return step
    return None
