"""Dev mailbox (local only) and web push subscriptions.

Dev mailbox: the emails the console backend wrote, so a local demo can read sign-in codes.

The route answers only when APP_ENV=local and returns 404 everywhere else. It is always
registered so the OpenAPI document is the same in every environment.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field, HttpUrl

from app.core.config import get_settings
from app.core.db import TenantContext, tenant_session
from app.core.errors import ProblemError
from app.identity.deps import CurrentPrincipal
from app.notify import push
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


push_router = APIRouter(tags=["push"])
# Push services of Chrome, Firefox, Edge and Safari.
PUSH_SERVICES = (
    "fcm.googleapis.com",
    "push.services.mozilla.com",
    "notify.windows.com",
    "push.apple.com",
)


class PushKeyOut(BaseModel):
    public_key: str | None


class PushKeys(BaseModel):
    p256dh: str = Field(min_length=10, max_length=200)
    auth: str = Field(min_length=8, max_length=100)


class PushSubscriptionIn(BaseModel):
    endpoint: HttpUrl
    keys: PushKeys


class PushUnsubscribeIn(BaseModel):
    endpoint: HttpUrl


@push_router.get("/push/key", response_model=PushKeyOut)
async def push_key() -> PushKeyOut:
    """The VAPID public key for the browser, or null when push is off here."""
    return PushKeyOut(public_key=get_settings().vapid_public_key if push.enabled() else None)


@push_router.post("/push/subscriptions", status_code=status.HTTP_204_NO_CONTENT)
async def push_subscribe(principal: CurrentPrincipal, body: PushSubscriptionIn) -> Response:
    if not push.enabled():
        raise ProblemError(404, "push-off", "Notifications are not available here.")
    host = (body.endpoint.host or "").lower()
    if body.endpoint.scheme != "https" or not any(
        host == h or host.endswith("." + h) for h in PUSH_SERVICES
    ):
        # The worker posts to this URL, so only the browsers' own push services are accepted.
        raise ProblemError(422, "validation-error", "This is not a browser push service.")
    async with tenant_session(TenantContext(user_id=principal.user_id)) as db:
        await push.subscribe(
            db, principal.user_id, str(body.endpoint), body.keys.p256dh, body.keys.auth
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@push_router.post("/push/subscriptions/remove", status_code=status.HTTP_204_NO_CONTENT)
async def push_unsubscribe(principal: CurrentPrincipal, body: PushUnsubscribeIn) -> Response:
    async with tenant_session(TenantContext(user_id=principal.user_id)) as db:
        await push.unsubscribe(db, principal.user_id, str(body.endpoint))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
