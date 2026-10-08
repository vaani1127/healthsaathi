"""Transactional email. Brevo free tier in staging and prod; the console in local and test."""

import logging
from dataclasses import dataclass, field

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


async def send_email(email: Email) -> None:
    settings = get_settings()
    if settings.email_backend == "console":
        outbox.sent.append(email)
        logger.info("email (console)", extra={"to": email.to, "subject": email.subject})
        if settings.env == "local":
            # Only in local dev, so developers can log in without a mail server.
            logger.info("email body", extra={"to": email.to, "body": email.text})
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
