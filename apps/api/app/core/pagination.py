"""Opaque cursors for keyset pagination."""

import base64
import json
from typing import Any

from pydantic import BaseModel

from app.core.errors import ProblemError

DEFAULT_LIMIT = 25
MAX_LIMIT = 100


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None


def encode_cursor(*values: Any) -> str:
    raw = json.dumps([str(v) for v in values], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, size: int) -> list[str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        values = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, TypeError) as exc:
        raise ProblemError(400, "bad-cursor") from exc
    if not isinstance(values, list) or len(values) != size:
        raise ProblemError(400, "bad-cursor")
    return [str(v) for v in values]
