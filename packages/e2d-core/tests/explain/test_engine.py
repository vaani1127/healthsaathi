import math
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from e2d_core.explain import (
    AccessEvent,
    AppointmentEv,
    EvidenceBundle,
    InvoiceEv,
    PatientEv,
    QueueTokenEv,
    ShiftEv,
    default_config,
    explain,
)
from e2d_core.explain.templates import parse_config, parse_duration

CLINIC = uuid.uuid4()
DOCTOR, NURSE, RECEPTION, PATIENT_USER, OTHER = (uuid.uuid4() for _ in range(5))
PATIENT = uuid.uuid4()
# 06:30 UTC is 12:00 in Asia/Kolkata.
NOON_IST = datetime(2026, 10, 8, 6, 30, tzinfo=UTC)
EARLY = NOON_IST - timedelta(days=2)


def event(role: str, user: uuid.UUID, resource: str, at: datetime = NOON_IST) -> AccessEvent:
    return AccessEvent(uuid.uuid4(), CLINIC, user, role, PATIENT, resource, "view", at)


def appointment(
    doctor: uuid.UUID = DOCTOR,
    start: datetime = NOON_IST,
    status: str = "booked",
    created_at: datetime = EARLY,
) -> AppointmentEv:
    return AppointmentEv(
        uuid.uuid4(),
        PATIENT,
        doctor,
        start,
        start + timedelta(minutes=15),
        "new",
        status,
        "reception",
        RECEPTION,
        created_at,
        created_at,
    )


def token(
    day: date = date(2026, 10, 8), nurse: uuid.UUID | None = NURSE, created_at: datetime = EARLY
) -> QueueTokenEv:
    return QueueTokenEv(
        uuid.uuid4(), uuid.uuid4(), PATIENT, 7, day, "waiting", nurse, RECEPTION, created_at
    )


def patient(user: uuid.UUID | None = None, created_at: datetime = EARLY) -> PatientEv:
    return PatientEv(PATIENT, user, RECEPTION, created_at)


# T_APPT --------------------------------------------------------------------------------------


def test_appointment_explains_doctor_inside_window() -> None:
    appt = appointment()
    result = explain(event("doctor", DOCTOR, "notes"), EvidenceBundle(appointments=(appt,)))
    assert result.template_code == "T_APPT"
    assert result.strength == pytest.approx(1.0)
    assert result.evidence[0].id == appt.id


@pytest.mark.parametrize(
    ("offset", "inside"),
    [
        (timedelta(minutes=-30), True),
        (timedelta(minutes=-31), False),
        (timedelta(hours=24, minutes=15), True),
        (timedelta(hours=25), False),
    ],
)
def test_appointment_window_edges(offset: timedelta, inside: bool) -> None:
    result = explain(
        event("doctor", DOCTOR, "notes", NOON_IST + offset),
        EvidenceBundle(appointments=(appointment(),)),
    )
    assert (result.strength == pytest.approx(1.0)) is inside
    assert result.template_code == "T_APPT"


def test_strength_decays_outside_window() -> None:
    spec = default_config().templates["T_APPT"]
    at = NOON_IST + timedelta(minutes=15) + spec.after + spec.tau
    result = explain(
        event("doctor", DOCTOR, "notes", at), EvidenceBundle(appointments=(appointment(),))
    )
    assert result.strength == pytest.approx(spec.weight * math.exp(-1))


def test_appointment_of_another_doctor_or_cancelled_does_not_count() -> None:
    bundle = EvidenceBundle(
        appointments=(appointment(doctor=OTHER), appointment(status="cancelled"))
    )
    result = explain(event("doctor", DOCTOR, "notes"), bundle)
    assert result.template_code is None
    assert result.strength == 0
    assert not result.explained


def test_evidence_created_after_the_access_does_not_count() -> None:
    late = appointment(created_at=NOON_IST + timedelta(minutes=1))
    assert (
        explain(event("doctor", DOCTOR, "notes"), EvidenceBundle(appointments=(late,))).strength
        == 0
    )


def test_small_clock_skew_is_tolerated() -> None:
    skewed = appointment(created_at=NOON_IST + timedelta(seconds=2))
    result = explain(event("doctor", DOCTOR, "notes"), EvidenceBundle(appointments=(skewed,)))
    assert result.template_code == "T_APPT"


def test_appointment_does_not_explain_billing_for_doctor() -> None:
    result = explain(
        event("doctor", DOCTOR, "billing"), EvidenceBundle(appointments=(appointment(),))
    )
    assert result.template_code is None


