"""Every explanation template (SPEC 5.2) and forgery flag (SPEC 5.3), on both backends.

Each case builds a small graph, writes it to Postgres and to polars frames, fetches the evidence
through SqlRepository and MemoryRepository, and checks the same expected explanation.
"""

import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from e2d_core.explain import AccessEvent, explain
from e2d_core.explain.forgery import any_flag
from e2d_core.explain.model import AccessReason
from tests.e2d.graph import Graph, fetch_memory, fetch_sql

# Thursday 8 Oct 2026, 12:00 in Asia/Kolkata.
AT = datetime(2026, 10, 8, 6, 30, tzinfo=UTC)
TODAY = date(2026, 10, 8)
M = timedelta(minutes=1)
H = timedelta(hours=1)
D = timedelta(days=1)


@dataclass
class Case:
    name: str
    build: Callable[[Graph], None]
    user: str
    role: str
    resource: str
    template: str | None
    strength: float | None = None
    flags: dict[str, Any] = field(default_factory=dict)
    at: timedelta = timedelta(0)
    as_of: timedelta | None = None
    reason: AccessReason | None = None


def _nothing(g: Graph) -> None:
    return None


def _appt_now(g: Graph) -> None:
    g.add_appointment(AT)


def _token(g: Graph) -> None:
    g.add_token(g.add_appointment(AT + H), TODAY, nurse="nurse")


def _unassigned_token_and_shift(g: Graph) -> None:
    g.add_token(g.add_appointment(AT + H), TODAY, nurse=None)
    g.add_shift("nurse", AT - 4 * H, AT + 4 * H)


def _lab_open(g: Graph) -> None:
    g.add_lab_order(AT - 3 * H)


def _lab_released_long_ago(g: Graph) -> None:
    # Released 2 days + 6 hours ago: one tau past the end of the window.
    g.add_lab_order(AT - 10 * D, status="resulted", released_at=AT - 2 * D - 6 * H)


def _referral(g: Graph) -> None:
    g.add_referral(AT - D, AT + 7 * D)


def _care_team_nurse(g: Graph) -> None:
    g.add_care_team("nurse", "nurse", AT - 2 * D)


def _followup(g: Graph) -> None:
    g.add_appointment(AT - 30 * D, status="completed", created_at=AT - 32 * D)
    g.add_appointment(AT + 10 * D, created_at=AT - 30 * D)


def _followup_by_encounter(g: Graph) -> None:
    g.add_encounter(AT - 20 * D)
    g.add_appointment(AT + 5 * D, created_at=AT - 20 * D)


def _paid_invoice(g: Graph) -> None:
    g.add_invoice(AT - 5 * D, status="paid", updated_at=AT - 3 * D)


def _break_glass(g: Graph) -> None:
    g.add_break_glass("nurse", AT - H)


def _self_booked_off_path(g: Graph) -> None:
    g.add_appointment(
        AT, source="doctor", created_by="doctor", created_at=AT - 10 * timedelta(minutes=1)
    )


def _doctor_followup_after_own_encounter(g: Graph) -> None:
    g.add_encounter(AT - 10 * D)
    g.add_appointment(AT, source="doctor", created_by="doctor", created_at=AT - 10 * D + 5 * M)


def _doctor_booking_after_another_doctors_encounter(g: Graph) -> None:
    g.add_encounter(AT - 10 * D, doctor="doctor2")
    g.add_appointment(AT, source="doctor", created_by="doctor", created_at=AT - 10 * D + 5 * M)


def _cancelled_before(g: Graph) -> None:
    g.add_appointment(AT, status="cancelled", created_at=AT - D, updated_at=AT - 2 * H)


def _cancelled_much_later(g: Graph) -> None:
    g.add_appointment(AT, status="cancelled", created_at=AT - D, updated_at=AT + 30 * H)


def _cancelled_without_history(g: Graph) -> None:
    g.add_appointment(
        AT, status="cancelled", created_at=AT - D, updated_at=AT + 2 * H, history=False
    )


def _lab_cancelled_after_access(g: Graph) -> None:
    g.add_lab_order(AT - 3 * H, status="cancelled", updated_at=AT + H)


