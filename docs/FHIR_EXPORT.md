# FHIR export

A patient's download includes their record as a FHIR R4 `collection` Bundle
(`apps/api/app/fhir/bundle.py`). FHIR R4 export follows NRCeS ABDM profiles, validated offline.
Not ABDM certified.

## Resources

Patient, Organization, Practitioner, Encounter, Observation (vital signs and lab values),
MedicationRequest, DiagnosticReport, Consent and AuditEvent. They follow the NRCeS ABDM profiles
(IG package `ndhm.in` 6.5.0) where one exists; Consent and AuditEvent use the base R4 resources,
because the IG has no profile for them. The bundle is built from the data the patient download
already returns.

## Validation

`make fhir-validate` exports 10 demo patients and checks them with the HAPI FHIR validator 7.0.1
against `ndhm.in#6.5.0` (`infra/fhir/validate.sh`). It runs without a terminology server, so
codes are checked for structure and profile rules only: it does not look codes up in LOINC,
SNOMED CT or UCUM, and it cannot say whether a code exists in a value set. Any error fails the
run; warnings and notes are counted.

Narrative (`dom-6`, "a resource should have narrative") is optional in FHIR and is left out on
purpose: the data is structured and the app shows it in its own screens.

## AuditEvent.purposeOfEvent

Each access in the log becomes an AuditEvent. `purposeOfEvent` keeps the explanation template as
its text and adds a code from `http://terminology.hl7.org/CodeSystem/v3-ActReason` where one
fits. A break-glass access is always `ETREAT`, whatever its template.

| What explained the access | Template | Code |
|---|---|---|
| Appointment with the doctor | `T_APPT` | `TREAT` (treatment) |
| Queue token (nurse) | `T_QUEUE` | `TREAT` |
| Lab order (lab technician) | `T_LAB` | `TREAT` |
| Referral | `T_REFERRAL` | `TREAT` |
| Care team | `T_CARETEAM` | `TREAT` |
| Follow-up | `T_FOLLOWUP` | `TREAT` |
| Front desk (registration, appointment work) | `T_FRONTDESK` | `HOPERAT` (healthcare operations) |
| Billing | `T_BILLING` | `HPAYMT` (healthcare payment) |
| Break-glass | `T_BREAKGLASS`, or any access with break-glass | `ETREAT` (emergency treatment) |
| The patient's own access | `T_SELF` | `PATRQT` (patient requested) |
| Typed reason only, no workflow evidence | `T_REASON` | none |
| Unexplained | none | none |

The map is `PURPOSE_OF_EVENT` in `apps/api/app/fhir/bundle.py`; a test fails if a template in
`packages/e2d-core/src/e2d_core/explain/templates.yaml` is missing from it.
