"""Output tables: the product's table and column names (SPEC 3.1).

Ids are UUID strings and times are UTC. Columns that only hold secrets or free text in the product
(password hashes, encrypted note bodies) are left out. Labels are not tables: they are written to a
separate folder by the recorder.
"""

import polars as pl

UTC_TS = pl.Datetime("us", "UTC")
S = pl.String()
I = pl.Int64()  # noqa: E741
F = pl.Float64()
B = pl.Boolean()
D = pl.Date()
T = pl.Time()

TABLES: dict[str, dict[str, pl.DataType]] = {
    "clinics": {
        "id": S, "name": S, "city": S, "state": S, "timezone": S, "created_at": UTC_TS,
    },
    "users": {
        "id": S, "email": S, "phone": S, "name": S, "is_active": B, "created_at": UTC_TS,
    },
    "memberships": {
        "id": S, "clinic_id": S, "user_id": S, "role": S, "is_active": B, "created_at": UTC_TS,
    },
    "staff_profiles": {
        "id": S, "clinic_id": S, "user_id": S, "registration_no": S, "specialization": S,
        "department": S,
    },
    "patients": {
        "id": S, "clinic_id": S, "user_id": S, "mrn": S, "name": S, "dob": D, "sex": S,
        "phone": S, "address": S, "abha_number": S, "emergency_contact": S, "created_by": S,
        "created_at": UTC_TS,
    },
    "devices": {
        "id": S, "user_id": S, "fingerprint_hash": S, "user_agent": S, "first_seen": UTC_TS,
        "last_seen": UTC_TS,
    },
    "sessions": {
        "id": S, "user_id": S, "device_id": S, "family_id": S, "expires_at": UTC_TS,
        "revoked_at": UTC_TS, "created_at": UTC_TS,
    },
    "services": {
        "id": S, "clinic_id": S, "name": S, "price_paise": I, "is_active": B,
    },
    "schedules": {
        "id": S, "clinic_id": S, "doctor_user_id": S, "weekday": I, "start_time": T,
        "end_time": T, "slot_minutes": I, "status": S, "created_by": S, "created_at": UTC_TS,
        "updated_at": UTC_TS,
    },
    "shifts": {
        "id": S, "clinic_id": S, "user_id": S, "starts_at": UTC_TS, "ends_at": UTC_TS, "role": S,
        "status": S, "created_by": S, "created_at": UTC_TS, "updated_at": UTC_TS,
    },
    "appointments": {
        "id": S, "clinic_id": S, "patient_id": S, "doctor_user_id": S, "slot_start": UTC_TS,
        "slot_end": UTC_TS, "kind": S, "status": S, "source": S, "created_by": S,
        "created_at": UTC_TS, "updated_at": UTC_TS,
    },
    "queue_tokens": {
        "id": S, "clinic_id": S, "appointment_id": S, "token_no": I, "day": D, "status": S,
        "assigned_nurse_user_id": S, "created_by": S, "created_at": UTC_TS, "updated_at": UTC_TS,
    },
    "encounters": {
        "id": S, "clinic_id": S, "appointment_id": S, "patient_id": S, "doctor_user_id": S,
        "started_at": UTC_TS, "ended_at": UTC_TS, "status": S, "created_by": S,
        "created_at": UTC_TS, "updated_at": UTC_TS,
    },
    "referrals": {
        "id": S, "clinic_id": S, "patient_id": S, "from_user_id": S, "to_user_id": S,
        "reason": S, "valid_until": UTC_TS, "status": S, "created_by": S, "created_at": UTC_TS,
        "updated_at": UTC_TS,
    },
    "care_team_assignments": {
        "id": S, "clinic_id": S, "patient_id": S, "user_id": S, "role": S, "starts_at": UTC_TS,
        "ends_at": UTC_TS, "status": S, "created_by": S, "created_at": UTC_TS,
        "updated_at": UTC_TS,
    },
    "vitals": {
        "id": S, "clinic_id": S, "patient_id": S, "encounter_id": S, "recorded_by": S,
        "recorded_at": UTC_TS, "bp_sys": I, "bp_dia": I, "pulse": I, "temp_c": F, "spo2": I,
        "rr": I, "weight_kg": F, "height_cm": F,
    },
    "allergies": {
        "id": S, "clinic_id": S, "patient_id": S, "substance": S, "reaction": S, "severity": S,
        "is_active": B, "recorded_by": S, "created_at": UTC_TS,
    },
    "conditions": {
        "id": S, "clinic_id": S, "patient_id": S, "code": S, "text": S, "onset": D, "status": S,
        "recorded_by": S, "created_at": UTC_TS,
    },
    "clinical_notes": {
        "id": S, "clinic_id": S, "patient_id": S, "encounter_id": S, "author_user_id": S,
        "version": I, "parent_id": S, "signed_at": UTC_TS, "created_at": UTC_TS,
    },
    "prescriptions": {
        "id": S, "clinic_id": S, "patient_id": S, "encounter_id": S, "author_user_id": S,
        "items": S, "signed_at": UTC_TS, "version": I, "parent_id": S, "created_at": UTC_TS,
    },
    "lab_orders": {
        "id": S, "clinic_id": S, "patient_id": S, "encounter_id": S, "ordered_by": S, "tests": S,
        "status": S, "created_at": UTC_TS, "updated_at": UTC_TS,
    },
    "lab_results": {
        "id": S, "clinic_id": S, "lab_order_id": S, "patient_id": S, "resulted_by": S,
        "resulted_at": UTC_TS, "released_at": UTC_TS,
    },
    "invoices": {
        "id": S, "clinic_id": S, "patient_id": S, "appointment_id": S, "status": S,
        "total_paise": I, "created_by": S, "created_at": UTC_TS, "updated_at": UTC_TS,
    },
    "payments": {
        "id": S, "clinic_id": S, "invoice_id": S, "method": S, "amount_paise": I,
        "received_by": S, "received_at": UTC_TS,
    },
    "consents": {
        "id": S, "clinic_id": S, "patient_id": S, "notice_id": S, "granted_at": UTC_TS,
        "channel": S, "withdrawn_at": UTC_TS,
    },
    "break_glass_events": {
        "id": S, "clinic_id": S, "user_id": S, "patient_id": S, "reason_code": S,
        "reason_text": S, "at": UTC_TS,
    },
    "access_events": {
        "id": S, "clinic_id": S, "user_id": S, "role": S, "patient_id": S, "resource_type": S,
        "action": S, "at": UTC_TS, "session_id": S, "device_id": S, "ip_hash": S,
        "request_id": S, "policy_version": S, "decision": S, "break_glass_id": S,
    },
    # One row per status an appointment, lab order, referral or invoice takes, creation included.
    "status_events": {
        "clinic_id": S, "entity_type": S, "entity_id": S, "status": S, "at": UTC_TS,
    },
}  # fmt: skip

# Kept apart from the tables: what the simulator knows about why an access happened. Feature and
# detector code must never read this folder.
LABEL_TABLES: dict[str, dict[str, pl.DataType]] = {
    "access_labels": {
        "access_event_id": S, "is_attack": B, "attack_type": I, "campaign_id": S, "mimicry": F,
    },
    "campaigns": {
        "campaign_id": S, "clinic_id": S, "attack_type": I, "attack_name": S,
        "actor_user_id": S, "actor_role": S, "mimicry": F, "variant": S, "start_at": UTC_TS,
        "targets": I,
    },
    "benign_scenarios": {"access_event_id": S, "scenario": S},
}  # fmt: skip
