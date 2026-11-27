"""Record integrity (ADR 0011): salted commitments of clinical record versions in the audit log.

Each committed version of a clinical record gets:

- a `record_versions` row with a random 32-byte salt and
  commitment = SHA-256(salt || JCS(content)), and
- a `record_version` leaf in the clinic's audit log with the same commitment, in the same
  transaction as the clinical write.

The leaf holds ids, the content schema name and the commitment only: no patient id, no salt and
no content. Only signed tree heads of the log leave the database (SPEC 6), so nothing about a
record goes to any witness. The leaf is an ordinary audit payload, so leaf hashes, the hash chain,
checkpoints and anchoring are unchanged, and leaves written before this module keep their hashes.

Content schemas (`<type>/v1`) list exactly which columns are committed. Times are UTC ISO 8601
with microseconds, decimals are strings with the column's scale, ids are strings, and note bodies
are the decrypted text (the stored ciphertext changes if data keys are rotated). A schema is never
changed once rows use it; a change needs a new schema name.

What is committed when (ADR 0011): signed notes and prescriptions on sign (one version per row,
linked to the parent row's version); vitals, documents and lab orders on insert; lab results on
insert and on release; allergies and conditions on insert and on every status change. A lab
order's status is not in its content (status history is in status_events).

The check here is the shared core of the integrity endpoint and the admin audit status. It never
grants access: it only labels what it finds.
"""

import hashlib
import secrets
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.clinical.notes_crypto import decrypt_body
from app.db.enums import AnchorBackend, AnchorStatus, RecordType, ResourceType
from app.db.models import (
    Allergy,
    AnchorReceipt,
    AuditEvent,
    ClinicalNote,
    Condition,
    Document,
    LabOrder,
    LabResult,
    MerkleCheckpoint,
    Prescription,
    RecordVersion,
    Vital,
)
from app.ledger.writer import append_audit_event, append_audit_events_with_seqs, lock_clinic
from e2d_core.ids import uuid7
from e2d_core.ledger.jcs import canonicalize

LEAF_KIND = "record_version"
LEAF_TYPE = "record_version"
LEAF_FORMAT = 1
SALT_BYTES = 32

MODELS: dict[RecordType, type[Any]] = {
    RecordType.NOTE: ClinicalNote,
    RecordType.PRESCRIPTION: Prescription,
    RecordType.VITAL: Vital,
    RecordType.ALLERGY: Allergy,
    RecordType.CONDITION: Condition,
    RecordType.LAB_ORDER: LabOrder,
    RecordType.LAB_RESULT: LabResult,
    RecordType.DOCUMENT: Document,
}

# Who may check a record is decided by the access policy for this resource (conditions are part
# of the clinical record and logged under notes, ADR 0005).
RESOURCES: dict[RecordType, ResourceType] = {
    RecordType.NOTE: ResourceType.NOTES,
    RecordType.CONDITION: ResourceType.NOTES,
    RecordType.PRESCRIPTION: ResourceType.PRESCRIPTIONS,
    RecordType.VITAL: ResourceType.VITALS,
    RecordType.ALLERGY: ResourceType.ALLERGIES,
    RecordType.LAB_ORDER: ResourceType.LAB,
    RecordType.LAB_RESULT: ResourceType.LAB,
    RecordType.DOCUMENT: ResourceType.DOCUMENTS,
}

# Witnesses that count as anchored for the verdict: the same as for patient receipts.
CHAIN_BACKENDS = (AnchorBackend.AMOY, AnchorBackend.BESU)

# Rows that are a chain of versions, one row per version (parent_id).
CHAINED = frozenset({RecordType.NOTE, RecordType.PRESCRIPTION})


# Commitment -------------------------------------------------------------------------------------


def new_salt() -> bytes:
    return secrets.token_bytes(SALT_BYTES)


def commitment(salt: bytes, content: dict[str, Any]) -> bytes:
    """SHA-256(salt || JCS(content)). The salt stops anyone guessing small contents (one blood
    pressure reading) from the commitment."""
    if len(salt) != SALT_BYTES:
        raise ValueError("salt must be 32 bytes")
    return hashlib.sha256(salt + canonicalize(content)).digest()


# Content schemas --------------------------------------------------------------------------------


def _ts(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat(timespec="microseconds")


def _dec(value: Decimal | float | None, places: int) -> str | None:
    return None if value is None else f"{Decimal(str(value)):.{places}f}"


def _id(value: uuid.UUID | None) -> str | None:
    return None if value is None else str(value)


def _note(row: ClinicalNote) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "patient_id": str(row.patient_id),
        "encounter_id": str(row.encounter_id),
        "author_user_id": str(row.author_user_id),
        "version": row.version,
        "parent_id": _id(row.parent_id),
        "body": decrypt_body(row.id, row.body_enc),
        "created_at": _ts(row.created_at),
        "signed_at": _ts(row.signed_at),
    }


