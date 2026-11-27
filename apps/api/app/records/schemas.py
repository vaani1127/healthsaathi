import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.anchor.proofs import AnchorRef
from app.db.enums import RecordType

VerdictName = Literal["match", "pending", "mismatch", "missing_version", "missing_row"]
VersionStatusName = Literal["match", "superseded", "mismatch", "missing_row"]


class VersionProof(BaseModel):
    """One committed version and everything needed to check it without trusting the server:
    recompute SHA-256(salt || JCS(content)), compare it with the leaf's commitment, hash the leaf
    (SHA-256(0x00 || SHA-256(JCS(payload)))), check the inclusion proof to the signed tree head,
    the head's signature, and the anchors."""

    record_id: uuid.UUID
    version: int
    status: VersionStatusName
    reason: str | None
    backfilled: bool = Field(
        description="Added for a record that existed before record versions. It shows the record "
        "has not changed since committed_at, not before."
    )
    committed_at: datetime | None
    content_schema: str | None
    content: dict[str, Any] | None = Field(
        description="The record as it is now, in the content schema. Only for the newest version "
        "of a record whose row still exists."
    )
    salt: str | None
    commitment: str | None
    audit_seq: int | None
    payload: dict[str, Any] | None
    payload_hash: str | None
    leaf_hash: str | None
    leaf_index: int | None
    proof: list[str] | None
    sth: dict[str, Any] | None
    signature: str | None
    anchored: bool = Field(description="The tree head is confirmed on a chain.")
    anchors: list[AnchorRef]


class RecordIntegrity(BaseModel):
    version: int = 1
    record_type: RecordType
    record_id: uuid.UUID
    patient_id: uuid.UUID
    verdict: VerdictName
    reason: str | None
    covered_since: datetime | None = Field(
        description="When the first version of this record was committed. Nothing before this "
        "time is covered."
    )
    backfilled: bool = Field(
        description="At least one version was backfilled; see covered_since and each version."
    )
    clinic_id: uuid.UUID
    clinic_id_bytes32: str
    public_key: str
    versions: list[VersionProof]
