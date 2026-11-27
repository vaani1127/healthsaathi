"""Record version leaves (ADR 0011): commitments, content schemas, leaf format, and the guarantee
that adding the new leaf type does not change any existing leaf or root."""

import hashlib
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.db import TenantContext, tenant_session
from app.db import models as m
from app.db.enums import RecordType
from app.ledger.checkpoint import build_checkpoint, load_tree
from app.ledger.records import (
    LEAF_KIND,
    SCHEMAS,
    commitment,
    content_of,
    leaf_payload,
    new_salt,
)
from app.ledger.writer import append_audit_event
from e2d_core.ledger import MerkleTree, leaf_hash, payload_hash, verify_consistency
from e2d_core.ledger.jcs import canonicalize

# The fixed log below and its root were computed with the code at main 61692ec, before record
# versions existed (e2d-core and the ledger writer are unchanged by ADR 0011).
FIXED_ROOT = "d009169190d98affb4e52cd246739d760783fd3ac9e52c10aa961c8e88258868"
PATIENT = "01920000-0000-7000-8000-0000000000a1"
USER = "01920000-0000-7000-8000-0000000000b2"


def fixed_log() -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for i in range(7):
        events.append(
            {
                "type": "access",
                "access_event_id": f"01920000-0000-7000-8000-{i:012d}",
                "at": f"2026-11-0{i + 1}T04:30:00.123456+00:00",
                "user_id": USER,
                "role": "doctor",
                "patient_id": PATIENT,
                "resource": "notes",
                "action": "view",
                "decision": "allow",
                "policy_version": "0" * 64,
                "template": "T_APPT",
                "strength": 0.875,
            }
        )
    events.append(
        {
            "type": "break_glass",
            "break_glass_id": "01920000-0000-7000-8000-00000000bb01",
            "user_id": USER,
            "patient_id": PATIENT,
            "reason_code": "emergency",
            "at": "2026-11-08T10:00:00+00:00",
        }
    )
    events.append(
        {
            "type": "detect_daily",
            "outcome": "skipped",
            "day": "2026-11-08",
            "reason": "older than the 7 day catch-up window when the job ran",
            "at": "2026-11-09T00:00:00+00:00",
        }
    )
    return events


def test_fixed_log_root_is_unchanged() -> None:
    tree = MerkleTree([leaf_hash(payload_hash(p)) for p in fixed_log()])
    assert tree.size == 9
    assert tree.root().hex() == FIXED_ROOT


async def test_record_leaves_extend_a_fixed_log_without_changing_its_root(
    admin_engine: AsyncEngine,
) -> None:
    async with AsyncSession(admin_engine) as s, s.begin():
        clinic = m.Clinic(name="Fixed log clinic", city="Test", state="Test")
        s.add(clinic)
        await s.flush()
        clinic_id = clinic.id
    ctx = TenantContext(clinic_id=clinic_id, role="system")
    async with tenant_session(ctx) as db:
        for payload in fixed_log():
            await append_audit_event(db, clinic_id, str(payload["type"]), payload)
    before = await build_checkpoint(clinic_id)
    assert before.head.root_hex == FIXED_ROOT

    leaf = leaf_payload(
        RecordType.VITAL,
        uuid.UUID("01920000-0000-7000-8000-0000000000c3"),
        1,
        None,
        "vital/v1",
        bytes(32),
        uuid.UUID(USER),
        datetime(2026, 11, 10, tzinfo=UTC),
    )
    async with tenant_session(ctx) as db:
        await append_audit_event(db, clinic_id, LEAF_KIND, leaf)
        tree, _ = await load_tree(db, clinic_id)
    assert tree.size == 10
    assert tree.root(9).hex() == FIXED_ROOT
    assert verify_consistency(
        9, 10, bytes.fromhex(FIXED_ROOT), tree.root(), tree.consistency_proof(9, 10)
    )
    after = await build_checkpoint(clinic_id)
    assert after.created and after.head.prev_root_hex == FIXED_ROOT


# Commitment --------------------------------------------------------------------------------------


def test_commitment_is_sha256_of_salt_and_canonical_content() -> None:
    salt = bytes(range(32))
    content = {"b": 2, "a": "x"}
    assert commitment(salt, content) == hashlib.sha256(salt + b'{"a":"x","b":2}').digest()
    assert commitment(salt, {"a": "x", "b": 2}) == commitment(salt, content)


def test_salt_must_be_32_bytes() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        commitment(b"short", {})


def test_new_salts_differ() -> None:
    assert len(new_salt()) == 32
    assert new_salt() != new_salt()


json_values = st.recursive(
    st.none() | st.booleans() | st.integers(-(2**53), 2**53) | st.text(max_size=20),
    lambda inner: (
        st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=8), inner, max_size=4)
    ),
    max_leaves=12,
)