def _prescription(row: Prescription) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "patient_id": str(row.patient_id),
        "encounter_id": str(row.encounter_id),
        "author_user_id": str(row.author_user_id),
        "version": row.version,
        "parent_id": _id(row.parent_id),
        "items": row.items,
        "advice_en": row.advice_en,
        "advice_hi": row.advice_hi,
        "created_at": _ts(row.created_at),
        "signed_at": _ts(row.signed_at),
    }


def _vital(row: Vital) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "patient_id": str(row.patient_id),
        "encounter_id": _id(row.encounter_id),
        "recorded_by": str(row.recorded_by),
        "recorded_at": _ts(row.recorded_at),
        "bp_sys": row.bp_sys,
        "bp_dia": row.bp_dia,
        "pulse": row.pulse,
        "temp_c": _dec(row.temp_c, 1),
        "spo2": row.spo2,
        "rr": row.rr,
        "weight_kg": _dec(row.weight_kg, 2),
        "height_cm": _dec(row.height_cm, 1),
        "created_at": _ts(row.created_at),
    }


def _allergy(row: Allergy) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "patient_id": str(row.patient_id),
        "substance": row.substance,
        "reaction": row.reaction,
        "severity": str(row.severity),
        "is_active": row.is_active,
        "recorded_by": str(row.recorded_by),
        "created_at": _ts(row.created_at),
    }


def _condition(row: Condition) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "patient_id": str(row.patient_id),
        "code": row.code,
        "text": row.text,
        "onset": None if row.onset is None else row.onset.isoformat(),
        "status": str(row.status),
        "recorded_by": str(row.recorded_by),
        "created_at": _ts(row.created_at),
    }


def _lab_order(row: LabOrder) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "patient_id": str(row.patient_id),
        "encounter_id": _id(row.encounter_id),
        "ordered_by": str(row.ordered_by),
        "tests": row.tests,
        "created_at": _ts(row.created_at),
    }


def _lab_result(row: LabResult) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "lab_order_id": str(row.lab_order_id),
        "patient_id": str(row.patient_id),
        "values": row.values,
        "document_id": _id(row.document_id),
        "resulted_by": str(row.resulted_by),
        "resulted_at": _ts(row.resulted_at),
        "released_at": _ts(row.released_at),
    }


def _document(row: Document) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "patient_id": str(row.patient_id),
        "kind": row.kind,
        "blob_path": row.blob_path,
        # SHA-256 of the file bytes, checked again on every download: the attachment is covered.
        "sha256": row.sha256,
        "uploaded_by": str(row.uploaded_by),
        "created_at": _ts(row.created_at),
    }


@dataclass(frozen=True)
class ContentSchema:
    name: str
    build: Callable[[Any], dict[str, Any]]
    author: Callable[[Any], uuid.UUID]


SCHEMAS: dict[RecordType, ContentSchema] = {
    RecordType.NOTE: ContentSchema("note/v1", _note, lambda r: r.author_user_id),
    RecordType.PRESCRIPTION: ContentSchema(
        "prescription/v1", _prescription, lambda r: r.author_user_id
    ),
    RecordType.VITAL: ContentSchema("vital/v1", _vital, lambda r: r.recorded_by),
    RecordType.ALLERGY: ContentSchema("allergy/v1", _allergy, lambda r: r.recorded_by),
    RecordType.CONDITION: ContentSchema("condition/v1", _condition, lambda r: r.recorded_by),
    RecordType.LAB_ORDER: ContentSchema("lab_order/v1", _lab_order, lambda r: r.ordered_by),
    RecordType.LAB_RESULT: ContentSchema("lab_result/v1", _lab_result, lambda r: r.resulted_by),
    RecordType.DOCUMENT: ContentSchema("document/v1", _document, lambda r: r.uploaded_by),
}
SCHEMA_TYPES = {s.name: t for t, s in SCHEMAS.items()}


def content_of(record_type: RecordType, row: Any) -> dict[str, Any]:
    return SCHEMAS[record_type].build(row)


def committable(record_type: RecordType, row: Any) -> bool:
    """Only signed notes and prescriptions are part of the record; drafts are not committed."""
    if record_type in CHAINED:
        return row.signed_at is not None
    return True


