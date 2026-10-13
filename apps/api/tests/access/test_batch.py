"""The batched list-screen path records exactly what recording each patient in turn records.

1. load_evidence returns, for every patient, the bundle SqlRepository.evidence_for returns, on
   every template and forgery-flag case and on one clinic holding all of them at once.
2. record_many writes the same access events, explanations (template, strength, flags,
   evidence) and audit payloads and links as calling record_and_explain once per patient, with
   ids and the clock fixed so the two runs can be compared field by field.
"""

import itertools
import uuid
from collections.abc import Awaitable, Callable, Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.access import service as access
from app.access.evidence import load_evidence
from app.chart.service import PATIENT_RESOURCES
from app.core.db import TenantContext, tenant_session
from app.db import models as m
from app.db.enums import AccessAction, ResourceType, Role
from app.identity import service as identity
from app.identity.deps import ClinicPrincipal
from e2d_core.explain.model import AccessReason
from e2d_core.ledger import GENESIS, chain_hash
from tests.e2d.graph import Graph, fetch_sql
from tests.e2d.test_templates_and_flags import AT, CASES, Case

# The record path never sets as_of; cases that need it are detection-time cases.
RECORD_CASES = [c for c in CASES if c.as_of is None]


async def _batched(
    graph: Graph, user: str, role: str, at: datetime, patients: list[uuid.UUID]
) -> Any:
    ctx = TenantContext(clinic_id=graph.clinic, role="system")
    async with tenant_session(ctx) as db:
        return await load_evidence(
            db, graph.clinic, graph.users[user], role, at, "Asia/Kolkata", patients
        )


@pytest.mark.parametrize("case", RECORD_CASES, ids=[c.name for c in RECORD_CASES])
async def test_batched_evidence_equals_sql_repository(
    case: Case, admin_engine: AsyncEngine
) -> None:
    graph = Graph(AT)
    case.build(graph)
    await graph.write(admin_engine)
    at = AT + case.at
    query = graph.query(case.user, case.role, at=at)
    bundles = await _batched(graph, case.user, case.role, at, [graph.patient])
    assert bundles[graph.patient] == await fetch_sql(query)


def _many(builds: list[Callable[[Graph], None]]) -> tuple[Graph, list[uuid.UUID]]:
    """One clinic with one patient per build, so their rows sit side by side."""
    graph = Graph(AT)
    patients = [graph.patient]
    for build in builds:
        graph.patient = graph.add_patient()
        build(graph)
        patients.append(graph.patient)
    return graph, patients


@pytest.mark.parametrize(
    ("user", "role"), [("doctor", "doctor"), ("nurse", "nurse"), ("doctor2", "doctor")]
)
async def test_batched_evidence_for_many_patients(
    user: str, role: str, admin_engine: AsyncEngine
) -> None:
    graph, patients = _many([c.build for c in RECORD_CASES if c.at == timedelta(0)])
    await graph.write(admin_engine)
    bundles = await _batched(graph, user, role, AT, patients)
    for patient in patients:
        graph.patient = patient
        assert bundles[patient] == await fetch_sql(graph.query(user, role, at=AT)), patient


class _RollbackError(Exception):
    pass


@pytest.fixture
def fixed(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[], None]]:
    """Fixed access ids and clock for both paths; call the returned function between runs."""
    counter: Iterator[int] = itertools.count(1)

    def next_id() -> uuid.UUID:
        return uuid.UUID(int=next(counter))

    original = access.clock_and_timezone

    async def clock(db: AsyncSession, clinic_id: uuid.UUID) -> tuple[datetime, str]:
        _, tz = await original(db, clinic_id)
        return AT, tz

    def reset() -> None:
        nonlocal counter
        counter = itertools.count(1)

    monkeypatch.setattr(access, "uuid7", next_id)
    monkeypatch.setattr(access, "clock_and_timezone", clock)
    yield reset


async def _principal(graph: Graph, user: str, role: Role) -> ClinicPrincipal:
    kind: identity.SessionKind = "email" if role == Role.PATIENT else "mfa"
    issued = await identity.create_session(
        graph.users[user], kind, identity.DeviceInfo("test-device", "pytest")
    )
    return ClinicPrincipal(
        user_id=graph.users[user],
        session_id=issued.session_id,
        token_id="test",
        amr=("email_otp",) if role == Role.PATIENT else ("pwd", "otp"),
        clinic_id=graph.clinic,
        role=role,
    )


async def _capture(
    principal: ClinicPrincipal, record: Callable[[AsyncSession], Awaitable[None]]
) -> dict[str, list[Any]]:
    """Run `record`, read back what it wrote, then roll everything back."""
    seen: dict[str, list[Any]] = {}
    with pytest.raises(_RollbackError):
        async with tenant_session(principal.tenant()) as db:
            await record(db)
            await db.flush()
            clinic = principal.clinic_id
            events = (
                await db.scalars(
                    select(m.AccessEvent)
                    .where(m.AccessEvent.clinic_id == clinic)
                    .order_by(m.AccessEvent.id)
                )
            ).all()
            seen["events"] = [
                {c.name: getattr(e, c.name) for c in m.AccessEvent.__table__.columns}
                for e in events
            ]
            explanations = (
                await db.scalars(
                    select(m.AccessExplanation)
                    .where(m.AccessExplanation.clinic_id == clinic)
                    .order_by(m.AccessExplanation.access_event_id)
                )
            ).all()
            seen["explanations"] = [
                (x.access_event_id, x.template_code, x.strength, x.forgery_flags, x.evidence)
                for x in explanations
            ]
            audits = (
                await db.scalars(
                    select(m.AuditEvent)
                    .where(m.AuditEvent.clinic_id == clinic)
                    .order_by(m.AuditEvent.seq)
                )
            ).all()
            seen["audit"] = [
                (a.seq, a.payload, a.payload_hash, a.prev_hash, a.chain_hash) for a in audits
            ]
            raise _RollbackError
    return seen


