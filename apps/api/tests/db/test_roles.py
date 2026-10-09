"""DB roles from SPEC 3.2: append-only audit log, partitions, anchor_job and researcher_ro."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.core.db import ANONYMOUS, tenant_session
from tests.db.test_rls import INSUFFICIENT_PRIVILEGE, sqlstate
from tests.factories import ClinicGraph, build_clinic_graph


@pytest.fixture(scope="module")
async def clinics(admin_engine: AsyncEngine) -> tuple[ClinicGraph, ClinicGraph]:
    return (
        await build_clinic_graph(admin_engine, "roles-a"),
        await build_clinic_graph(admin_engine, "roles-b"),
    )


@pytest.fixture
async def owner(admin_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    async with admin_engine.connect() as conn:
        yield conn
        await conn.rollback()


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE audit_events SET kind = 'changed'",
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
        "UPDATE access_events SET action = 'export'",
        "DELETE FROM access_explanations",
        "UPDATE status_events SET status = 'completed'",
        "DELETE FROM status_events",
    ],
)
async def test_app_rw_cannot_change_append_only_tables(
    sql: str, clinics: tuple[ClinicGraph, ClinicGraph]
) -> None:
    a, _ = clinics
    with pytest.raises(DBAPIError) as exc:
        async with tenant_session(a.ctx()) as s:
            await s.execute(text(sql))
    assert sqlstate(exc.value) == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE audit_events SET kind = 'changed'",
        "DELETE FROM audit_events",
        "UPDATE status_events SET status = 'completed'",
        "DELETE FROM status_events",
    ],
)
async def test_trigger_blocks_even_the_owner(
    sql: str, owner: AsyncConnection, clinics: tuple[ClinicGraph, ClinicGraph]
) -> None:
    with pytest.raises(DBAPIError, match="append-only"):
        await owner.execute(text(sql))


async def test_app_rw_can_append_audit_events(clinics: tuple[ClinicGraph, ClinicGraph]) -> None:
    a, _ = clinics
    async with tenant_session(a.ctx()) as s:
        seq = await s.scalar(
            text(
                "INSERT INTO audit_events (id, clinic_id, kind, payload, payload_hash, prev_hash,"
                " chain_hash) VALUES (gen_random_uuid(), :c, 'test', '{}', :h, :h, :h)"
                " RETURNING seq"
            ),
            {"c": a.clinic_id, "h": bytes(32)},
        )
    assert seq is not None


async def test_partitions_cannot_be_read_directly(
    owner: AsyncConnection, clinics: tuple[ClinicGraph, ClinicGraph]
) -> None:
    a, _ = clinics
    partition = await owner.scalar(
        text(
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid"
            " WHERE i.inhparent = 'audit_events'::regclass LIMIT 1"
        )
    )
    with pytest.raises(DBAPIError) as exc:
        async with tenant_session(a.ctx()) as s:
            await s.execute(text(f'SELECT * FROM "{partition}"'))
    assert sqlstate(exc.value) == INSUFFICIENT_PRIVILEGE


async def test_no_tenant_context_sees_no_clinic_rows(
    clinics: tuple[ClinicGraph, ClinicGraph],
) -> None:
    async with tenant_session(ANONYMOUS) as s:
        assert await s.scalar(text("SELECT count(*) FROM patients")) == 0
        assert await s.scalar(text("SELECT count(*) FROM audit_events")) == 0


async def test_memberships_visible_to_their_user_without_clinic(
    clinics: tuple[ClinicGraph, ClinicGraph],
) -> None:
    a, _ = clinics
    from app.core.db import TenantContext

    async with tenant_session(TenantContext(user_id=a.doctor_id)) as s:
        rows = (await s.execute(text("SELECT clinic_id, user_id FROM memberships"))).all()
    assert rows
    assert all(r.user_id == a.doctor_id for r in rows)


async def test_partition_function_is_idempotent(owner: AsyncConnection) -> None:
    created = await owner.scalar(text("SELECT create_month_partitions('audit_events', 3, 6)"))
    assert created == 0
    more = await owner.scalar(text("SELECT create_month_partitions('audit_events', 3, 8)"))
    assert more == 2
    with pytest.raises(DBAPIError, match="not a partitioned table"):
        await owner.execute(text("SELECT create_month_partitions('patients', 0, 1)"))


async def test_anchor_job_reads_audit_events_across_clinics(
    owner: AsyncConnection, clinics: tuple[ClinicGraph, ClinicGraph]
) -> None:
    a, b = clinics
    await owner.execute(text("SET LOCAL ROLE anchor_job"))
    seen = set((await owner.execute(text("SELECT DISTINCT clinic_id FROM audit_events"))).scalars())
    assert {a.clinic_id, b.clinic_id} <= seen


async def test_anchor_job_cannot_read_patients(owner: AsyncConnection) -> None:
    await owner.execute(text("SET LOCAL ROLE anchor_job"))
    with pytest.raises(DBAPIError) as exc:
        await owner.execute(text("SELECT * FROM patients"))
    assert sqlstate(exc.value) == INSUFFICIENT_PRIVILEGE


async def test_anchor_job_cannot_change_audit_events(owner: AsyncConnection) -> None:
    await owner.execute(text("SET LOCAL ROLE anchor_job"))
    with pytest.raises(DBAPIError) as exc:
        await owner.execute(text("UPDATE audit_events SET kind = 'x'"))
    assert sqlstate(exc.value) == INSUFFICIENT_PRIVILEGE


async def test_researcher_reads_only_the_deidentified_view(
    owner: AsyncConnection, clinics: tuple[ClinicGraph, ClinicGraph]
) -> None:
    await owner.execute(text("SET LOCAL ROLE researcher_ro"))
    rows = (
        await owner.execute(
            text("SELECT user_pseudo, patient_pseudo FROM research.access_events_deid")
        )
    ).all()
    assert rows
    assert all(len(r.user_pseudo) == 64 for r in rows)

    await owner.execute(text("SAVEPOINT s"))
    with pytest.raises(DBAPIError) as exc:
        await owner.execute(text("SELECT * FROM access_events"))
    assert sqlstate(exc.value) == INSUFFICIENT_PRIVILEGE


async def test_roles_do_not_bypass_rls(owner: AsyncConnection) -> None:
    rows = (
        await owner.execute(
            text(
                "SELECT rolname, rolbypassrls, rolsuper FROM pg_roles"
                " WHERE rolname IN ('app_rw', 'anchor_job', 'researcher_ro')"
            )
        )
    ).all()
    assert len(rows) == 3
    assert not any(r.rolbypassrls or r.rolsuper for r in rows)
