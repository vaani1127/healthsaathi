"""One test suite for both e2d-core repository backends (SPEC 5.1).

The same workflow rows are written to Postgres (for SqlRepository) and to polars frames (for
MemoryRepository). Every test runs against both and checks the same expectations.
"""

import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl
import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db import TenantContext, tenant_session
from app.db import models as m
from e2d_core.explain import AccessEvent, EvidenceBundle, EvidenceQuery, explain
from e2d_core.repo import EvidenceRepository
from e2d_core.repo.memory import COLUMNS, MemoryRepository, frames_from_rows
from e2d_core.repo.sql import SqlRepository

BASIC = ("patients", "appointments", "queue_tokens", "shifts", "invoices", "status_events")
AT = datetime(2026, 10, 8, 6, 30, tzinfo=UTC)  # 12:00 in Asia/Kolkata
D = timedelta(days=1)
EARLIER = AT - timedelta(days=3)


class Scenario:
    def __init__(self) -> None:
        self.clinic = uuid.uuid4()
        self.other_clinic = uuid.uuid4()
        self.doctor, self.nurse, self.reception, self.patient_user = (
            uuid.uuid4() for _ in range(4)
        )
        self.patient, self.other_patient = uuid.uuid4(), uuid.uuid4()
        self.rows: dict[str, list[dict[str, Any]]] = {t: [] for t in COLUMNS if t in BASIC}
        self.extra_users = [self.doctor, self.nurse, self.reception, self.patient_user]

        def patient(pid: uuid.UUID, clinic: uuid.UUID, user: uuid.UUID | None) -> None:
            self.rows["patients"].append(
                {
                    "id": pid,
                    "clinic_id": clinic,
                    "user_id": user,
                    "created_by": self.reception,
                    "created_at": EARLIER,
                }
            )

        patient(self.patient, self.clinic, self.patient_user)
        patient(self.other_patient, self.clinic, None)

        def appointment(
            start: datetime, status: str = "booked", patient_id: uuid.UUID | None = None
        ) -> uuid.UUID:
            aid = uuid.uuid4()
            self.rows["appointments"].append(
                {
                    "id": aid,
                    "clinic_id": self.clinic,
                    "patient_id": patient_id or self.patient,
                    "doctor_user_id": self.doctor,
                    "slot_start": start,
                    "slot_end": start + timedelta(minutes=15),
                    "kind": "new",
                    "status": status,
                    "source": "reception",
                    "created_by": self.reception,
                    "created_at": EARLIER,
                    "updated_at": EARLIER,
                }
            )
            return aid

        self.appt_today = appointment(AT)
        self.appt_old = appointment(AT - timedelta(days=200))  # outside the lookback
        self.appt_other_patient = appointment(AT, patient_id=self.other_patient)
        self.appt_cancelled = appointment(AT + timedelta(hours=2), status="cancelled")
        self.appt_today_status = appointment(AT, status="completed")

        def status(entity: uuid.UUID, value: str, at: datetime) -> None:
            self.rows["status_events"].append(
                {
                    "id": uuid.uuid4(),
                    "clinic_id": self.clinic,
                    "entity_type": "appointment",
                    "entity_id": entity,
                    "status": value,
                    "at": at,
                }
            )

        # Several transitions, one of them after the access.
        self.transitions = (
            ("booked", EARLIER),
            ("checked_in", AT - timedelta(minutes=20)),
            ("in_consult", AT - timedelta(minutes=5)),
            ("completed", AT + timedelta(minutes=10)),
        )
        for value, at in self.transitions:
            status(self.appt_today_status, value, at)
        status(self.appt_cancelled, "booked", EARLIER)
        status(self.appt_cancelled, "cancelled", AT + timedelta(hours=1))

        self.token_today = uuid.uuid4()
        self.token_old = uuid.uuid4()
        for tid, day, appt in (
            (self.token_today, date(2026, 10, 8), self.appt_today),
            (self.token_old, date(2026, 10, 1), self.appt_cancelled),
        ):
            self.rows["queue_tokens"].append(
                {
                    "id": tid,
                    "clinic_id": self.clinic,
                    "appointment_id": appt,
                    "token_no": 4,
                    "day": day,
                    "status": "waiting",
                    "assigned_nurse_user_id": None,
                    "created_by": self.reception,
                    "created_at": EARLIER,
                }
            )

        self.shift = uuid.uuid4()
        self.rows["shifts"].append(
            {
                "id": self.shift,
                "clinic_id": self.clinic,
                "user_id": self.nurse,
                "role": "nurse",
                "starts_at": AT - timedelta(hours=4),
                "ends_at": AT + timedelta(hours=4),
                "status": "scheduled",
            }
        )
        self.invoice = uuid.uuid4()
        self.rows["invoices"].append(
            {
                "id": self.invoice,
                "clinic_id": self.clinic,
                "patient_id": self.patient,
                "status": "issued",
                "created_by": self.reception,
                "created_at": AT - timedelta(hours=1),
                "updated_at": AT - timedelta(hours=1),
            }
        )

    def query(
        self, user: uuid.UUID, patient: uuid.UUID | None = None, clinic: uuid.UUID | None = None
    ) -> EvidenceQuery:
        return EvidenceQuery(clinic or self.clinic, user, patient or self.patient, AT)

    def event(self, role: str, user: uuid.UUID, resource: str) -> AccessEvent:
        return AccessEvent(
            uuid.uuid4(), self.clinic, user, role, self.patient, resource, "view", AT
        )


