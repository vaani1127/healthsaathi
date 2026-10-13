"""Database URLs for SQLAlchemy (asyncpg) from the forms hosted Postgres gives out.

Neon, Azure and others hand out `postgres://` or `postgresql://` URLs with libpq options such as
`sslmode=require` and `channel_binding=require`. asyncpg through SQLAlchemy needs the
`postgresql+asyncpg://` scheme and an `ssl` option instead, and does not know `channel_binding`.
A pooled endpoint (PgBouncer in transaction mode, for example a Neon host containing "-pooler")
cannot keep prepared statements between transactions, so their caches are turned off there.
"""

import uuid
from typing import Any

from sqlalchemy.engine import URL, make_url

LIBPQ_ONLY = ("channel_binding", "target_session_attrs", "gssencmode")


def is_pooled(url: str | URL) -> bool:
    host = make_url(url).host or ""
    return "-pooler" in host


def asyncpg_url(url: str) -> str:
    """The SQLAlchemy asyncpg form of a Postgres URL; other URLs are returned unchanged."""
    parsed = make_url(url)
    if parsed.drivername not in ("postgres", "postgresql", "postgresql+asyncpg"):
        return url
    query = dict(parsed.query)
    for name in LIBPQ_ONLY:
        query.pop(name, None)
    sslmode = query.pop("sslmode", None)
    if sslmode and "ssl" not in query:
        query["ssl"] = "require" if sslmode in ("require", "verify-ca", "verify-full") else sslmode
    parsed = parsed.set(drivername="postgresql+asyncpg", query=query)
    return parsed.render_as_string(hide_password=False)


def asyncpg_dsn(url: str) -> str:
    """A plain DSN for asyncpg.connect (used for LISTEN), keeping SSL."""
    parsed = make_url(asyncpg_url(url))
    query = dict(parsed.query)
    ssl = query.pop("ssl", None)
    if ssl:
        query["sslmode"] = ssl
    return parsed.set(drivername="postgresql", query=query).render_as_string(hide_password=False)


def engine_options(url: str, pooler_prepared: bool = False) -> dict[str, Any]:
    """Options for create_async_engine: check connections before use (a sleeping Neon compute
    closes them) and recycle idle ones. Behind a pooler, prepared statements get unique names;
    their caches stay on only when the pooler supports prepared statements (PgBouncer 1.21 and
    later with max_prepared_statements, as Neon's does), which saves two round trips a query."""
    options: dict[str, Any] = {"pool_pre_ping": True, "pool_recycle": 300}
    if is_pooled(url):
        connect_args: dict[str, Any] = {
            "prepared_statement_name_func": lambda: f"__hs_{uuid.uuid4().hex}__",
        }
        if not pooler_prepared:
            connect_args.update(statement_cache_size=0, prepared_statement_cache_size=0)
        options["connect_args"] = connect_args
    return options
