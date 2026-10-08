"""WebSocket endpoint. The access token is sent in the first message, never in the URL."""

import asyncio
import contextlib
import json
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.db.enums import STAFF_ROLES, Role
from app.identity import service, tokens
from app.realtime.hub import hub

router = APIRouter()

AUTH_TIMEOUT_SECONDS = 5
POLICY_VIOLATION = 1008


async def _authenticate(ws: WebSocket) -> uuid.UUID | None:
    try:
        raw = await asyncio.wait_for(ws.receive_text(), AUTH_TIMEOUT_SECONDS)
        message = json.loads(raw)
        if message.get("type") != "auth":
            return None
        claims = tokens.decode_token(str(message.get("token", "")), "access")
    except (TimeoutError, ValueError, tokens.TokenError):
        return None
    if not claims.get("clinic_id") or not claims.get("role"):
        return None
    if Role(claims["role"]) in STAFF_ROLES and "otp" not in (claims.get("amr") or []):
        return None
    if not await service.session_is_active(uuid.UUID(claims["sid"])):
        return None
    return uuid.UUID(claims["clinic_id"])


@router.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    await ws.accept()
    clinic_id = await _authenticate(ws)
    if clinic_id is None:
        await ws.close(code=POLICY_VIOLATION)
        return

    queue = await hub.subscribe(clinic_id)
    await ws.send_json({"type": "ready"})

    async def pump() -> None:
        while True:
            await ws.send_json(await queue.get())

    sender = asyncio.create_task(pump())
    try:
        while True:
            message = await ws.receive_text()
            if message == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        sender.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sender
        hub.unsubscribe(clinic_id, queue)
