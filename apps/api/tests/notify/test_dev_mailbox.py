from pathlib import Path

import pytest
from httpx import AsyncClient

from app.core.config import get_settings
from app.notify.email import Email, send_email


async def test_mailbox_is_404_outside_local(client: AsyncClient) -> None:
    assert get_settings().env == "test"
    assert (await client.get("/api/v1/dev/mailbox")).status_code == 404


async def test_mailbox_lists_console_emails_in_local(
    client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "env", "local")
    monkeypatch.setattr(settings, "email_outbox_dir", str(tmp_path))
    await send_email(Email(to="one@mailbox.test", subject="Code", text="Your code is 123456"))
    await send_email(Email(to="two@mailbox.test", subject="Hello", text="Second"))

    mails = (await client.get("/api/v1/dev/mailbox")).json()
    assert [m["to"] for m in mails[:2]] == ["two@mailbox.test", "one@mailbox.test"]
    assert mails[0]["sent_at"] is not None
    one = (await client.get("/api/v1/dev/mailbox", params={"to": "ONE@mailbox.test"})).json()
    assert [m["text"] for m in one] == ["Your code is 123456"]

    monkeypatch.setattr(settings, "email_backend", "brevo")
    assert (await client.get("/api/v1/dev/mailbox")).status_code == 404
