"""Tenant isolation: for every table with clinic_id, clinic A cannot read or write clinic B rows."""

from typing import Any

import pytest
from sqlalchemy import Table, delete, func, insert, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db import tenant_session
from app.db import models  # noqa: F401
from app.db.base import Base
from e2d_core.ids import uuid7
from tests.factories import ClinicGraph, build_clinic_graph

CLINIC_TABLES = sorted(t.name for t in Base.metadata.sorted_tables if "clinic_id" in t.c)
NO_UPDATE_FOR_APP = {"audit_events", "access_events", "access_explanations", "merkle_checkpoints"}
NO_UPDATE_FOR_APP |= {"anchor_receipts", "status_events"}
INSUFFICIENT_PRIVILEGE = "42501"


def sqlstate(exc: DBAPIError) -> str | None:
    orig = exc.orig
    return getattr(orig, "sqlstate", None) or getattr(
        getattr(orig, "__cause__", None), "sqlstate", None
    )


@pytest.fixture(scope="module")
async def clinics(admin_engine: AsyncEngine) -> tuple[ClinicGraph, ClinicGraph]:
    return (
        await build_clinic_graph(admin_engine, "rls-a"),
        await build_clinic_graph(admin_engine, "rls-b"),
    )


def table(name: str) -> Table:
    return Base.metadata.tables[name]


async def owner_rows(engine: AsyncEngine, name: str, clinic: ClinicGraph) -> list[dict[str, Any]]:
    t = table(name)
    async with AsyncSession(engine) as s:
        result = await s.execute(select(t).where(t.c.clinic_id == clinic.clinic_id))
        return [dict(r) for r in result.mappings()]


def test_every_clinic_table_is_covered() -> None:
    # Guards against a new table being added without this suite noticing it.
    assert len(CLINIC_TABLES) == 35


@pytest.mark.parametrize("name", CLINIC_TABLES)
async def test_cannot_read_other_clinic(
    name: str, clinics: tuple[ClinicGraph, ClinicGraph], admin_engine: AsyncEngine
) -> None:
    a, b = clinics
    t = table(name)
    own_expected = len(await owner_rows(admin_engine, name, a))
    assert own_expected > 0, f"factory made no {name} row"

    async with tenant_session(a.ctx()) as s:
        other = await s.scalar(
            select(func.count()).select_from(t).where(t.c.clinic_id == b.clinic_id)
        )
        visible = await s.scalar(select(func.count()).select_from(t))

    assert other == 0
    assert visible == own_expected


@pytest.mark.parametrize("name", CLINIC_TABLES)
async def test_cannot_insert_into_other_clinic(
    name: str, clinics: tuple[ClinicGraph, ClinicGraph], admin_engine: AsyncEngine
) -> None:
    a, b = clinics
    t = table(name)
    row = (await owner_rows(admin_engine, name, b))[0]
    row.pop("seq", None)
    for key in ("id", "access_event_id"):
        if key in row and t.c[key].primary_key:
            row[key] = uuid7()

    with pytest.raises(DBAPIError) as exc:
        async with tenant_session(a.ctx()) as s:
            await s.execute(insert(t).values(**row))
    assert sqlstate(exc.value) == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize("name", CLINIC_TABLES)
async def test_cannot_update_other_clinic(
    name: str, clinics: tuple[ClinicGraph, ClinicGraph]
) -> None:
    a, b = clinics
    t = table(name)
    stmt = update(t).where(t.c.clinic_id == b.clinic_id).values(clinic_id=b.clinic_id)

    if name in NO_UPDATE_FOR_APP:
        with pytest.raises(DBAPIError) as exc:
            async with tenant_session(a.ctx()) as s:
                await s.execute(stmt)
        assert sqlstate(exc.value) == INSUFFICIENT_PRIVILEGE
        return

    async with tenant_session(a.ctx()) as s:
        result = await s.execute(stmt)
    assert result.rowcount == 0  # type: ignore[attr-defined]


@pytest.mark.parametrize("name", sorted(set(CLINIC_TABLES) - NO_UPDATE_FOR_APP))
async def test_cannot_move_own_row_to_other_clinic(
    name: str, clinics: tuple[ClinicGraph, ClinicGraph]
) -> None:
    a, b = clinics
    t = table(name)
    with pytest.raises(DBAPIError) as exc:
        async with tenant_session(a.ctx()) as s:
            await s.execute(
                update(t).where(t.c.clinic_id == a.clinic_id).values(clinic_id=b.clinic_id)
            )
    assert sqlstate(exc.value) == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize("name", CLINIC_TABLES)
async def test_app_cannot_delete_clinic_rows(
    name: str, clinics: tuple[ClinicGraph, ClinicGraph]
) -> None:
    a, b = clinics
    t = table(name)
    with pytest.raises(DBAPIError) as exc:
        async with tenant_session(a.ctx()) as s:
            await s.execute(delete(t).where(t.c.clinic_id == b.clinic_id))
    assert sqlstate(exc.value) == INSUFFICIENT_PRIVILEGE
