import threading
import time

import pyotp

from app.core.crypto import get_keyring

ISSUER_NAME = "HealthSaathi"
STEP_SECONDS = 30

# Last accepted time step per user, so a code cannot be used twice (one VM, kept in memory).
_last_step: dict[str, int] = {}
_lock = threading.Lock()


def new_secret() -> str:
    return pyotp.random_base32()


def encrypt_secret(secret: str, user_id: str) -> bytes:
    return get_keyring().encrypt(secret.encode(), aad=f"totp:{user_id}".encode())


def decrypt_secret(blob: bytes, user_id: str) -> str:
    return get_keyring().decrypt(blob, aad=f"totp:{user_id}".encode()).decode()


def provisioning_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER_NAME)


def verify(secret: str, code: str, user_id: str, now: float | None = None) -> bool:
    """Accept the code for the current step or one step either side, but each step only once."""
    code = code.strip().replace(" ", "")
    if not (code.isdigit() and len(code) == 6):
        return False
    totp = pyotp.TOTP(secret, interval=STEP_SECONDS)
    current = int((time.time() if now is None else now) // STEP_SECONDS)
    for step in (current - 1, current, current + 1):
        if totp.at(step * STEP_SECONDS) == code:
            with _lock:
                if step <= _last_step.get(user_id, -1):
                    return False
                _last_step[user_id] = step
            return True
    return False
