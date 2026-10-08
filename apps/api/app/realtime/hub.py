"""Realtime events over WebSocket, fanned out with Postgres LISTEN/NOTIFY.

Services call `notify()` inside their transaction; Postgres delivers the message only if the
transaction commits. Each API worker keeps one LISTEN connection and passes messages to its own
WebSocket clients for that clinic. Messages carry ids and types only, never patient data: clients
refetch through the normal API, which records the access.
"""

import asyncio
import contextlib
import json
import logging
import uuid
from collections import defaultdict
from typing import Any

import asyncpg
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings

logger = logging.getLogger(__name__)

CHANNEL = "hs_events"


async def notify(db: AsyncSession, clinic_id: uuid.UUID, kind: str, **data: Any) -> None:
    payload = json.dumps({"clinic_id": str(clinic_id), "type": kind, "data": data}, default=str)
    await db.execute(
        text("SELECT pg_notify(:channel, :payload)"), {"channel": CHANNEL, "payload": payload}
    )


class Hub:
    def __init__(self) -> None:
        self._queues: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._conn: asyncpg.Connection | None = None
        self._lock = asyncio.Lock()

    async def _ensure_listening(self) -> None:
        async with self._lock:
            if self._conn is not None and not self._conn.is_closed():
                return
            url = make_url(get_settings().database_url).set(drivername="postgresql")
            self._conn = await asyncpg.connect(url.render_as_string(hide_password=False))
            await self._conn.add_listener(CHANNEL, self._on_message)

    def _on_message(self, _conn: object, _pid: int, _channel: str, payload: str) -> None:
        try:
            message = json.loads(payload)
        except ValueError:
            return
        for queue in list(self._queues.get(message.get("clinic_id", ""), ())):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait({"type": message["type"], "data": message.get("data", {})})

    async def subscribe(self, clinic_id: uuid.UUID) -> asyncio.Queue[dict[str, Any]]:
        await self._ensure_listening()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._queues[str(clinic_id)].add(queue)
        return queue

    def unsubscribe(self, clinic_id: uuid.UUID, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._queues[str(clinic_id)].discard(queue)

    async def close(self) -> None:
        if self._conn is not None and not self._conn.is_closed():
            await self._conn.close()
        self._conn = None


hub = Hub()
