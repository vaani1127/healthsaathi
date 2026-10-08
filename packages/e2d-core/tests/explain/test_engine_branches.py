"""Evidence that must be ignored: wrong patient, wrong user, created after the access, inactive."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from e2d_core.explain import AccessEvent, EvidenceBundle, explain
from e2d_core.explain.model import (
    AccessReason,
    BreakGlassEv,
    CareTeamEv,
    EncounterEv,
    InvoiceEv,
    LabOrderEv,
    PatientEv,
    ReferralEv,
)

AT = datetime(2026, 10, 8, 6, 30, tzinfo=UTC)
USER, OTHER = uuid.uuid4(), uuid.uuid4()
PATIENT, OTHER_PATIENT = uuid.uuid4(), uuid.uuid4()
LATER = AT + timedelta(hours=1)
EARLY = AT - timedelta(days=1)


def event(role: str, resource: str, reason: AccessReason | None = None) -> AccessEvent:
    return AccessEvent(
        uuid.uuid4(), uuid.uuid4(), USER, role, PATIENT, resource, "view", AT, reason=reason
    )


def lab(
    patient: uuid.UUID = PATIENT, status: str = "ordered", created: datetime = EARLY
) -> LabOrderEv:
    return LabOrderEv(uuid.uuid4(), patient, OTHER, status, created, created, None)


def referral(
    to: uuid.UUID = USER,
    patient: uuid.UUID = PATIENT,
    status: str = "active",
    created: datetime = EARLY,
) -> ReferralEv:
    return ReferralEv(
        uuid.uuid4(), patient, OTHER, to, AT + timedelta(days=3), status, OTHER, created, created
    )


def member(
    user: uuid.UUID = USER, patient: uuid.UUID = PATIENT, created: datetime = EARLY
) -> CareTeamEv:
    return CareTeamEv(uuid.uuid4(), patient, user, "nurse", EARLY, None, "active", OTHER, created)


def invoice(
    patient: uuid.UUID = PATIENT, status: str = "issued", created: datetime = EARLY
) -> InvoiceEv:
    return InvoiceEv(uuid.uuid4(), patient, status, OTHER, created, created)


def glass(
    user: uuid.UUID = USER, patient: uuid.UUID = PATIENT, at: datetime = EARLY
) -> BreakGlassEv:
    return BreakGlassEv(uuid.uuid4(), user, patient, "emergency", at)


@pytest.mark.parametrize(
    ("role", "resource", "bundle"),
    [
        ("lab_tech", "lab", EvidenceBundle(lab_orders=(lab(patient=OTHER_PATIENT),))),
        ("lab_tech", "lab", EvidenceBundle(lab_orders=(lab(status="cancelled"),))),
        ("lab_tech", "lab", EvidenceBundle(lab_orders=(lab(created=LATER),))),
        ("doctor", "notes", EvidenceBundle(referrals=(referral(to=OTHER),))),
        ("doctor", "notes", EvidenceBundle(referrals=(referral(patient=OTHER_PATIENT),))),
        ("doctor", "notes", EvidenceBundle(referrals=(referral(status="completed"),))),
        ("doctor", "notes", EvidenceBundle(referrals=(referral(created=LATER),))),
        ("nurse", "vitals", EvidenceBundle(care_team=(member(user=OTHER),))),
        ("nurse", "vitals", EvidenceBundle(care_team=(member(patient=OTHER_PATIENT),))),
        ("nurse", "vitals", EvidenceBundle(care_team=(member(created=LATER),))),
        ("reception", "billing", EvidenceBundle(invoices=(invoice(patient=OTHER_PATIENT),))),
        (
            "reception",
            "billing",
            EvidenceBundle(invoices=(invoice(status="void", created=AT - timedelta(days=30)),)),
        ),
        ("reception", "billing", EvidenceBundle(invoices=(invoice(created=LATER),))),
        ("nurse", "allergies", EvidenceBundle(break_glass=(glass(user=OTHER),))),
        ("nurse", "allergies", EvidenceBundle(break_glass=(glass(patient=OTHER_PATIENT),))),
        ("nurse", "allergies", EvidenceBundle(break_glass=(glass(at=LATER),))),
        (
            "reception",
            "demographics",
            EvidenceBundle(patient=PatientEv(OTHER_PATIENT, None, OTHER, EARLY)),
        ),
        (
            "reception",
            "demographics",
            EvidenceBundle(patient=PatientEv(PATIENT, None, OTHER, LATER)),
        ),
        (
            "doctor",
            "notes",
            EvidenceBundle(
                encounters=(EncounterEv(uuid.uuid4(), PATIENT, USER, EARLY, USER, EARLY),)
            ),
        ),
    ],
)
def test_irrelevant_evidence_is_ignored(role: str, resource: str, bundle: EvidenceBundle) -> None:
    assert explain(event(role, resource), bundle).template_code is None


def test_blank_reason_does_not_explain() -> None:
    assert (
        explain(
            event("doctor", "notes", AccessReason("other", "   ")), EvidenceBundle()
        ).template_code
        is None
    )


def test_void_invoice_still_counts_for_frontdesk_window() -> None:
    result = explain(
        event("reception", "billing"), EvidenceBundle(invoices=(invoice(status="void"),))
    )
    assert result.template_code == "T_FRONTDESK"