def leaf_payload(
    record_type: RecordType,
    record_id: uuid.UUID,
    version: int,
    prev_version_id: uuid.UUID | None,
    content_schema: str,
    digest: bytes,
    by: uuid.UUID,
    at: datetime,
    *,
    backfilled: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": LEAF_TYPE,
        "v": LEAF_FORMAT,
        "record_type": record_type.value,
        "record_id": str(record_id),
        "version": version,
        "prev_version_id": _id(prev_version_id),
        "content_schema": content_schema,
        "commitment": digest.hex(),
        "by": str(by),
        "at": at.isoformat(),
    }
    if backfilled:
        payload["backfilled"] = True
    return payload


# Writing ----------------------------------------------------------------------------------------


async def _clock(db: AsyncSession) -> datetime:
    now = await db.scalar(text("SELECT clock_timestamp()"))
    assert isinstance(now, datetime)
    return now


async def _latest_version(
    db: AsyncSession, clinic_id: uuid.UUID, record_type: RecordType, record_id: uuid.UUID
) -> RecordVersion | None:
    return await db.scalar(
        select(RecordVersion)
        .where(
            RecordVersion.clinic_id == clinic_id,
            RecordVersion.record_type == record_type,
            RecordVersion.record_id == record_id,
        )
        .order_by(RecordVersion.version.desc())
        .limit(1)
    )


async def write_version(
    db: AsyncSession,
    clinic_id: uuid.UUID,
    record_type: RecordType,
    row: Any,
    by: uuid.UUID,
) -> RecordVersion:
    """Commit the row's current content as its next version, with its audit leaf, in the caller's
    transaction. The content is read back from the database first, so it is exactly what a later
    check reads."""
    await db.flush()
    await db.refresh(row)
    if not committable(record_type, row):
        raise ValueError(f"{record_type.value} {row.id} is not signed and cannot be committed")
    # The audit-chain lock serialises writers in this clinic until commit, so the version number
    # read here cannot be taken by a concurrent request.
    await lock_clinic(db, "audit-chain", clinic_id)
    latest = await _latest_version(db, clinic_id, record_type, row.id)
    prev: uuid.UUID | None = latest.id if latest else None
    if latest is None and record_type in CHAINED and row.parent_id is not None:
        parent = await _latest_version(db, clinic_id, record_type, row.parent_id)
        prev = parent.id if parent else None
    version = latest.version + 1 if latest else 1
    schema = SCHEMAS[record_type]
    salt = new_salt()
    digest = commitment(salt, schema.build(row))
    at = await _clock(db)
    version_id = uuid7()
    event = await append_audit_event(
        db,
        clinic_id,
        LEAF_KIND,
        leaf_payload(record_type, row.id, version, prev, schema.name, digest, by, at),
    )
    stored = RecordVersion(
        id=version_id,
        clinic_id=clinic_id,
        patient_id=row.patient_id,
        record_type=record_type,
        record_id=row.id,
        version=version,
        prev_version_id=prev,
        content_schema=schema.name,
        salt=salt,
        commitment=digest,
        audit_seq=event.seq,
        backfilled=False,
        created_by=by,
        created_at=at,
    )
    db.add(stored)
    await db.flush()
    return stored


@dataclass(frozen=True)
class Backfill:
    record_type: RecordType
    row: Any
    prev_version_id: uuid.UUID | None
    # Chosen by the caller, so a later note version in the same batch can link to this one.
    version_id: uuid.UUID = field(default_factory=uuid7)


async def write_backfilled(
    db: AsyncSession, clinic_id: uuid.UUID, items: Sequence[Backfill]
) -> list[RecordVersion]:
    """Version 1 of rows that existed before record versions, marked backfilled, with one insert
    per table. The caller makes sure none of these rows has a version yet."""
    if not items:
        return []
    at = await _clock(db)
    prepared = []
    for item in items:
        schema = SCHEMAS[item.record_type]
        salt = new_salt()
        digest = commitment(salt, schema.build(item.row))
        by = schema.author(item.row)
        payload = leaf_payload(
            item.record_type,
            item.row.id,
            1,
            item.prev_version_id,
            schema.name,
            digest,
            by,
            at,
            backfilled=True,
        )
        prepared.append((item, schema, salt, digest, by, payload))
    seqs = await append_audit_events_with_seqs(db, clinic_id, LEAF_KIND, [p[-1] for p in prepared])
    versions = [
        RecordVersion(
            id=item.version_id,
            clinic_id=clinic_id,
            patient_id=item.row.patient_id,
            record_type=item.record_type,
            record_id=item.row.id,
            version=1,
            prev_version_id=item.prev_version_id,
            content_schema=schema.name,
            salt=salt,
            commitment=digest,
            audit_seq=seq,
            backfilled=True,
            created_by=by,
            created_at=at,
        )
        for (item, schema, salt, digest, by, _), (seq, _) in zip(prepared, seqs, strict=True)
    ]
    db.add_all(versions)
    await db.flush()
    return versions


