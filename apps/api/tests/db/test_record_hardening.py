"""Migrations 0008 and 0009 (ADR 0011): record_versions is append-only; vitals and documents are
append-only; lab_results only lets released_at be set once; one newer version per note or
prescription; and the 0009 guard stops on duplicate parent_id values."""

import importlib.util
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from app.core.db import tenant_session
from app.db import models as m
from tests.db.test_rls import INSUFFICIENT_PRIVILEGE, sqlstate
from tests.factories import ClinicGraph, build_clinic_graph

MIGRATION_0009 = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "20261127_0009_clinical_hardening.py"
)


def migration_0009() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0009", MIGRATION_0009)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
async def clinics(admin_engine: AsyncEngine) -> tuple[ClinicGraph, ClinicGraph]:
    return (
        await build_clinic_graph(admin_engine, "harden-a"),
        await build_clinic_graph(admin_engine, "harden-b"),
    )


@pytest.fixture
async def owner(admin_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    async with admin_engine.connect() as conn:
        yield conn
        await conn.rollback()


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE record_versions SET version = 2",
        "DELETE FROM record_versions",
        "TRUNCATE record_versions",
        "UPDATE vitals SET pulse = 1",
        "DELETE FROM vitals",
        "UPDATE documents SET sha256 = 'x'",
        "DELETE FROM documents",
        "UPDATE lab_results SET values = '{}'::jsonb",
        "DELETE FROM lab_results",
    ],
)
async def test_app_rw_cannot_change_committed_clinical_rows(
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
        "UPDATE record_versions SET version = 2",
        "DELETE FROM record_versions",
        "UPDATE vitals SET pulse = 1",
        "DELETE FROM vitals",
        "TRUNCATE vitals CASCADE",
        "UPDATE documents SET sha256 = 'x'",
        "DELETE FROM documents",
        "UPDATE lab_results SET values = '{\"x\": 1}'::jsonb",
        "DELETE FROM lab_results",
        "TRUNCATE lab_results",
    ],
)
async def test_triggers_block_even_the_owner(
    sql: str, owner: AsyncConnection, clinics: tuple[ClinicGraph, ClinicGraph]
) -> None:
    with pytest.raises(DBAPIError, match="append-only"):
        await owner.execute(text(sql))


async def test_app_rw_can_add_record_versions(
    clinics: tuple[ClinicGraph, ClinicGraph], admin_engine: AsyncEngine
) -> None:
    a, _ = clinics
    async with tenant_session(a.ctx()) as s:
        s.add(
            m.RecordVersion(
                clinic_id=a.clinic_id,
                patient_id=a.patient_id,
                record_type="vital",
                record_id=uuid.uuid4(),
                version=1,
                content_schema="vital/v1",
                salt=bytes(32),
                commitment=bytes(32),
                audit_seq=1,
                created_by=a.doctor_id,
            )
        )


async def _result_id(engine: AsyncEngine, clinic: ClinicGraph) -> uuid.UUID:
    async with AsyncSession(engine) as s:
        found = await s.scalar(
            select(m.LabResult.id).where(
                m.LabResult.clinic_id == clinic.clinic_id, m.LabResult.released_at.is_(None)
            )
        )
    assert found is not None
    return found


async def test_lab_result_release_can_be_set_once_and_nothing_else(
    admin_engine: AsyncEngine,
) -> None:
    clinic = await build_clinic_graph(admin_engine, f"release-{uuid.uuid4().hex[:6]}")
    result_id = await _result_id(admin_engine, clinic)
    release = text("UPDATE lab_results SET released_at = clock_timestamp() WHERE id = :id")

    with pytest.raises(DBAPIError) as exc:
        async with tenant_session(clinic.ctx()) as s:
            await s.execute(
                text("UPDATE lab_results SET released_at = now(), values = '{}' WHERE id = :id"),
                {"id": result_id},
            )
    assert sqlstate(exc.value) == INSUFFICIENT_PRIVILEGE

    async with tenant_session(clinic.ctx()) as s:
        assert (await s.execute(release, {"id": result_id})).rowcount == 1  # type: ignore[attr-defined]

    with pytest.raises(DBAPIError, match="append-only"):
        async with tenant_session(clinic.ctx()) as s:
            await s.execute(release, {"id": result_id})
    with pytest.raises(DBAPIError, match="append-only"):
        async with admin_engine.begin() as conn:
            await conn.execute(
                text("UPDATE lab_results SET released_at = NULL WHERE id = :id"), {"id": result_id}
            )


async def _signed_note(engine: AsyncEngine, clinic: ClinicGraph) -> m.ClinicalNote:
    async with AsyncSession(engine, expire_on_commit=False) as s, s.begin():
        note = await s.scalar(
            select(m.ClinicalNote).where(m.ClinicalNote.clinic_id == clinic.clinic_id)
        )
        assert note is not None
        return note


def _child(note: m.ClinicalNote) -> m.ClinicalNote:
    return m.ClinicalNote(
        clinic_id=note.clinic_id,
        patient_id=note.patient_id,
        encounter_id=note.encounter_id,
        author_user_id=note.author_user_id,
        version=2,
        parent_id=note.id,
        body_enc=b"encrypted",
    )


async def test_a_note_can_have_only_one_newer_version(admin_engine: AsyncEngine) -> None:
    clinic = await build_clinic_graph(admin_engine, f"parent-{uuid.uuid4().hex[:6]}")
    note = await _signed_note(admin_engine, clinic)
    async with AsyncSession(admin_engine) as s, s.begin():
        s.add(_child(note))
    with pytest.raises(IntegrityError, match="uq_clinical_notes_parent_id"):
        async with AsyncSession(admin_engine) as s, s.begin():
            s.add(_child(note))


def _guard(module: ModuleType) -> Callable[[Connection], None]:
    def run(conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(conn)):
            module.check_no_duplicate_parents()

    return run


async def test_guard_passes_when_there_are_no_duplicates(owner: AsyncConnection) -> None:
    await owner.run_sync(_guard(migration_0009()))


async def test_guard_stops_on_duplicate_parent_ids(
    admin_engine: AsyncEngine, owner: AsyncConnection
) -> None:
    clinic = await build_clinic_graph(admin_engine, f"guard-{uuid.uuid4().hex[:6]}")
    note = await _signed_note(admin_engine, clinic)
    # Inside the fixture's transaction, rolled back afterwards: the state before migration 0009.
    await owner.execute(text("DROP INDEX uq_clinical_notes_parent_id"))
    for _ in range(2):
        await owner.execute(
            text(
                "INSERT INTO clinical_notes (id, clinic_id, patient_id, encounter_id, "
                "author_user_id, version, parent_id, body_enc) VALUES (gen_random_uuid(), "
                ":clinic, :patient, :encounter, :author, 2, :parent, 'x')"
            ),
            {
                "clinic": note.clinic_id,
                "patient": note.patient_id,
                "encounter": note.encounter_id,
                "author": note.author_user_id,
                "parent": note.id,
            },
        )
    module = migration_0009()
    rows = (await owner.execute(text(module.DUPLICATE_PARENTS_SQL))).all()
    assert [(r.table_name, r.parent_id, r.children) for r in rows] == [
        ("clinical_notes", note.id, 2)
    ]
    with pytest.raises(module.DuplicateParentError, match="Migration 0009 stopped"):
        await owner.run_sync(_guard(module))
