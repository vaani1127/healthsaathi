"""Create .env from .env.example with fresh random secrets. Does nothing if .env already exists."""

import secrets
import sys
from collections.abc import Callable

from app.core.config import REPO_ROOT
from app.core.crypto import generate_key_spec

GENERATED: dict[str, Callable[[], str]] = {
    "JWT_SECRET": lambda: secrets.token_urlsafe(48),
    "OTP_HMAC_KEY": lambda: secrets.token_urlsafe(48),
    "DATA_KEYS": generate_key_spec,
}


def main() -> int:
    target = REPO_ROOT / ".env"
    if target.exists():
        print(".env already exists, leaving it unchanged.")
        return 0
    lines = []
    for line in (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        key = line.split("=", 1)[0]
        if key in GENERATED:
            line = f"{key}={GENERATED[key]()}"
        lines.append(line)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("Wrote .env with new random secrets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