def _stale_appointment_checked_in(g: Graph) -> None:
    g.add_appointment(
        AT - 3 * D, status="checked_in", created_at=AT - 3 * D - H, updated_at=AT - 3 * D + H
    )


def _completed_visit_then_followup_cancelled_later(g: Graph) -> None:
    # The follow-up was booked when the access happened and cancelled a day later.
    past = g.add_appointment(
        AT - 30 * D, status="completed", created_at=AT - 32 * D, updated_at=AT - 30 * D + 15 * M
    )
    g.add_status("appointment", past, "checked_in", AT - 30 * D - 10 * M)
    future = g.add_appointment(AT + 10 * D, created_at=AT - 30 * D)
    g.add_status("appointment", future, "cancelled", AT + D)


def _hours_and_late_booking(g: Graph) -> None:
    for weekday in range(6):
        g.add_schedule(weekday, time(9), time(13))
    # Booked at 23:00 local the night before.
    g.add_appointment(AT, created_at=datetime(2026, 10, 7, 17, 30, tzinfo=UTC))


def _hours_and_daytime_booking(g: Graph) -> None:
    for weekday in range(6):
        g.add_schedule(weekday, time(9), time(13))
    g.add_appointment(AT, created_at=AT - 2 * D)  # Tuesday 12:00 local


def _stale_appointment(g: Graph) -> None:
    g.add_appointment(AT - 3 * D, created_at=AT - 3 * D - H)


def _stale_appointment_with_vitals(g: Graph) -> None:
    g.add_appointment(AT - 3 * D, created_at=AT - 3 * D - H)
    g.add_vitals(AT - 3 * D + H)


def _stale_appointment_with_note(g: Graph) -> None:
    g.add_appointment(AT - 3 * D, created_at=AT - 3 * D - H)
    g.add_note(AT - 3 * D + 2 * H)


def _cancelled_later(g: Graph) -> None:
    g.add_appointment(AT, status="cancelled", created_at=AT - D, updated_at=AT + 2 * H)


def _cancelled_appointment_still_seen_by_frontdesk(g: Graph) -> None:
    g.add_appointment(AT, status="cancelled", created_at=AT - D, updated_at=AT + 2 * H)


def _many_self_created(g: Graph) -> None:
    for i in range(6):
        g.add_appointment(
            AT + (i + 1) * D,
            patient=g.add_patient(),
            source="doctor",
            created_by="doctor",
            created_at=AT - (i + 1) * H,
        )


