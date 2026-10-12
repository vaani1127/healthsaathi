"""The demo loader writes many rows with fixed ids, so it gets its own fresh database."""

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

DEMO_DB = "healthsaathi_demo_test"
_base = make_url(
    os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://healthsaathi:healthsaathi@localhost:5432/healthsaathi"
    )
)
DEMO_DB_URL = _base.set(database=DEMO_DB).render_as_string(hide_password=False)
API_DIR = Path(__file__).resolve().parents[3] / "apps" / "api"

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-" + "x" * 32)
os.environ.setdefault("OTP_HMAC_KEY", "test-otp-key-" + "y" * 32)
os.environ.setdefault("DATA_KEYS", "t1:" + "A" * 43 + "=")
os.environ.setdefault("LEDGER_SIGNING_SEED", "test-ledger-seed-" + "z" * 32)


async def _recreate() -> None:
    import asyncpg

    admin = _base.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )
    conn = await asyncpg.connect(admin)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{DEMO_DB}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{DEMO_DB}"')
    finally:
        await conn.close()


def _clear_caches() -> None:
    from app.core.config import get_settings
    from app.core.db import get_engine, get_sessionmaker

    get_sessionmaker.cache_clear()
    get_engine.cache_clear()
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def demo_database() -> Iterator[str]:
    from alembic import command
    from alembic.config import Config

    asyncio.run(_recreate())
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "migrations"))
    cfg.attributes["database_url"] = DEMO_DB_URL
    command.upgrade(cfg, "head")
    yield DEMO_DB_URL


@pytest.fixture(scope="module")
async def demo_app(demo_database: str) -> AsyncIterator[str]:
    """Point the app's settings and engine at the demo database for this module only."""
    from app.core.db import get_engine

    await get_engine().dispose()
    saved = {k: os.environ.get(k) for k in ("DATABASE_URL", "MIGRATOR_DATABASE_URL", "MODEL_DIR")}
    os.environ["DATABASE_URL"] = demo_database
    os.environ["MIGRATOR_DATABASE_URL"] = demo_database
    os.environ["MODEL_DIR"] = tempfile.mkdtemp(prefix="hs-demo-models-")
    _clear_caches()
    try:
        yield demo_database
    finally:
        await get_engine().dispose()
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        _clear_caches()
