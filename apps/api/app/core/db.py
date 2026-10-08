"""Database engine and sessions.

Every application session runs as the `app_rw` role with the tenant context set per transaction
(`SET LOCAL ROLE app_rw` plus `app.clinic_id`, `app.user_id`, `app.role`), so row level security
applies even if the connection itself logs in as a more powerful user. The context is re-applied
at the start of every transaction, so a commit in the middle of a request cannot drop it.
"""

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

APP_ROLE = "app_rw"
_TENANT_KEY = "tenant"


@dataclass(frozen=True, slots=True)
class TenantContext:
    clinic_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    role: str | None = None


ANONYMOUS = TenantContext()


@event.listens_for(Session, "after_begin")
def _apply_tenant_context(
    session: Session, transaction: SessionTransaction, connection: Connection
) -> None:
    ctx = session.info.get(_TENANT_KEY)
    if not isinstance(ctx, TenantContext):
        return
    connection.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
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
    return create_async_engine(get_settings().database_url, pool_pre_ping=True)


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
