import asyncio
import os
import re
from typing import Any

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.db import models  # noqa: F401  (registers tables)
from app.db.base import Base

config = context.config
target_metadata = Base.metadata


# Monthly partitions are created by create_month_partitions(), not by the models.
PARTITION = re.compile(r"_(\d{6}|default)$")


def include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    table = obj if type_ == "table" else getattr(obj, "table", None)
    table_name = getattr(table, "name", None) or ""
    return not (reflected and compare_to is None and PARTITION.search(table_name))


def database_url() -> str:
    # Read the variable directly first, so CI and deploy jobs can migrate without the app secrets.
    url = (
        config.attributes.get("database_url")
        or os.environ.get("MIGRATOR_DATABASE_URL")
        or get_settings().migrator_database_url
    )
    return str(url)


def run_migrations_offline() -> None:
    context.configure(url=database_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(database_url())
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        do_run_migrations(connection)
        return
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