@settings(max_examples=200, deadline=None)
@given(
    content=st.dictionaries(st.text(max_size=8), json_values, min_size=1, max_size=6),
    salt=st.binary(min_size=32, max_size=32),
    other_salt=st.binary(min_size=32, max_size=32),
    key=st.text(max_size=8),
    value=json_values,
)
def test_any_content_or_salt_change_changes_the_commitment(
    content: dict[str, object], salt: bytes, other_salt: bytes, key: str, value: object
) -> None:
    digest = commitment(salt, content)
    changed = {**content, key: value}
    if canonicalize(changed) != canonicalize(content):
        assert commitment(salt, changed) != digest
    if other_salt != salt:
        assert commitment(other_salt, content) != digest


# Content schemas ---------------------------------------------------------------------------------


def test_every_record_type_has_a_v1_schema() -> None:
    assert set(SCHEMAS) == set(RecordType)
    assert {s.name for s in SCHEMAS.values()} == {f"{t.value}/v1" for t in RecordType}


def test_vital_content_uses_decimal_strings_and_utc_microseconds() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    row = SimpleNamespace(
        id=uuid.UUID(int=1),
        patient_id=uuid.UUID(int=2),
        encounter_id=None,
        recorded_by=uuid.UUID(int=3),
        recorded_at=datetime(2026, 11, 27, 10, 0, tzinfo=ist),
        bp_sys=120,
        bp_dia=80,
        pulse=72,
        temp_c=Decimal("37"),
        spo2=98,
        rr=16,
        weight_kg=Decimal("60.5"),
        height_cm=None,
        created_at=datetime(2026, 11, 27, 4, 30, 0, 5, tzinfo=UTC),
    )
    content = content_of(RecordType.VITAL, row)
    assert content["temp_c"] == "37.0"
    assert content["weight_kg"] == "60.50"
    assert content["height_cm"] is None
    assert content["recorded_at"] == "2026-11-27T04:30:00.000000+00:00"
    assert content["created_at"] == "2026-11-27T04:30:00.000005+00:00"
    assert content["encounter_id"] is None
    canonicalize(content)  # never raises: no floats or Decimals left


def test_condition_content_keeps_the_onset_date() -> None:
    row = SimpleNamespace(
        id=uuid.UUID(int=1),
        patient_id=uuid.UUID(int=2),
        code="J06.9",
        text="Upper respiratory infection",
        onset=date(2026, 11, 20),
        status="active",
        recorded_by=uuid.UUID(int=3),
        created_at=datetime(2026, 11, 27, tzinfo=UTC),
    )
    assert content_of(RecordType.CONDITION, row)["onset"] == "2026-11-20"


def test_document_content_includes_the_file_hash() -> None:
    row = SimpleNamespace(
        id=uuid.UUID(int=1),
        patient_id=uuid.UUID(int=2),
        kind="lab_report",
        blob_path="c/p/d.pdf",
        sha256="ab" * 32,
        uploaded_by=uuid.UUID(int=3),
        created_at=datetime(2026, 11, 27, tzinfo=UTC),
    )
    assert content_of(RecordType.DOCUMENT, row)["sha256"] == "ab" * 32


def test_lab_order_content_leaves_out_the_status() -> None:
    row = SimpleNamespace(
        id=uuid.UUID(int=1),
        patient_id=uuid.UUID(int=2),
        encounter_id=None,
        ordered_by=uuid.UUID(int=3),
        tests=[{"code": "CBC", "name": "Complete blood count"}],
        status="collected",
        created_at=datetime(2026, 11, 27, tzinfo=UTC),
    )
    assert "status" not in content_of(RecordType.LAB_ORDER, row)


# Leaf format -------------------------------------------------------------------------------------


def test_leaf_has_ids_and_the_commitment_only() -> None:
    at = datetime(2026, 11, 27, 4, 30, tzinfo=UTC)
    leaf = leaf_payload(
        RecordType.NOTE,
        uuid.UUID(int=10),
        1,
        uuid.UUID(int=9),
        "note/v1",
        bytes(range(32)),
        uuid.UUID(int=3),
        at,
    )
    assert leaf == {
        "type": "record_version",
        "v": 1,
        "record_type": "note",
        "record_id": str(uuid.UUID(int=10)),
        "version": 1,
        "prev_version_id": str(uuid.UUID(int=9)),
        "content_schema": "note/v1",
        "commitment": bytes(range(32)).hex(),
        "by": str(uuid.UUID(int=3)),
        "at": at.isoformat(),
    }
    for forbidden in ("patient_id", "salt", "content", "body", "backfilled"):
        assert forbidden not in leaf


def test_backfilled_appears_only_when_true() -> None:
    args = (RecordType.VITAL, uuid.UUID(int=1), 1, None, "vital/v1", bytes(32), uuid.UUID(int=2))
    at = datetime(2026, 11, 27, tzinfo=UTC)
    assert "backfilled" not in leaf_payload(*args, at)
    assert leaf_payload(*args, at, backfilled=True)["backfilled"] is True
