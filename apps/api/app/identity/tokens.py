"""JWTs (HS256, 32+ byte secret) and opaque refresh tokens.

Token types:
- access: 15 minutes, claims sub, sid, jti, clinic_id, role, amr.
- mfa_challenge: 5 minutes, after a correct password, before the TOTP code.
- mfa_enroll: 10 minutes, lets a staff user without TOTP set it up.
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt

from app.core.config import get_settings

TokenType = Literal["access", "mfa_challenge", "mfa_enroll"]
ALGORITHM = "HS256"
ISSUER = "healthsaathi"

CHALLENGE_MINUTES = 5
ENROLL_MINUTES = 10


class TokenError(Exception):
    pass


def _secret() -> str:
    return get_settings().jwt_secret.get_secret_value()


def encode_token(
    token_type: TokenType, subject: uuid.UUID, ttl: timedelta, **claims: Any
) -> tuple[str, datetime]:
    now = datetime.now(UTC)
    expires = now + ttl
    payload = {
        "iss": ISSUER,
        "typ": token_type,
        "sub": str(subject),
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        **claims,
    }
    return jwt.encode(payload, _secret(), algorithm=ALGORITHM), expires


def decode_token(token: str, token_type: TokenType) -> dict[str, Any]:
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            _secret(),
            algorithms=[ALGORITHM],
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "jti", "typ"]},
        )
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
    if payload.get("typ") != token_type:
        raise TokenError("wrong token type")
    return payload


def access_token(
    user_id: uuid.UUID,
    session_family: uuid.UUID,
    amr: list[str],
    clinic_id: uuid.UUID | None = None,
    role: str | None = None,
) -> tuple[str, datetime]:
    return encode_token(
        "access",
        user_id,
        timedelta(minutes=get_settings().access_token_minutes),
        sid=str(session_family),
        amr=amr,
        clinic_id=str(clinic_id) if clinic_id else None,
        role=role,
    )


def new_refresh_token() -> tuple[str, str]:
    """Return (token for the client, SHA-256 hex stored in the database)."""
    token = secrets.token_urlsafe(32)
    return token, hash_refresh_token(token)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