# Checking ---------------------------------------------------------------------------------------


class VersionStatus(StrEnum):
    MATCH = "match"  # the record's current content recomputes to this, its latest commitment
    SUPERSEDED = "superseded"  # an earlier version of a record that has changed since
    MISMATCH = "mismatch"
    MISSING_ROW = "missing_row"


class Verdict(StrEnum):
    MATCH = "match"
    PENDING = "pending"
    MISMATCH = "mismatch"
    MISSING_VERSION = "missing_version"
    MISSING_ROW = "missing_row"


@dataclass(frozen=True)
class Leaf:
    seq: int
    payload: dict[str, Any]

    @property
    def version(self) -> int:
        return int(self.payload.get("version", 0))


@dataclass
class VersionCheck:
    record_type: RecordType
    record_id: uuid.UUID
    version: int
    status: VersionStatus
    leaf: Leaf | None
    stored: RecordVersion | None
    reason: str | None = None
    content: dict[str, Any] | None = None


@dataclass
class RecordCheck:
    """The checks of one record row: one per committed version, newest last."""

    record_type: RecordType
    record_id: uuid.UUID
    row: Any | None
    versions: list[VersionCheck] = field(default_factory=list)

    @property
    def needs_version(self) -> bool:
        return self.row is not None and committable(self.record_type, self.row)

    @property
    def missing_version(self) -> bool:
        return self.needs_version and not self.versions

    @property
    def current(self) -> VersionCheck | None:
        return self.versions[-1] if self.versions else None


def check_record(
    record_type: RecordType,
    record_id: uuid.UUID,
    row: Any | None,
    stored: Iterable[RecordVersion],
    leaves: Iterable[Leaf],
) -> RecordCheck:
    """Compare a record row, its record_versions rows and its audit leaves.

    The leaves are the reference: they are in the hash chain and under the anchored roots. A
    record_versions row must agree with its leaf (same commitment and audit seq), and the newest
    version must recompute from the row as it is now.
    """
    result = RecordCheck(record_type, record_id, row)
    by_version = {v.version: v for v in stored}
    ordered = sorted(leaves, key=lambda leaf: (leaf.version, leaf.seq))
    seen: set[int] = set()
    for i, leaf in enumerate(ordered):
        is_current = i == len(ordered) - 1
        version = leaf.version
        mine = by_version.get(version)
        check = VersionCheck(record_type, record_id, version, VersionStatus.SUPERSEDED, leaf, mine)
        result.versions.append(check)
        if version in seen:
            check.status, check.reason = VersionStatus.MISMATCH, "two leaves for one version"
            continue
        seen.add(version)
        if (
            mine is None
            or mine.commitment.hex() != leaf.payload.get("commitment")
            or mine.audit_seq != leaf.seq
            or mine.content_schema != leaf.payload.get("content_schema")
        ):
            check.status = VersionStatus.MISMATCH
            check.reason = "the stored version does not match its audit leaf"
            continue
        if not is_current:
            continue
        if row is None:
            check.status, check.reason = VersionStatus.MISSING_ROW, "the record row is gone"
            continue
        try:
            content = content_of(record_type, row)
            recomputed = commitment(mine.salt, content)
        except Exception as exc:  # an unreadable row is a finding, not a server error
            check.status = VersionStatus.MISMATCH
            check.reason = f"the record cannot be read: {type(exc).__name__}"
            continue
        check.content = content
        if recomputed.hex() != leaf.payload.get("commitment"):
            check.status = VersionStatus.MISMATCH
            check.reason = "the record differs from its last committed version"
            continue
        check.status = VersionStatus.MATCH
    for version, mine in sorted(by_version.items()):
        if version not in seen:
            result.versions.append(
                VersionCheck(
                    record_type,
                    record_id,
                    version,
                    VersionStatus.MISMATCH,
                    None,
                    mine,
                    "a stored version has no audit leaf",
                )
            )
    return result


def verdict(checks: Sequence[RecordCheck], anchored: Callable[[int], bool]) -> Verdict:
    """One verdict for a set of records. `anchored(seq)` says whether that leaf is under a root
    confirmed on a chain."""
    statuses = [v.status for c in checks for v in c.versions]
    if VersionStatus.MISSING_ROW in statuses:
        return Verdict.MISSING_ROW
    if VersionStatus.MISMATCH in statuses:
        return Verdict.MISMATCH
    if any(c.missing_version for c in checks):
        return Verdict.MISSING_VERSION
    for c in checks:
        current = c.current
        if current is not None and current.leaf is not None and not anchored(current.leaf.seq):
            return Verdict.PENDING
    return Verdict.MATCH