CASES = [
    # Templates ------------------------------------------------------------------------------------
    Case("T_APPT doctor at the slot", _appt_now, "doctor", "doctor", "notes", "T_APPT", 1.0),
    Case(
        "T_APPT one tau after the window",
        _appt_now,
        "doctor",
        "doctor",
        "notes",
        "T_APPT",
        math.exp(-1),
        at=timedelta(minutes=15) + 24 * H + 2 * H,
    ),
    Case("T_QUEUE assigned nurse", _token, "nurse", "nurse", "vitals", "T_QUEUE", 0.9),
    Case(
        "T_QUEUE nurse on shift",
        _unassigned_token_and_shift,
        "nurse",
        "nurse",
        "allergies",
        "T_QUEUE",
        0.9,
    ),
    Case(
        "T_FRONTDESK booking",
        _appt_now,
        "reception",
        "reception",
        "demographics",
        "T_FRONTDESK",
        0.9,
    ),
    Case("T_LAB open order", _lab_open, "lab", "lab_tech", "lab", "T_LAB", 1.0),
    Case(
        "T_LAB decays after release",
        _lab_released_long_ago,
        "lab",
        "lab_tech",
        "lab",
        "T_LAB",
        math.exp(-1),
    ),
    Case("T_REFERRAL to the doctor", _referral, "doctor2", "doctor", "notes", "T_REFERRAL", 0.8),
    Case("T_REFERRAL not for others", _referral, "doctor3", "doctor", "notes", None, 0.0),
    Case(
        "T_CARETEAM nurse default view",
        _care_team_nurse,
        "nurse",
        "nurse",
        "vitals",
        "T_CARETEAM",
        0.8,
    ),
    Case("T_CARETEAM not beyond role view", _care_team_nurse, "nurse", "nurse", "notes", None, 0.0),
    Case("T_FOLLOWUP after a visit", _followup, "doctor", "doctor", "notes", "T_FOLLOWUP", 0.6),
    Case(
        "T_FOLLOWUP via encounter",
        _followup_by_encounter,
        "doctor",
        "doctor",
        "lab",
        "T_FOLLOWUP",
        0.6,
    ),
    Case(
        "T_BILLING paid invoice",
        _paid_invoice,
        "reception",
        "reception",
        "billing",
        "T_BILLING",
        0.8,
    ),
    Case(
        "T_REASON typed reason",
        _nothing,
        "doctor",
        "doctor",
        "notes",
        "T_REASON",
        0.3,
        reason=AccessReason("covering_doctor", "covering for the regular doctor today"),
    ),
    Case(
        "T_BREAKGLASS nurse meds",
        _break_glass,
        "nurse",
        "nurse",
        "prescriptions",
        "T_BREAKGLASS",
        0.2,
    ),
    Case("T_BREAKGLASS not for billing", _break_glass, "nurse", "nurse", "billing", None, 0.0),
    Case("T_SELF own record", _nothing, "patient_user", "patient", "lab", "T_SELF", 1.0),
    Case("reception cannot explain notes", _appt_now, "reception", "reception", "notes", None, 0.0),
    # Forgery flags --------------------------------------------------------------------------------
    Case(
        "self_created_recent and off_path_creation",
        _self_booked_off_path,
        "doctor",
        "doctor",
        "notes",
        "T_APPT",
        1.0,
        flags={"self_created_recent": True, "off_path_creation": True},
    ),
    Case(
        "reception booking is on path",
        _appt_now,
        "doctor",
        "doctor",
        "notes",
        "T_APPT",
        flags={"self_created_recent": False, "off_path_creation": False},
    ),
    Case(
        "doctor follow-up after a completed encounter is on path",
        _doctor_followup_after_own_encounter,
        "doctor",
        "doctor",
        "notes",
        "T_APPT",
        flags={"off_path_creation": False},
    ),
    Case(
        "doctor booking after another doctor's encounter is off path",
        _doctor_booking_after_another_doctors_encounter,
        "doctor",
        "doctor",
        "notes",
        "T_APPT",
        flags={"off_path_creation": True},
    ),
    Case(
        "created_off_hours true",
        _hours_and_late_booking,
        "doctor",
        "doctor",
        "notes",
        "T_APPT",
        flags={"created_off_hours": True},
    ),
    Case(
        "created_off_hours false",
        _hours_and_daytime_booking,
        "doctor",
        "doctor",
        "notes",
        "T_APPT",
        flags={"created_off_hours": False},
    ),
    Case(
        "no_progress unknown at access time",
        _stale_appointment,
        "doctor",
        "doctor",
        "notes",
        "T_APPT",
        flags={"no_progress": None},
        at=-3 * D + 30 * timedelta(minutes=1),
    ),
    Case(
        "no_progress true later",
        _stale_appointment,
        "doctor",
        "doctor",
        "notes",
        "T_APPT",
        flags={"no_progress": True},
        at=-3 * D + 30 * timedelta(minutes=1),
        as_of=timedelta(0),
    ),
    Case(
        "no_progress false with vitals",
        _stale_appointment_with_vitals,
        "doctor",
        "doctor",
        "notes",
        "T_APPT",
        flags={"no_progress": False},
        at=-3 * D + 30 * timedelta(minutes=1),
        as_of=timedelta(0),
    ),
    Case(
        "no_progress false with a note",
        _stale_appointment_with_note,
        "doctor",
        "doctor",
        "notes",
        "T_APPT",
        flags={"no_progress": False},
        at=-3 * D + 30 * timedelta(minutes=1),
        as_of=timedelta(0),
    ),
    Case(
        "cancelled_after_access unknown at access",
        _cancelled_appointment_still_seen_by_frontdesk,
        "reception",
        "reception",
        "demographics",
        "T_FRONTDESK",
        flags={"cancelled_after_access": None},
    ),
    Case(
        "cancelled_after_access true later",
        _cancelled_later,
        "reception",
        "reception",
        "demographics",
        "T_FRONTDESK",
        flags={"cancelled_after_access": True},
        as_of=D,
    ),
    Case(
        "cancelled_after_access unknown before 24 hours",
        _cancelled_later,
        "reception",
        "reception",
        "demographics",
        "T_FRONTDESK",
        flags={"cancelled_after_access": None},
        as_of=3 * H,
    ),
    Case(
        "cancelled_after_access false when cancelled before the access",
        _cancelled_before,
        "reception",
        "reception",
        "demographics",
        "T_FRONTDESK",
        flags={"cancelled_after_access": False},
        as_of=D,
    ),
    Case(
        "cancelled_after_access false when cancelled after 24 hours",
        _cancelled_much_later,
        "reception",
        "reception",
        "demographics",
        "T_FRONTDESK",
        flags={"cancelled_after_access": False},
        as_of=2 * D,
    ),
    Case(
        "no_progress false when the appointment moved forward",
        _stale_appointment_checked_in,
        "doctor",
        "doctor",
        "notes",
        "T_APPT",
        flags={"no_progress": False},
        at=-3 * D + 30 * M,
        as_of=timedelta(0),
    ),
    # Status at the access time --------------------------------------------------------------------
    Case(
        "appointment cancelled after the access still explains it",
        _cancelled_later,
        "doctor",
        "doctor",
        "notes",
        "T_APPT",
        1.0,
        as_of=D,
    ),
    Case(
        "appointment cancelled before the access does not",
        _cancelled_before,
        "doctor",
        "doctor",
        "notes",
        None,
    ),
    Case(
        "a record without history keeps its stored status",
        _cancelled_without_history,
        "doctor",
        "doctor",
        "notes",
        None,
    ),
    Case(
        "lab order cancelled after the access still explains it",
        _lab_cancelled_after_access,
        "lab",
        "lab_tech",
        "lab",
        "T_LAB",
        1.0,
        flags={"cancelled_after_access": True},
        as_of=D,
    ),
    Case(
        "follow-up cancelled after the access still explains it",
        _completed_visit_then_followup_cancelled_later,
        "doctor",
        "doctor",
        "notes",
        "T_FOLLOWUP",
    ),
    Case(
        "self_creation_high",
        _many_self_created,
        "doctor",
        "doctor",
        "demographics",
        None,
        flags={"self_creation_high": True},
    ),
    Case(
        "self_creation normal",
        _appt_now,
        "doctor",
        "doctor",
        "notes",
        "T_APPT",
        flags={"self_creation_high": False, "self_creation_rate": 1.0},
    ),
]


