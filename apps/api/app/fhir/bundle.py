"""FHIR R4 export of one patient's record (SPEC 1.2, P17).

A `collection` Bundle with Patient, Organization, Practitioner, Encounter, Observation (vital
signs and lab values), MedicationRequest, DiagnosticReport, Consent and AuditEvent resources.
Resources follow the NRCeS ABDM profiles (package ndhm.in 6.5.0) where one exists; Consent and
AuditEvent use the base R4 resources because the IG has no profile for them. The bundle is built
from the same data the patient download already returns, so it adds no access of its own.

FHIR R4 export follows NRCeS ABDM profiles, validated offline. Not ABDM certified.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.chart.schemas import LabOrderOut, PrescriptionOut, VitalOut
from app.clinic.schemas import AppointmentOut, ClinicOut, PatientOut
from app.consent.schemas import ConsentOut
from app.portal.schemas import AccessLogEntry

NRCES = "https://nrces.in/ndhm/fhir/r4/StructureDefinition/"
LOINC = "http://loinc.org"
UCUM = "http://unitsofmeasure.org"
SNOMED = "http://snomed.info/sct"
V2_0203 = "http://terminology.hl7.org/CodeSystem/v2-0203"
OBS_CATEGORY = "http://terminology.hl7.org/CodeSystem/observation-category"
ACT_CODE = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
IDENTIFIER_SYSTEM = "https://healthsaathi.dev/fhir/identifier/"

# Vital signs: (field, LOINC code, display, UCUM unit). Blood pressure is one panel.
VITALS = (
    ("pulse", "8867-4", "Heart rate", "/min"),
    ("rr", "9279-1", "Respiratory rate", "/min"),
    ("temp_c", "8310-5", "Body temperature", "Cel"),
    ("spo2", "59408-5", "Oxygen saturation in Arterial blood by Pulse oximetry", "%"),
    ("weight_kg", "29463-7", "Body weight", "kg"),
    ("height_cm", "8302-2", "Body height", "cm"),
)
BP_PANEL = ("85354-9", "Blood pressure panel with all children optional")
BP_SYSTOLIC = ("8480-6", "Systolic blood pressure")
BP_DIASTOLIC = ("8462-4", "Diastolic blood pressure")

# Lab panels the clinic orders, by the product's test code.
LAB_PANELS = {
    "CBC": ("58410-2", "CBC panel - Blood by Automated count"),
    "HB": ("718-7", "Hemoglobin [Mass/volume] in Blood"),
    "HBA1C": ("4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood"),
    "LIPID": ("57698-3", "Lipid panel with direct LDL - Serum or Plasma"),
    "URINE": ("24356-8", "Urinalysis complete panel - Urine"),
    "FBS": ("1558-6", "Fasting glucose [Mass/volume] in Serum or Plasma"),
    "RBS": ("2345-7", "Glucose [Mass/volume] in Serum or Plasma"),
    "LFT": ("24325-3", "Hepatic function 2000 panel - Serum or Plasma"),
    "KFT": ("24362-6", "Renal function 2000 panel - Serum or Plasma"),
}
GENERIC_LAB = ("11502-2", "Laboratory report")
AUDIT_ACTION = {"view": "R", "create": "C", "edit": "U", "export": "R", "print": "R"}
GENDER = {"female": "female", "male": "male", "other": "other", "unknown": "unknown"}


@dataclass(frozen=True)
class StaffRef:
    user_id: uuid.UUID
    name: str
    registration_no: str | None = None


def _urn(value: uuid.UUID | str) -> str:
    return f"urn:uuid:{value}"


def _instant(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _coding(system: str, code: str, display: str) -> dict[str, str]:
    return {"system": system, "code": code, "display": display}


def _number(value: Any) -> float | int:
    number = Decimal(str(value))
    return int(number) if number == number.to_integral_value() else float(number)


class BundleBuilder:
    def __init__(
        self, patient: PatientOut, clinic: ClinicOut, staff: dict[uuid.UUID, StaffRef]
    ) -> None:
        self.patient = patient
        self.clinic = clinic
        self.staff = staff
        self.entries: list[dict[str, Any]] = []
        self.practitioners: set[uuid.UUID] = set()
        self.patient_ref = {"reference": _urn(patient.id), "display": patient.name}
        self.org_ref = {"reference": _urn(clinic.id), "display": clinic.name}

    def add(self, resource: dict[str, Any]) -> dict[str, Any]:
        self.entries.append({"fullUrl": _urn(resource["id"]), "resource": resource})
        return resource

    def practitioner(self, user_id: uuid.UUID) -> dict[str, str]:
        ref = self.staff.get(user_id) or StaffRef(user_id, "Clinic staff member")
        if user_id not in self.practitioners:
            self.practitioners.add(user_id)
            licence = ref.registration_no
            self.add(
                {
                    "resourceType": "Practitioner",
                    "id": str(user_id),
                    "meta": {"profile": [NRCES + "Practitioner"]},
                    "identifier": [
                        {
                            "type": {
                                "coding": [
                                    _coding(V2_0203, "MD", "Medical License number")
                                    if licence
                                    else _coding(V2_0203, "EI", "Employee number")
                                ]
                            },
                            "system": IDENTIFIER_SYSTEM + ("licence" if licence else "staff"),
                            "value": licence or str(user_id),
                        }
                    ],
                    "name": [{"text": ref.name}],
                }
            )
        return {"reference": _urn(user_id), "display": ref.name}

    def organization(self) -> None:
        self.add(
            {
                "resourceType": "Organization",
                "id": str(self.clinic.id),
                "meta": {"profile": [NRCES + "Organization"]},
                "identifier": [
                    {
                        "type": {"coding": [_coding(V2_0203, "PRN", "Provider number")]},
                        "system": IDENTIFIER_SYSTEM + "clinic",
                        "value": str(self.clinic.id),
                    }
                ],
                "name": self.clinic.name,
                "address": [
                    {"city": self.clinic.city, "state": self.clinic.state, "country": "IN"}
                ],
            }
        )

    def patient_resource(self) -> None:
        p = self.patient
        identifiers: list[dict[str, Any]] = [
            {
                "type": {"coding": [_coding(V2_0203, "MR", "Medical record number")]},
                "system": IDENTIFIER_SYSTEM + f"mrn/{self.clinic.id}",
                "value": p.mrn,
            }
        ]
        if p.abha_number:
            identifiers.append(
                {
                    "type": {"coding": [_coding(V2_0203, "SN", "Subscriber Number")]},
                    "system": "https://healthid.ndhm.gov.in",
                    "value": p.abha_number,
                }
            )
        resource: dict[str, Any] = {
            "resourceType": "Patient",
            "id": str(p.id),
            "meta": {"profile": [NRCES + "Patient"]},
            "identifier": identifiers,
            "name": [{"text": p.name}],
            "gender": GENDER.get(str(p.sex), "unknown"),
            "managingOrganization": self.org_ref,
        }
        if p.dob:
            resource["birthDate"] = p.dob.isoformat()
        if p.phone:
            resource["telecom"] = [{"system": "phone", "value": p.phone, "use": "mobile"}]
        if p.address:
            resource["address"] = [{"text": p.address, "country": "IN"}]
        self.add(resource)

    def encounters(self, appointments: list[AppointmentOut]) -> None:
        for a in appointments:
            status = {
                "completed": "finished",
                "in_consult": "in-progress",
                "checked_in": "arrived",
                "booked": "planned",
                "cancelled": "cancelled",
                "no_show": "cancelled",
            }.get(str(a.status), "unknown")
            self.add(
                {
                    "resourceType": "Encounter",
                    "id": str(a.id),
                    "meta": {"profile": [NRCES + "Encounter"]},
                    "status": status,
                    "class": _coding(ACT_CODE, "AMB", "ambulatory"),
                    "subject": self.patient_ref,
                    "participant": [{"individual": self.practitioner(a.doctor_user_id)}],
                    "period": {"start": _instant(a.slot_start), "end": _instant(a.slot_end)},
                    "serviceProvider": self.org_ref,
                }
            )

    def _vital(
        self, vital: VitalOut, key: str, code: tuple[str, str], extra: dict[str, Any]
    ) -> None:
        self.add(
            {
                "resourceType": "Observation",
                "id": str(uuid.uuid5(vital.id, key)),
                "meta": {"profile": [NRCES + "ObservationVitalSigns"]},
                "status": "final",
                "category": [{"coding": [_coding(OBS_CATEGORY, "vital-signs", "Vital Signs")]}],
                "code": {"coding": [_coding(LOINC, *code)], "text": code[1]},
                "subject": self.patient_ref,
                "effectiveDateTime": _instant(vital.recorded_at),
                **extra,
            }
        )

    def vitals(self, vitals: list[VitalOut]) -> None:
        for v in vitals:
            if v.bp_sys is not None and v.bp_dia is not None:
                components = [
                    {
                        "code": {"coding": [_coding(LOINC, *code)]},
                        "valueQuantity": {
                            "value": value,
                            "unit": "mmHg",
                            "system": UCUM,
                            "code": "mm[Hg]",
                        },
                    }
                    for code, value in ((BP_SYSTOLIC, v.bp_sys), (BP_DIASTOLIC, v.bp_dia))
                ]
                self._vital(v, "bp", BP_PANEL, {"component": components})
            for field, loinc, display, unit in VITALS:
                value = getattr(v, field)
                if value is None:
                    continue
                quantity = {"value": _number(value), "unit": unit, "system": UCUM, "code": unit}
                self._vital(v, field, (loinc, display), {"valueQuantity": quantity})
                if field == "spo2":
                    # The R4 oxygen saturation profile requires the general code 2708-6 too.
                    self.entries[-1]["resource"]["code"]["coding"].insert(
                        0, _coding(LOINC, "2708-6", "Oxygen saturation in Arterial blood")
                    )

    def prescriptions(self, prescriptions: list[PrescriptionOut]) -> None:
        superseded = {rx.parent_id for rx in prescriptions if rx.parent_id}
        for rx in prescriptions:
            if rx.signed_at is None:
                continue  # drafts are not prescriptions yet
            status = "stopped" if rx.id in superseded else "active"
            for i, item in enumerate(rx.items):
                drug = " ".join(
                    str(x) for x in (item.get("drug"), item.get("strength")) if x
                ).strip()
                dose = str(item.get("dose") or "")
                days = item.get("duration_days")
                text = ", ".join(
                    x
                    for x in (
                        dose,
                        f"{days} days" if days else "",
                        str(item.get("instructions_en") or ""),
                    )
                    if x
                )
                dosage: dict[str, Any] = {"text": text or dose or "As directed"}
                if isinstance(days, int) and days > 0:
                    dosage["timing"] = {
                        "repeat": {
                            "boundsDuration": {
                                "value": days,
                                "unit": "days",
                                "system": UCUM,
                                "code": "d",
                            }
                        }
                    }
                self.add(
                    {
                        "resourceType": "MedicationRequest",
                        "id": str(uuid.uuid5(rx.id, str(i))),
                        "meta": {"profile": [NRCES + "MedicationRequest"]},
                        "status": status,
                        "intent": "order",
                        "medicationCodeableConcept": {"text": drug or "Medicine"},
                        "subject": self.patient_ref,
                        "authoredOn": _instant(rx.signed_at),
                        "requester": self.practitioner(rx.author_user_id),
                        "dosageInstruction": [dosage],
                        **({"note": [{"text": rx.advice_en}]} if rx.advice_en else {}),
                    }
                )

    def lab_reports(self, orders: list[LabOrderOut]) -> None:
        for order in orders:
            result = order.result
            if result is None or result.released_at is None:
                continue  # only released results are part of the record
            tests = [t for t in order.tests if isinstance(t, dict)]
            codes = [str(t.get("code", "")) for t in tests]
            names = [str(t.get("name") or t.get("code") or "") for t in tests]
            panel = LAB_PANELS.get(codes[0], GENERIC_LAB) if len(codes) == 1 else GENERIC_LAB
            observation_refs = []
            for label, raw in result.values.items():
                value = raw if isinstance(raw, dict) else {"value": raw}
                obs: dict[str, Any] = {
                    "resourceType": "Observation",
                    "id": str(uuid.uuid5(result.id, label)),
                    "meta": {"profile": [NRCES + "Observation"]},
                    "status": "final",
                    "category": [{"coding": [_coding(OBS_CATEGORY, "laboratory", "Laboratory")]}],
                    "code": {"text": label},
                    "subject": self.patient_ref,
                    "effectiveDateTime": _instant(result.resulted_at),
                }
                number = value.get("value")
                unit = str(value.get("unit") or "")
                if isinstance(number, int | float) and not isinstance(number, bool):
                    quantity: dict[str, Any] = {"value": number}
                    if unit:
                        quantity["unit"] = unit
                    obs["valueQuantity"] = quantity
                else:
                    obs["valueString"] = f"{number} {unit}".strip()
                self.add(obs)
                observation_refs.append({"reference": _urn(obs["id"]), "display": label})
            if not observation_refs:
                continue
            interpreter = self.practitioner(order.ordered_by)
            self.add(
                {
                    "resourceType": "DiagnosticReport",
                    "id": str(result.id),
                    "meta": {"profile": [NRCES + "DiagnosticReportLab"]},
                    "status": "final",
                    "category": [{"coding": [_coding(SNOMED, "15220000", "Laboratory test")]}],
                    "code": {"coding": [_coding(LOINC, *panel)], "text": ", ".join(names)},
                    "subject": self.patient_ref,
                    "effectiveDateTime": _instant(result.resulted_at),
                    "issued": _instant(result.released_at),
                    "performer": [self.org_ref],
                    "resultsInterpreter": [interpreter],
                    "result": observation_refs,
                    "conclusion": "Results released by the clinic laboratory.",
                }
            )

    def consents(self, consents: list[ConsentOut]) -> None:
        for c in consents:
            period: dict[str, str] = {"start": _instant(c.granted_at)}
            if c.withdrawn_at:
                period["end"] = _instant(c.withdrawn_at)
            self.add(
                {
                    "resourceType": "Consent",
                    "id": str(c.id),
                    "status": "inactive" if c.withdrawn_at else "active",
                    "scope": {
                        "coding": [
                            _coding(
                                "http://terminology.hl7.org/CodeSystem/consentscope",
                                "patient-privacy",
                                "Privacy Consent",
                            )
                        ]
                    },
                    "category": [{"coding": [_coding(LOINC, "59284-0", "Consent Document")]}],
                    "patient": self.patient_ref,
                    "dateTime": _instant(c.granted_at),
                    "organization": [self.org_ref],
                    "policy": [
                        {
                            "uri": f"{IDENTIFIER_SYSTEM}consent-notice/{c.notice_id}",
                        }
                    ],
                    "provision": {"type": "permit", "period": period},
                }
            )

    def audit_events(self, log: list[AccessLogEntry]) -> None:
        source = {"observer": self.org_ref}
        for e in log:
            action, resource, role = str(e.action), str(e.resource), str(e.role)
            why = e.because.template or ("break_glass" if e.break_glass else "unexplained")
            self.add(
                {
                    "resourceType": "AuditEvent",
                    "id": str(e.id),
                    "type": _coding(
                        "http://terminology.hl7.org/CodeSystem/audit-event-type",
                        "rest",
                        "Restful Operation",
                    ),
                    "action": AUDIT_ACTION.get(action, "R"),
                    "recorded": _instant(e.at),
                    "outcome": "0" if str(e.decision) == "allow" else "4",
                    "purposeOfEvent": [{"text": f"{why}"}],
                    "agent": [
                        {
                            "who": {"display": e.user_name},
                            "requestor": True,
                            "role": [{"text": role}],
                        }
                    ],
                    "source": source,
                    "entity": [
                        {
                            "what": self.patient_ref,
                            "description": f"{action} {resource}",
                        }
                    ],
                }
            )

    def bundle(self, exported_at: datetime) -> dict[str, Any]:
        return {
            "resourceType": "Bundle",
            "id": str(uuid.uuid5(self.patient.id, _instant(exported_at))),
            "meta": {"lastUpdated": _instant(exported_at)},
            "identifier": {"system": IDENTIFIER_SYSTEM + "export", "value": str(self.patient.id)},
            "type": "collection",
            "timestamp": _instant(exported_at),
            "entry": self.entries,
        }


def build_bundle(
    *,
    patient: PatientOut,
    clinic: ClinicOut,
    staff: dict[uuid.UUID, StaffRef],
    appointments: list[AppointmentOut],
    vitals: list[VitalOut],
    prescriptions: list[PrescriptionOut],
    lab_orders: list[LabOrderOut],
    consents: list[ConsentOut],
    access_log: list[AccessLogEntry],
    exported_at: datetime,
) -> dict[str, Any]:
    builder = BundleBuilder(patient, clinic, staff)
    builder.organization()
    builder.patient_resource()
    builder.encounters(appointments)
    builder.vitals(vitals)
    builder.prescriptions(prescriptions)
    builder.lab_reports(lab_orders)
    builder.consents(consents)
    builder.audit_events(access_log)
    return builder.bundle(exported_at)
