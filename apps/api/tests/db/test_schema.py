"""Schema drift: the migrated database must match the SQLAlchemy models."""

from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import models  # noqa: F401
from app.db.base import Base

# information_schema data_type for each compiled Postgres type name.
TYPE_NAMES = {
    "UUID": "uuid",
    "TEXT": "text",
    "VARCHAR": "character varying",
    "BOOLEAN": "boolean",
    "INTEGER": "integer",
    "SMALLINT": "smallint",
    "BIGINT": "bigint",
    "DATE": "date",
    "TIME WITHOUT TIME ZONE": "time without time zone",
    "TIMESTAMP WITH TIME ZONE": "timestamp with time zone",
    "NUMERIC": "numeric",
    "BYTEA": "bytea",
    "JSONB": "jsonb",
    "REAL": "real",
    "FLOAT": "double precision",
}


def expected_type(column: Any) -> str:
    compiled: str = column.type.compile(dialect=postgresql.dialect())  # type: ignore[no-untyped-call]
    return TYPE_NAMES[compiled.split("(")[0].strip()]


async def test_columns_match_information_schema(admin_engine: AsyncEngine) -> None:
    async with admin_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT table_name, column_name, data_type, is_nullable"
                    " FROM information_schema.columns WHERE table_schema = 'public'"
                )
            )
        ).all()
    actual = {(r.table_name, r.column_name): (r.data_type, r.is_nullable == "YES") for r in rows}

    problems = []
    for table in Base.metadata.sorted_tables:
        db_columns = {c for (t, c) in actual if t == table.name}
        model_columns = {c.name for c in table.columns}
        if db_columns != model_columns:
            problems.append(f"{table.name}: db {db_columns ^ model_columns} differ")
            continue
        for column in table.columns:
            data_type, nullable = actual[(table.name, column.name)]
            if data_type != expected_type(column):
                problems.append(
                    f"{table.name}.{column.name}: {data_type} != {expected_type(column)}"
                )
            if nullable != column.nullable:
                problems.append(f"{table.name}.{column.name}: nullable {nullable}")
    assert problems == []


def _is_partition(name: str | None) -> bool:
    # Monthly partitions are created by create_month_partitions(), not declared in the models.
    return bool(name) and name.startswith(("access_events_", "audit_events_"))  # type: ignore[union-attr]


def _include(obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any) -> bool:
    if type_ == "table":
        return not _is_partition(name)
    table = getattr(obj, "table", None)
    return not _is_partition(getattr(table, "name", None))


def _diff(connection: Connection) -> list[Any]:
    context = MigrationContext.configure(
        connection, opts={"compare_type": True, "include_object": _include}
    )
    return list(compare_metadata(context, Base.metadata))


async def test_alembic_sees_no_pending_changes(admin_engine: AsyncEngine) -> None:
    async with admin_engine.connect() as conn:
        diff = await conn.run_sync(_diff)
    assert diff == []


@pytest.mark.parametrize("table", sorted(t.name for t in Base.metadata.sorted_tables))
async def test_every_table_has_row_level_security_or_is_identity(
    table: str, admin_engine: AsyncEngine
) -> None:
    identity_tables = {
        "users",
        "devices",
        "sessions",
        "mfa_secrets",
        "email_otps",
        "jobs",
        "policy_versions",
        "push_subscriptions",
    }
    async with admin_engine.connect() as conn:
        enabled = await conn.scalar(
            text("SELECT relrowsecurity FROM pg_class WHERE oid = to_regclass(:t)"), {"t": table}
        )
    assert enabled is (table not in identity_tables)
