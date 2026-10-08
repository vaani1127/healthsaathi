"""Transactional email. Brevo free tier in staging and prod; the console in local and test."""

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

BREVO_URL = "https://api.brevo.com/v3/smtp/email"


@dataclass(frozen=True)
class Email:
    to: str
    subject: str
    text: str


@dataclass
class Outbox:
    """Keeps console emails in memory so tests can read the codes that were sent."""

    sent: list[Email] = field(default_factory=list)

    def last_to(self, address: str) -> Email | None:
        for email in reversed(self.sent):
            if email.to.lower() == address.lower():
                return email
        return None


outbox = Outbox()


def _write_outbox(folder: Path, email: Email) -> None:
    """Browser tests read sign-in codes from here (local and test only)."""
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}-{email.to.replace('@', '_at_')}.json"
    (folder / name).write_text(json.dumps(asdict(email)), encoding="utf-8")


async def send_email(email: Email) -> None:
    settings = get_settings()
    if settings.email_backend == "console":
        outbox.sent.append(email)
        logger.info("email (console)", extra={"to": email.to, "subject": email.subject})
        if settings.env == "local":
            # Only in local dev, so developers can log in without a mail server.
            logger.info("email body", extra={"to": email.to, "body": email.text})
        if settings.email_outbox_dir and settings.env in ("local", "test"):
            await asyncio.to_thread(_write_outbox, Path(settings.email_outbox_dir), email)
        return

    if settings.brevo_api_key is None:
        raise RuntimeError("BREVO_API_KEY is not set")
    payload = {
        "sender": {"email": settings.email_from, "name": settings.email_from_name},
        "to": [{"email": email.to}],
        "subject": email.subject,
        "textContent": email.text,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            BREVO_URL,
            json=payload,
            headers={"api-key": settings.brevo_api_key.get_secret_value()},
        )
    if resp.status_code >= 300:
        logger.error("email send failed", extra={"status": resp.status_code})
        raise RuntimeError("email send failed")