async def _write_sql(engine: AsyncEngine, sc: Scenario) -> None:
    async with AsyncSession(engine) as s, s.begin():
        await s.execute(
            insert(m.Clinic).values(id=sc.clinic, name="Repo test", city="X", state="Y")
        )
        for uid in sc.extra_users:
            await s.execute(insert(m.User).values(id=uid, email=f"{uid}@repo.test", name="Repo"))
        for row in sc.rows["patients"]:
            await s.execute(
                insert(m.Patient).values(
                    **row, mrn=str(row["id"])[:8], name="Repo patient", sex="female"
                )
            )
        for row in sc.rows["appointments"]:
            await s.execute(insert(m.Appointment).values(**row))
        for row in sc.rows["queue_tokens"]:
            await s.execute(insert(m.QueueToken).values(**row, updated_at=EARLIER))
        for row in sc.rows["shifts"]:
            await s.execute(insert(m.Shift).values(**row, created_by=sc.reception))
        for row in sc.rows["invoices"]:
            await s.execute(insert(m.Invoice).values(**row))
        for row in sc.rows["status_events"]:
            await s.execute(insert(m.StatusEvent).values(**row))


def _frames(sc: Scenario) -> dict[str, pl.DataFrame]:
    return frames_from_rows(sc.rows)


@pytest.fixture(scope="module")
async def scenario(admin_engine: AsyncEngine) -> Scenario:
    sc = Scenario()
    await _write_sql(admin_engine, sc)
    return sc


Runner = Callable[[EvidenceQuery], Any]


@pytest.fixture(params=["sql", "memory"])
async def fetch(
    request: pytest.FixtureRequest, scenario: Scenario
) -> AsyncIterator[Callable[[EvidenceQuery], Any]]:
    if request.param == "memory":
        repo: EvidenceRepository = MemoryRepository(_frames(scenario))

        async def run(q: EvidenceQuery) -> EvidenceBundle:
            return await repo.evidence_for(q)

        yield run
        return

    async def run_sql(q: EvidenceQuery) -> EvidenceBundle:
        async with tenant_session(TenantContext(clinic_id=q.clinic_id, role="system")) as db:
            return await SqlRepository(db).evidence_for(q)

    yield run_sql


async def test_patient_record(fetch: Runner, scenario: Scenario) -> None:
    bundle = await fetch(scenario.query(scenario.doctor))
    assert bundle.patient is not None
    assert bundle.patient.id == scenario.patient
    assert bundle.patient.user_id == scenario.patient_user


async def test_appointments_within_lookback_only(fetch: Runner, scenario: Scenario) -> None:
    bundle = await fetch(scenario.query(scenario.doctor))
    ids = [a.id for a in bundle.appointments]
    assert scenario.appt_today in ids
    assert scenario.appt_cancelled in ids
    assert scenario.appt_old not in ids
    assert scenario.appt_other_patient not in ids
    assert ids == sorted(
        ids, key=lambda i: next(a.slot_start for a in bundle.appointments if a.id == i)
    )


async def test_queue_tokens_for_patient_near_the_day(fetch: Runner, scenario: Scenario) -> None:
    bundle = await fetch(scenario.query(scenario.nurse))
    assert [t.id for t in bundle.queue_tokens] == [scenario.token_today]
    token = bundle.queue_tokens[0]
    assert token.patient_id == scenario.patient
    assert token.day == date(2026, 10, 8)