async def _run(
    principal: ClinicPrincipal,
    patients: list[uuid.UUID],
    resource: ResourceType,
    batched: bool,
) -> dict[str, list[Any]]:
    async def batch(db: AsyncSession) -> None:
        await access.record_many(db, principal, patients, resource, AccessAction.VIEW)

    async def one_by_one(db: AsyncSession) -> None:
        for patient in patients:
            await access.record_and_explain(db, principal, patient, resource, AccessAction.VIEW)

    return await _capture(principal, batch if batched else one_by_one)


def _appt_now_and_vitals(graph: Graph) -> None:
    graph.add_appointment(AT)
    graph.add_vitals(AT - timedelta(hours=1))


@pytest.mark.parametrize(
    ("user", "role", "resource"),
    [
        ("doctor", Role.DOCTOR, ResourceType.DEMOGRAPHICS),
        ("nurse", Role.NURSE, ResourceType.DEMOGRAPHICS),
        ("reception", Role.RECEPTION, ResourceType.DEMOGRAPHICS),
        ("lab", Role.LAB_TECH, ResourceType.LAB),
    ],
)
async def test_record_many_equals_recording_one_by_one(
    user: str,
    role: Role,
    resource: ResourceType,
    admin_engine: AsyncEngine,
    fixed: Callable[[], None],
) -> None:
    graph, patients = _many([c.build for c in RECORD_CASES if c.at == timedelta(0)])
    await graph.write(admin_engine)
    principal = await _principal(graph, user, role)

    one_by_one = await _run(principal, patients, resource, batched=False)
    fixed()
    batched = await _run(principal, patients, resource, batched=True)

    assert len(batched["events"]) == len(patients)
    assert batched["events"] == one_by_one["events"]
    assert batched["explanations"] == one_by_one["explanations"]
    # Several template codes and forgery flags are exercised, not one repeated.
    assert len({x[1] for x in batched["explanations"]}) > 1

    # Same payloads in the same order. Sequence numbers come from a database sequence that a
    # rollback does not rewind, so the links are compared by rebuilding the one-by-one chain on
    # the batched run's numbers: every link must come out the same.
    assert [a[1] for a in batched["audit"]] == [a[1] for a in one_by_one["audit"]]
    assert [a[2] for a in batched["audit"]] == [a[2] for a in one_by_one["audit"]]
    prev = GENESIS
    for (seq, _, _, prev_hash, link), (_, _, digest, _, _) in zip(
        batched["audit"], one_by_one["audit"], strict=True
    ):
        assert prev_hash == prev
        assert link == chain_hash(prev, digest, seq, graph.clinic)
        prev = link


async def test_record_many_refuses_the_whole_list_like_one_by_one(
    admin_engine: AsyncEngine,
) -> None:
    """A patient account listing someone else's record is refused, and the denial is logged."""
    graph = Graph(AT)
    other = graph.add_patient()
    await graph.write(admin_engine)
    principal = await _principal(graph, "patient_user", Role.PATIENT)
    with pytest.raises(access.AccessDeniedError):
        async with access.access_session(principal) as db:
            await access.record_many(
                db, principal, [graph.patient, other], ResourceType.DEMOGRAPHICS, AccessAction.VIEW
            )
    async with AsyncSession(admin_engine) as s:
        events = (
            await s.scalars(select(m.AccessEvent).where(m.AccessEvent.clinic_id == graph.clinic))
        ).all()
    assert [(e.patient_id, e.decision.value) for e in events] == [(other, "deny")]


async def _run_resources(
    principal: ClinicPrincipal,
    patient: uuid.UUID,
    resources: list[ResourceType],
    reason: AccessReason | None,
    batched: bool,
) -> dict[str, list[Any]]:
    async def batch(db: AsyncSession) -> None:
        await access.record_resources(
            db, principal, patient, resources, AccessAction.VIEW, reason=reason
        )

    async def one_by_one(db: AsyncSession) -> None:
        for resource in resources:
            await access.record_and_explain(
                db, principal, patient, resource, AccessAction.VIEW, reason=reason
            )

    return await _capture(principal, batch if batched else one_by_one)


@pytest.mark.parametrize(
    ("user", "role", "resources", "reason"),
    [
        ("patient_user", Role.PATIENT, list(PATIENT_RESOURCES), None),
        (
            "doctor",
            Role.DOCTOR,
            [ResourceType.VITALS, ResourceType.NOTES, ResourceType.LAB],
            AccessReason("second_opinion", "Asked to look at this case"),
        ),
        ("nurse", Role.NURSE, [ResourceType.DEMOGRAPHICS, ResourceType.VITALS], None),
    ],
)
async def test_record_resources_equals_recording_one_by_one(
    user: str,
    role: Role,
    resources: list[ResourceType],
    reason: AccessReason | None,
    admin_engine: AsyncEngine,
    fixed: Callable[[], None],
) -> None:
    graph = Graph(AT)
    _appt_now_and_vitals(graph)
    await graph.write(admin_engine)
    principal = await _principal(graph, user, role)

    one_by_one = await _run_resources(principal, graph.patient, resources, reason, batched=False)
    fixed()
    batched = await _run_resources(principal, graph.patient, resources, reason, batched=True)

    assert len(batched["events"]) == len(resources)
    assert batched["events"] == one_by_one["events"]
    assert batched["explanations"] == one_by_one["explanations"]
    assert [a[1:3] for a in batched["audit"]] == [a[1:3] for a in one_by_one["audit"]]