# T_QUEUE -------------------------------------------------------------------------------------


def test_token_assigned_to_nurse_explains_vitals() -> None:
    t = token()
    result = explain(event("nurse", NURSE, "vitals"), EvidenceBundle(queue_tokens=(t,)))
    assert result.template_code == "T_QUEUE"
    assert result.strength == pytest.approx(0.9)
    assert [e.kind for e in result.evidence] == ["queue_token"]


def test_unassigned_token_needs_nurse_on_shift() -> None:
    t = token(nurse=None)
    shift = ShiftEv(
        uuid.uuid4(),
        NURSE,
        "nurse",
        NOON_IST - timedelta(hours=3),
        NOON_IST + timedelta(hours=5),
        "scheduled",
    )
    off = explain(event("nurse", NURSE, "vitals"), EvidenceBundle(queue_tokens=(t,)))
    assert off.template_code is None
    on = explain(
        event("nurse", NURSE, "vitals"), EvidenceBundle(queue_tokens=(t,), shifts=(shift,))
    )
    assert on.template_code == "T_QUEUE"
    assert [e.kind for e in on.evidence] == ["queue_token", "shift"]


def test_cancelled_shift_does_not_count() -> None:
    t = token(nurse=None)
    shift = ShiftEv(
        uuid.uuid4(),
        NURSE,
        "nurse",
        NOON_IST - timedelta(hours=3),
        NOON_IST + timedelta(hours=5),
        "cancelled",
    )
    assert (
        explain(
            event("nurse", NURSE, "vitals"), EvidenceBundle(queue_tokens=(t,), shifts=(shift,))
        ).template_code
        is None
    )


def test_queue_day_is_the_clinic_local_day() -> None:
    # 23:00 IST on 8 Oct is 17:30 UTC on 8 Oct; 00:30 IST on 9 Oct is 19:00 UTC on 8 Oct.
    late_evening = datetime(2026, 10, 8, 17, 30, tzinfo=UTC)
    after_midnight = datetime(2026, 10, 8, 19, 0, tzinfo=UTC)
    bundle = EvidenceBundle(queue_tokens=(token(),))
    assert explain(event("nurse", NURSE, "vitals", late_evening), bundle).strength == pytest.approx(
        0.9
    )
    assert explain(event("nurse", NURSE, "vitals", after_midnight), bundle).strength < 0.9


def test_nurse_cannot_explain_notes_with_a_token() -> None:
    assert (
        explain(
            event("nurse", NURSE, "notes"), EvidenceBundle(queue_tokens=(token(),))
        ).template_code
        is None
    )


def test_skipped_token_does_not_count() -> None:
    skipped = QueueTokenEv(
        uuid.uuid4(),
        uuid.uuid4(),
        PATIENT,
        3,
        date(2026, 10, 8),
        "skipped",
        NURSE,
        RECEPTION,
        EARLY,
    )
    assert (
        explain(
            event("nurse", NURSE, "vitals"), EvidenceBundle(queue_tokens=(skipped,))
        ).template_code
        is None
    )


# T_FRONTDESK ----------------------------------------------------------------------------------


def test_reception_explained_by_appointment_any_doctor() -> None:
    result = explain(
        event("reception", RECEPTION, "demographics"),
        EvidenceBundle(appointments=(appointment(doctor=OTHER),)),
    )
    assert result.template_code == "T_FRONTDESK"
    assert result.strength == pytest.approx(0.9)


def test_reception_explained_by_booking_time_for_future_slot() -> None:
    future = appointment(
        start=NOON_IST + timedelta(days=10), created_at=NOON_IST - timedelta(minutes=5)
    )
    result = explain(
        event("reception", RECEPTION, "demographics"), EvidenceBundle(appointments=(future,))
    )
    assert result.strength == pytest.approx(0.9)


def test_reception_explained_by_invoice_or_registration() -> None:
    invoice = InvoiceEv(
        uuid.uuid4(), PATIENT, "issued", RECEPTION, NOON_IST - timedelta(hours=2), NOON_IST
    )
    by_invoice = explain(
        event("reception", RECEPTION, "billing"), EvidenceBundle(invoices=(invoice,))
    )
    assert by_invoice.template_code == "T_FRONTDESK"
    assert by_invoice.evidence[0].kind == "invoice"

    fresh = patient(created_at=NOON_IST - timedelta(minutes=1))
    by_registration = explain(
        event("reception", RECEPTION, "consent"), EvidenceBundle(patient=fresh)
    )
    assert by_registration.evidence[0].kind == "registration"