async def test_shifts_for_the_user(fetch: Runner, scenario: Scenario) -> None:
    assert [s.id for s in (await fetch(scenario.query(scenario.nurse))).shifts] == [scenario.shift]
    assert (await fetch(scenario.query(scenario.doctor))).shifts == ()


async def test_invoices(fetch: Runner, scenario: Scenario) -> None:
    bundle = await fetch(scenario.query(scenario.reception))
    assert [i.id for i in bundle.invoices] == [scenario.invoice]


StatusAt = Callable[[uuid.UUID, str, uuid.UUID, datetime], Any]


@pytest.fixture(params=["sql", "memory"])
async def status_at(request: pytest.FixtureRequest, scenario: Scenario) -> AsyncIterator[StatusAt]:
    if request.param == "memory":
        yield MemoryRepository(_frames(scenario)).status_at
        return

    async def run_sql(clinic: uuid.UUID, kind: str, entity: uuid.UUID, at: datetime) -> Any:
        async with tenant_session(TenantContext(clinic_id=clinic, role="system")) as db:
            return await SqlRepository(db).status_at(clinic, kind, entity, at)

    yield run_sql


async def test_status_at_follows_every_transition(status_at: StatusAt, scenario: Scenario) -> None:
    appt, clinic = scenario.appt_today_status, scenario.clinic
    second = timedelta(seconds=1)
    assert await status_at(clinic, "appointment", appt, EARLIER - second) is None
    for value, at in scenario.transitions:
        assert await status_at(clinic, "appointment", appt, at) == value
        assert await status_at(clinic, "appointment", appt, at + second) == value
    assert await status_at(clinic, "invoice", appt, AT) is None
    assert await status_at(scenario.other_clinic, "appointment", appt, AT) is None


async def test_bundle_holds_statuses_up_to_as_of(fetch: Runner, scenario: Scenario) -> None:
    at_access = await fetch(scenario.query(scenario.doctor))
    statuses = [s.status for s in at_access.statuses if s.entity_id == scenario.appt_today_status]
    assert statuses == ["booked", "checked_in", "in_consult"]
    appt = next(a for a in at_access.appointments if a.id == scenario.appt_cancelled)
    assert at_access.status_at("appointment", appt, AT) == "booked"
    later = EvidenceQuery(scenario.clinic, scenario.doctor, scenario.patient, AT, as_of=AT + D)
    after = await fetch(later)
    assert after.status_at("appointment", appt, AT + D) == "cancelled"
    assert after.status_at("appointment", appt, AT) == "booked"


async def test_other_clinic_sees_nothing(fetch: Runner, scenario: Scenario) -> None:
    bundle = await fetch(scenario.query(scenario.doctor, clinic=scenario.other_clinic))
    assert bundle == EvidenceBundle()


async def test_unknown_patient(fetch: Runner, scenario: Scenario) -> None:
    bundle = await fetch(scenario.query(scenario.doctor, patient=uuid.uuid4()))
    assert bundle.patient is None
    assert bundle.appointments == ()


@pytest.mark.parametrize(
    ("role", "who", "resource", "template"),
    [
        ("doctor", "doctor", "notes", "T_APPT"),
        ("nurse", "nurse", "vitals", "T_QUEUE"),
        ("reception", "reception", "billing", "T_FRONTDESK"),
        ("patient", "patient_user", "lab", "T_SELF"),
        ("reception", "reception", "notes", None),
    ],
)
async def test_explanations_match(
    fetch: Runner, scenario: Scenario, role: str, who: str, resource: str, template: str | None
) -> None:
    user = getattr(scenario, who)
    bundle = await fetch(scenario.query(user))
    result = explain(scenario.event(role, user, resource), bundle)
    assert result.template_code == template


async def test_backends_return_identical_bundles(scenario: Scenario) -> None:
    memory = MemoryRepository(_frames(scenario))
    for user in (scenario.doctor, scenario.nurse, scenario.reception, scenario.patient_user):
        q = scenario.query(user)
        async with tenant_session(TenantContext(clinic_id=q.clinic_id, role="system")) as db:
            sql_bundle = await SqlRepository(db).evidence_for(q)
        assert await memory.evidence_for(q) == sql_bundle


def test_memory_repository_rejects_missing_columns() -> None:
    with pytest.raises(ValueError, match="missing"):
        MemoryRepository({"patients": pl.DataFrame({"id": ["x"]})})
