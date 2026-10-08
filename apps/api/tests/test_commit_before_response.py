"""Regression: a write must be committed before its response is sent.

Found in the browser tests: registering a patient and then recording consent failed with
"patient not found" because the request's transaction was committed after the response went out,
so the next request could run before the commit.
"""

import json
import uuid
from collections.abc import MutableMapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db import models as m
from app.db.enums import Role
from app.main import create_app
from tests.helpers import make_actor, make_clinic


async def test_patient_is_committed_when_the_response_starts(admin_engine: AsyncEngine) -> None:
    clinic = await make_clinic(admin_engine)
    reception = await make_actor(admin_engine, clinic, Role.RECEPTION)
    app = create_app()
    name = f"Commit Demo {uuid.uuid4().hex[:6]}"
    body = json.dumps({"name": name, "sex": "female"}).encode()
    seen_at_start: list[int] = []

    async def count_committed() -> int:
        async with AsyncSession(admin_engine) as s:
            return int(
                await s.scalar(
                    select(func.count()).select_from(m.Patient).where(m.Patient.name == name)
                )
                or 0
            )

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            assert message["status"] == 201
            seen_at_start.append(await count_committed())

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/patients",
        "raw_path": b"/api/v1/patients",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", b"application/json"),
            (b"authorization", f"Bearer {reception.token}".encode()),
        ],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }
    await app(scope, receive, send)
    assert seen_at_start == [1]
