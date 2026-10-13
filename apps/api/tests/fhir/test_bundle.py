import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.chart.schemas import LabOrderOut, LabResultOut, PrescriptionOut, VitalOut
from app.clinic.schemas import AppointmentOut, ClinicOut, PatientOut
from app.consent.schemas import ConsentOut
from app.fhir.bundle import NRCES, StaffRef, build_bundle
from app.portal.schemas import AccessLogEntry, Because

NOW = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)
CLINIC = uuid.uuid4()
DOCTOR = uuid.uuid4()
PATIENT = uuid.uuid4()


def bundle() -> dict[str, Any]:
    rx = uuid.uuid4()
    return build_bundle(
        patient=PatientOut(
            id=PATIENT,
            mrn="MRN-1",
            name="Asha Demo",
            dob=date(1980, 1, 2),
            sex="female",
            phone="+91 9000000000",
            address="Jaipur",
            abha_number=None,
            emergency_contact=None,
            created_at=NOW,
        ),
        clinic=ClinicOut(
            id=CLINIC, name="Demo Clinic", city="Jaipur", state="Rajasthan", timezone="Asia/Kolkata"
        ),
        staff={DOCTOR: StaffRef(DOCTOR, "Dr Demo", "RJ-1234")},
        appointments=[
            AppointmentOut(
                id=uuid.uuid4(),
                patient_id=PATIENT,
                doctor_user_id=DOCTOR,
                slot_start=NOW,
                slot_end=NOW + timedelta(minutes=10),
                kind="new",
                status="completed",
                source="reception",
                created_at=NOW,
            )
        ],
        vitals=[
            VitalOut(
                id=uuid.uuid4(),
                recorded_at=NOW,
                bp_sys=130,
                bp_dia=84,
                pulse=78,
                temp_c=Decimal("37.2"),
                spo2=98,
                rr=16,
                weight_kg=Decimal("62.5"),
                height_cm=None,
            )
        ],
        prescriptions=[
            PrescriptionOut(
                id=rx,
                version=1,
                parent_id=None,
                author_user_id=DOCTOR,
                items=[
                    {"drug": "Amlodipine", "strength": "5 mg", "dose": "1-0-0", "duration_days": 30}
                ],
                advice_en="Less salt",
                advice_hi=None,
                created_at=NOW,
                signed_at=NOW,
            ),
            PrescriptionOut(
                id=uuid.uuid4(),
                version=1,
                parent_id=None,
                author_user_id=DOCTOR,
                items=[{"drug": "Draft only", "dose": "1-0-0", "duration_days": 1}],
                advice_en=None,
                advice_hi=None,
                created_at=NOW,
                signed_at=None,
            ),
        ],
        lab_orders=[
            LabOrderOut(
                id=uuid.uuid4(),
                tests=[{"code": "CBC", "name": "Complete blood count"}],
                status="resulted",
                ordered_by=DOCTOR,
                created_at=NOW,
                result=LabResultOut(
                    id=uuid.uuid4(),
                    document_id=None,
                    resulted_at=NOW,
                    released_at=NOW,
                    values={
                        "Hb": {"value": 12.9, "unit": "g/dL"},
                        "Note": {"value": "Normal", "unit": ""},
                    },
                ),
            ),
            LabOrderOut(
                id=uuid.uuid4(),
                tests=[{"code": "HB", "name": "Haemoglobin"}],
                status="resulted",
                ordered_by=DOCTOR,
                created_at=NOW,
                result=LabResultOut(
                    id=uuid.uuid4(),
                    document_id=None,
                    resulted_at=NOW,
                    released_at=None,
                    values={"Hb": {"value": 11, "unit": "g/dL"}},
                ),
            ),
        ],
        consents=[
            ConsentOut(
                id=uuid.uuid4(),
                patient_id=PATIENT,
                notice_id=uuid.uuid4(),
                notice_version=1,
                granted_at=NOW,
                channel="reception",
                withdrawn_at=None,
                events=[],
            )
        ],
        access_log=[
            AccessLogEntry(
                id=uuid.uuid4(),
                at=NOW,
                user_name="Dr Demo",
                role="doctor",
                resource="notes",
                action="view",
                decision="allow",
                break_glass=False,
                because=Because(template="T_APPT"),
                query_status=None,
            )
        ],
        exported_at=NOW,
    )


def resources(b: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [e["resource"] for e in b["entry"] if e["resource"]["resourceType"] == kind]


def test_bundle_has_every_resource_type_with_profiles() -> None:
    b = bundle()
    assert b["resourceType"] == "Bundle" and b["type"] == "collection"
    kinds = {e["resource"]["resourceType"] for e in b["entry"]}
    assert kinds == {
        "Organization",
        "Patient",
        "Practitioner",
        "Encounter",
        "Observation",
        "MedicationRequest",
        "DiagnosticReport",
        "Consent",
        "AuditEvent",
    }
    patient = resources(b, "Patient")[0]
    assert patient["meta"]["profile"] == [NRCES + "Patient"]
    assert patient["identifier"][0]["value"] == "MRN-1"
    assert patient["gender"] == "female" and patient["birthDate"] == "1980-01-02"
    practitioner = resources(b, "Practitioner")[0]
    assert practitioner["identifier"][0]["value"] == "RJ-1234"


def test_every_reference_resolves_inside_the_bundle() -> None:
    b = bundle()
    urls = {e["fullUrl"] for e in b["entry"]}
    assert len(urls) == len(b["entry"]), "ids are unique"

    def refs(node: Any) -> list[str]:
        if isinstance(node, dict):
            found = [node["reference"]] if isinstance(node.get("reference"), str) else []
            return found + [r for v in node.values() for r in refs(v)]
        if isinstance(node, list):
            return [r for v in node for r in refs(v)]
        return []

    assert set(refs(b)) <= urls


def test_clinical_content() -> None:
    b = bundle()
    obs = resources(b, "Observation")
    bp = next(o for o in obs if o["code"]["coding"][0]["code"] == "85354-9")
    assert [c["valueQuantity"]["value"] for c in bp["component"]] == [130, 84]
    temp = next(o for o in obs if o["code"]["coding"][0]["code"] == "8310-5")
    assert temp["valueQuantity"] == {
        "value": 37.2,
        "unit": "Cel",
        "system": "http://unitsofmeasure.org",
        "code": "Cel",
    }
    meds = resources(b, "MedicationRequest")
    assert len(meds) == 1, "draft prescriptions are left out"
    assert meds[0]["medicationCodeableConcept"]["text"] == "Amlodipine 5 mg"
    reports = resources(b, "DiagnosticReport")
    assert len(reports) == 1, "unreleased results are left out"
    assert reports[0]["code"]["coding"][0]["code"] == "58410-2"
    assert len(reports[0]["result"]) == 2 and reports[0]["conclusion"]
    note = next(o for o in obs if o.get("code", {}).get("text") == "Note")
    assert note["valueString"] == "Normal"
    audit = resources(b, "AuditEvent")[0]
    assert audit["action"] == "R" and audit["outcome"] == "0"
    assert audit["purposeOfEvent"][0]["text"] == "T_APPT"


def test_no_em_dash_and_json_serialisable() -> None:
    text = json.dumps(bundle(), ensure_ascii=False)
    assert "—" not in text