def test_reception_not_explained_for_notes() -> None:
    result = explain(
        event("reception", RECEPTION, "notes"), EvidenceBundle(appointments=(appointment(),))
    )
    assert result.template_code is None


def test_old_registration_decays() -> None:
    old = patient(created_at=NOON_IST - timedelta(days=30))
    result = explain(event("reception", RECEPTION, "demographics"), EvidenceBundle(patient=old))
    assert result.template_code is None


# T_SELF -------------------------------------------------------------------------------------


def test_patient_reads_own_record() -> None:
    result = explain(
        event("patient", PATIENT_USER, "lab"), EvidenceBundle(patient=patient(user=PATIENT_USER))
    )
    assert result.template_code == "T_SELF"
    assert result.strength == 1.0


def test_patient_cannot_explain_someone_else() -> None:
    result = explain(
        event("patient", OTHER, "lab"), EvidenceBundle(patient=patient(user=PATIENT_USER))
    )
    assert result.template_code is None


# Selection -------------------------------------------------------------------------------------


def test_best_candidate_wins_and_alternatives_are_kept() -> None:
    near = appointment(start=NOON_IST)
    far = appointment(start=NOON_IST - timedelta(hours=26, minutes=15))
    result = explain(event("doctor", DOCTOR, "notes"), EvidenceBundle(appointments=(far, near)))
    assert result.evidence[0].id == near.id
    assert len(result.alternatives) == 1
    assert result.alternatives[0].evidence[0].id == far.id
    payload = result.evidence_json()
    assert payload["refs"][0]["id"] == str(near.id)
    assert payload["alternatives"][0]["template"] == "T_APPT"


def test_ties_go_to_the_closest_evidence() -> None:
    a = appointment(start=NOON_IST - timedelta(hours=2))
    b = appointment(start=NOON_IST - timedelta(minutes=10))
    result = explain(event("doctor", DOCTOR, "notes"), EvidenceBundle(appointments=(a, b)))
    assert result.evidence[0].id == b.id


# Config ----------------------------------------------------------------------------------------


def test_config_lookup() -> None:
    config = default_config()
    assert "notes" in config.templates["T_APPT"].resources
    assert config.get("T_UNKNOWN") is None


def test_config_validation_and_durations() -> None:
    assert parse_duration("90m") == timedelta(minutes=90)
    assert parse_duration(30) == timedelta(seconds=30)
    with pytest.raises(ValueError):
        parse_duration("5 weeks")
    with pytest.raises(ValueError):
        parse_config(
            {"templates": {"T_X": {"roles": ["doctor"], "resources": ["notes"], "weight": 2}}}
        )
    with pytest.raises(ValueError, match="unknown resource group"):
        parse_config(
            {"templates": {"T_X": {"roles": ["doctor"], "resources": "nope", "weight": 1}}}
        )
    custom = parse_config(
        {
            "resource_groups": {"mini": ["vitals"]},
            "templates": {
                "T_X": {
                    "roles": ["doctor", "nurse"],
                    "resources": ["notes", "vitals"],
                    "weight": 0.5,
                    "role_resources": {"nurse": "mini"},
                }
            },
            "gate": {"theta": 0.4},
        }
    )
    spec = custom.templates["T_X"]
    assert spec.applies_to("doctor", "notes")
    assert spec.applies_to("nurse", "vitals")
    assert not spec.applies_to("nurse", "notes")
    assert not spec.applies_to("reception", "vitals")
    assert custom.theta == 0.4
    assert custom.forgery.no_progress_after == timedelta(hours=24)


def test_default_config_has_every_spec_template() -> None:
    expected = {
        "T_APPT",
        "T_QUEUE",
        "T_FRONTDESK",
        "T_LAB",
        "T_REFERRAL",
        "T_CARETEAM",
        "T_FOLLOWUP",
        "T_BILLING",
        "T_REASON",
        "T_BREAKGLASS",
        "T_SELF",
    }
    config = default_config()
    assert set(config.templates) == expected
    weights = {code: spec.weight for code, spec in config.templates.items()}
    assert weights == {
        "T_APPT": 1.0,
        "T_QUEUE": 0.9,
        "T_FRONTDESK": 0.9,
        "T_LAB": 1.0,
        "T_REFERRAL": 0.8,
        "T_CARETEAM": 0.8,
        "T_FOLLOWUP": 0.6,
        "T_BILLING": 0.8,
        "T_REASON": 0.3,
        "T_BREAKGLASS": 0.2,
        "T_SELF": 1.0,
    }
    assert config.theta == 0.5
