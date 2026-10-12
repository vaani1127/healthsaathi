"""Dev mailbox: the emails the console backend wrote, so a local demo can read sign-in codes.

The route answers only when APP_ENV=local and returns 404 everywhere else. It is always
registered so the OpenAPI document is the same in every environment.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.errors import ProblemError
from app.notify.email import outbox

router = APIRouter(tags=["dev"])
LIMIT = 50


class MailOut(BaseModel):
    to: str
    subject: str
    text: str
    sent_at: datetime | None


def _from_folder(folder: Path) -> list[MailOut]:
    if not folder.is_dir():
        return []
    files = sorted(folder.glob("*.json"), key=lambda p: p.name, reverse=True)[:LIMIT]
    mails = []
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        stamp = path.name.split("-", 1)[0]
        sent = datetime.fromtimestamp(int(stamp) / 1e9, UTC) if stamp.isdigit() else None
        mails.append(
            MailOut(to=data["to"], subject=data["subject"], text=data["text"], sent_at=sent)
        )
    return mails


@router.get("/dev/mailbox", response_model=list[MailOut])
async def mailbox(to: str | None = None) -> list[MailOut]:
    """Local only: the newest emails first, optionally for one address."""
    settings = get_settings()
    if settings.env != "local" or settings.email_backend != "console":
        raise ProblemError(404, "not-found")
    if settings.email_outbox_dir:
        mails = _from_folder(Path(settings.email_outbox_dir))
    else:
        mails = [
            MailOut(to=m.to, subject=m.subject, text=m.text, sent_at=None)
            for m in reversed(outbox.sent[-LIMIT:])
        ]
    if to:
        mails = [m for m in mails if m.to.lower() == to.strip().lower()]
    return mails