# Loading ----------------------------------------------------------------------------------------


async def leaves_for(
    db: AsyncSession, clinic_id: uuid.UUID, record_ids: Iterable[uuid.UUID] | None = None
) -> list[Leaf]:
    """record_version leaves of the clinic, or of the given records only."""
    query = select(AuditEvent.seq, AuditEvent.payload).where(
        AuditEvent.clinic_id == clinic_id, AuditEvent.kind == LEAF_KIND
    )
    if record_ids is not None:
        ids = [str(r) for r in record_ids]
        if not ids:
            return []
        query = query.where(AuditEvent.payload["record_id"].astext.in_(ids))
    rows = (await db.execute(query.order_by(AuditEvent.seq))).all()
    return [Leaf(int(r.seq), dict(r.payload)) for r in rows]


async def stored_for(
    db: AsyncSession, clinic_id: uuid.UUID, record_ids: Iterable[uuid.UUID] | None = None
) -> list[RecordVersion]:
    query = select(RecordVersion).where(RecordVersion.clinic_id == clinic_id)
    if record_ids is not None:
        query = query.where(RecordVersion.record_id.in_(list(record_ids)))
    return list((await db.scalars(query.order_by(RecordVersion.version))).all())


def leaf_record(leaf: Leaf) -> tuple[RecordType, uuid.UUID] | None:
    try:
        return RecordType(leaf.payload["record_type"]), uuid.UUID(leaf.payload["record_id"])
    except (KeyError, ValueError):
        return None


@dataclass(frozen=True)
class ClinicRecordsCheck:
    records: int
    match: int
    pending: int
    mismatch: int
    missing_version: int
    missing_row: int
    backfilled: int


async def check_clinic(
    db: AsyncSession, clinic_id: uuid.UUID, anchored: Callable[[int], bool]
) -> ClinicRecordsCheck:
    """Every clinical record row of the clinic against its versions and leaves. One record per
    row: each note or prescription version row is its own record here."""
    leaves = await leaves_for(db, clinic_id)
    stored = await stored_for(db, clinic_id)
    rows: dict[tuple[RecordType, uuid.UUID], Any] = {}
    for record_type, model in MODELS.items():
        for row in (await db.scalars(select(model).where(model.clinic_id == clinic_id))).all():
            rows[(record_type, row.id)] = row
    leaves_by: dict[tuple[RecordType, uuid.UUID], list[Leaf]] = {}
    for leaf in leaves:
        key = leaf_record(leaf)
        if key is not None:
            leaves_by.setdefault(key, []).append(leaf)
    stored_by: dict[tuple[RecordType, uuid.UUID], list[RecordVersion]] = {}
    for version in stored:
        stored_by.setdefault((RecordType(version.record_type), version.record_id), []).append(
            version
        )
    counts = dict.fromkeys(Verdict, 0)
    total = 0
    for key in set(rows) | set(leaves_by) | set(stored_by):
        record_type, record_id = key
        check = check_record(
            record_type, record_id, rows.get(key), stored_by.get(key, []), leaves_by.get(key, [])
        )
        if not check.versions and not check.needs_version:
            continue  # a draft note or prescription: not part of the record yet
        total += 1
        counts[verdict([check], anchored)] += 1
    return ClinicRecordsCheck(
        records=total,
        match=counts[Verdict.MATCH],
        pending=counts[Verdict.PENDING],
        mismatch=counts[Verdict.MISMATCH],
        missing_version=counts[Verdict.MISSING_VERSION],
        missing_row=counts[Verdict.MISSING_ROW],
        backfilled=sum(1 for v in stored if v.backfilled),
    )


async def anchored_size(db: AsyncSession, clinic_id: uuid.UUID) -> int:
    """Tree size of the clinic's largest checkpoint confirmed on a chain (0 if none)."""
    size = await db.scalar(
        select(func.max(MerkleCheckpoint.tree_size))
        .join(AnchorReceipt, AnchorReceipt.checkpoint_id == MerkleCheckpoint.id)
        .where(
            MerkleCheckpoint.clinic_id == clinic_id,
            AnchorReceipt.backend.in_(CHAIN_BACKENDS),
            AnchorReceipt.status == AnchorStatus.CONFIRMED,
        )
    )
    return int(size or 0)
