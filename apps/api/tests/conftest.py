"""Test setup: a fresh Postgres database migrated with Alembic (no SQLite, no create_all)."""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

TEST_DB_NAME = "healthsaathi_test"
_base_url = make_url(
    os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://healthsaathi:healthsaathi@localhost:5432/healthsaathi"
    )
)
TEST_DB_URL = _base_url.set(database=TEST_DB_NAME).render_as_string(hide_password=False)
os.environ["DATABASE_URL"] = TEST_DB_URL
os.environ["MIGRATOR_DATABASE_URL"] = TEST_DB_URL
os.environ["APP_ENV"] = "test"
os.environ["COOKIE_SECURE"] = "false"
os.environ["EMAIL_BACKEND"] = "console"
# Fixed test-only secrets. Real ones come from .env or deployment secrets.
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-" + "x" * 32)
os.environ.setdefault("OTP_HMAC_KEY", "test-otp-key-" + "y" * 32)
os.environ.setdefault("DATA_KEYS", "t1:" + "A" * 43 + "=")

import asyncpg  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine  # noqa: E402

from app.main import create_app  # noqa: E402

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


async def _recreate_database() -> None:
    admin_dsn = _base_url.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    finally:
        await conn.close()


def alembic_config() -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.attributes["database_url"] = TEST_DB_URL
    return cfg


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> Iterator[str]:
    asyncio.run(_recreate_database())
    command.upgrade(alembic_config(), "head")
    yield TEST_DB_URL


@pytest.fixture(scope="session")
async def admin_engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    """Connects as the database owner. Bypasses RLS; use only to arrange and inspect data."""
    engine = create_async_engine(migrated_database)
    yield engine
    await engine.dispose()


@pytest.fixture(autouse=True)
def reset_rate_limits() -> None:
    from app.core.ratelimit import reset

    reset()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