@pytest.fixture(params=["sql", "memory"])
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
async def test_case(case: Case, backend: str, admin_engine: AsyncEngine) -> None:
    graph = Graph(AT)
    case.build(graph)
    at = AT + case.at
    as_of = AT + case.as_of if case.as_of is not None else None
    query = graph.query(case.user, case.role, at=at, as_of=as_of)
    if backend == "sql":
        await graph.write(admin_engine)
        bundle = await fetch_sql(query)
    else:
        bundle = await fetch_memory(graph, query)

    event = AccessEvent(
        uuid.uuid4(),
        graph.clinic,
        graph.users[case.user],
        case.role,
        graph.patient,
        case.resource,
        "view",
        at,
        reason=case.reason,
    )
    result = explain(event, bundle, as_of=as_of)
    assert result.template_code == case.template
    if case.strength is not None:
        assert result.strength == pytest.approx(case.strength, rel=1e-6)
    for name, expected in case.flags.items():
        assert result.forgery_flags[name] == expected, name
    if case.reason is not None:
        assert result.evidence_json()["reason"]["code"] == case.reason.code


async def test_both_backends_agree_on_every_case(admin_engine: AsyncEngine) -> None:
    for case in CASES:
        graph = Graph(AT)
        case.build(graph)
        await graph.write(admin_engine)
        query = graph.query(
            case.user,
            case.role,
            at=AT + case.at,
            as_of=AT + case.as_of if case.as_of is not None else None,
        )
        assert await fetch_sql(query) == await fetch_memory(graph, query), case.name


def test_any_flag() -> None:
    assert not any_flag(
        {"self_created_recent": None, "no_progress": False, "self_creation_rate": 9}
    )
    assert any_flag({"no_progress": True})
