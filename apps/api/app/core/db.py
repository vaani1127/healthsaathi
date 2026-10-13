"""Database engine and sessions.

Every application session runs as the `app_rw` role with the tenant context set per transaction
(`SET LOCAL ROLE app_rw` plus `app.clinic_id`, `app.user_id`, `app.role`), so row level security
applies even if the connection itself logs in as a more powerful user. The context is re-applied
at the start of every transaction, so a commit in the middle of a request cannot drop it.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from sqlalchemy import Connection, event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, SessionTransaction

from app.core.config import get_settings
from app.core.dburl import engine_options

logger = logging.getLogger(__name__)

APP_ROLE = "app_rw"
_TENANT_KEY = "tenant"


DB_ROLES = frozenset({"app_rw", "anchor_job"})


@dataclass(frozen=True, slots=True)
class TenantContext:
    clinic_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    role: str | None = None
    # The database role for the transaction. Only the anchor job uses anything but app_rw.
    db_role: str = APP_ROLE


ANONYMOUS = TenantContext()


@event.listens_for(Session, "after_begin")
def _apply_tenant_context(
    session: Session, transaction: SessionTransaction, connection: Connection
) -> None:
    ctx = session.info.get(_TENANT_KEY)
    if not isinstance(ctx, TenantContext):
        return
    if ctx.db_role not in DB_ROLES:
        raise ValueError(f"unknown database role {ctx.db_role!r}")
    connection.execute(text(f"SET LOCAL ROLE {ctx.db_role}"))
    connection.execute(
        text(
            "SELECT set_config('app.clinic_id', :clinic_id, true),"
            " set_config('app.user_id', :user_id, true),"
            " set_config('app.role', :role, true)"
        ),
        {
            "clinic_id": str(ctx.clinic_id) if ctx.clinic_id else "",
            "user_id": str(ctx.user_id) if ctx.user_id else "",
            "role": ctx.role or "",
        },
    )


@lru_cache
def get_engine() -> AsyncEngine:
    url = get_settings().database_url
    return create_async_engine(url, **engine_options(url))


async def wait_for_database(attempts: int = 8) -> None:
    """Wait for the database to answer. A hosted database that sleeps when idle (Neon) takes a
    few seconds to wake on the first connection; the app retries instead of failing to start."""
    delay = 1.0
    for attempt in range(1, attempts + 1):
        try:
            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
            return
        except Exception:
            if attempt == attempts:
                raise
            logger.warning("database not reachable yet, retrying", extra={"attempt": attempt})
            await asyncio.sleep(delay)
            delay = min(delay * 2, 8.0)


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


def new_session(ctx: TenantContext = ANONYMOUS, **kwargs: Any) -> AsyncSession:
    session = get_sessionmaker()(**kwargs)
    session.info[_TENANT_KEY] = ctx
    return session


@asynccontextmanager
async def tenant_session(ctx: TenantContext) -> AsyncIterator[AsyncSession]:
    """A session bound to one tenant context, committed on success and rolled back on error."""
    async with new_session(ctx) as session, session.begin():
        yield session


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency for routes that need no tenant (health checks, login lookups)."""
    async with new_session(ANONYMOUS) as session:
        yield session
