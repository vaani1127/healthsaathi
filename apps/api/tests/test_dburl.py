from sqlalchemy.engine import make_url

from app.core.dburl import asyncpg_dsn, asyncpg_url, engine_options, is_pooled

NEON_POOLED = (
    "postgresql://user:pw@ep-cool-1-pooler.ap-southeast-1.aws.neon.tech/neondb"
    "?sslmode=require&channel_binding=require"
)
NEON_DIRECT = "postgres://user:pw@ep-cool-1.ap-southeast-1.aws.neon.tech/neondb?sslmode=require"


def test_hosted_urls_become_asyncpg_urls_with_ssl() -> None:
    url = make_url(asyncpg_url(NEON_POOLED))
    assert url.drivername == "postgresql+asyncpg"
    assert dict(url.query) == {"ssl": "require"}
    assert url.password == "pw"
    assert make_url(asyncpg_url(NEON_DIRECT)).drivername == "postgresql+asyncpg"


def test_local_urls_are_unchanged() -> None:
    local = "postgresql+asyncpg://healthsaathi:healthsaathi@localhost:5432/healthsaathi"
    assert asyncpg_url(local) == local
    assert asyncpg_url("sqlite:///x.db") == "sqlite:///x.db"


def test_pooled_endpoints_get_no_statement_caches() -> None:
    assert is_pooled(NEON_POOLED) and not is_pooled(NEON_DIRECT)
    pooled = engine_options(asyncpg_url(NEON_POOLED))
    assert pooled["pool_pre_ping"] is True
    assert pooled["connect_args"]["statement_cache_size"] == 0
    assert pooled["connect_args"]["prepared_statement_cache_size"] == 0
    name = pooled["connect_args"]["prepared_statement_name_func"]
    assert name() != name()
    assert "connect_args" not in engine_options(asyncpg_url(NEON_DIRECT))
    supported = engine_options(asyncpg_url(NEON_POOLED), pooler_prepared=True)["connect_args"]
    assert "statement_cache_size" not in supported, "caches stay on when the pooler supports them"
    assert callable(supported["prepared_statement_name_func"])


def test_listen_dsn_keeps_ssl() -> None:
    dsn = make_url(asyncpg_dsn(NEON_DIRECT))
    assert dsn.drivername == "postgresql" and dict(dsn.query) == {"sslmode": "require"}
