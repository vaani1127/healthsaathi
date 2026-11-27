"""Shared test vectors for record integrity checks in the browser (ADR 0011).

    uv run python -m app.scripts.record_vectors packages/receipt-verify/test/record-vectors.json
    (or: make record-vectors)

Deterministic (fixed salts, ids, times and seed; Ed25519 signatures are deterministic). A Python
test checks that the committed file matches, and that the response has the exact shape of
GET /api/v1/records/{record_type}/{record_id}/integrity, so receipt-verify is tested against what
the API returns. The ledger vectors of e2d-core are separate and unchanged.
"""

import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.anchor.evm import clinic_bytes32
from app.db.enums import RecordType
from app.ledger.records import commitment, content_of, leaf_payload
from e2d_core.ledger import (
    MerkleTree,
    TreeHead,
    derive_clinic_key,
    key_id,
    leaf_hash,
    payload_hash,
    public_key_bytes,
    sign,
)

SEED = b"healthsaathi-record-vector-seed-01"
CLINIC_ID = uuid.UUID("01920000-0000-7000-8000-00000000c002")
PATIENT_ID = uuid.UUID("01920000-0000-7000-8000-0000000000a2")
LAB_USER = uuid.UUID("01920000-0000-7000-8000-0000000000b3")
RESULT_ID = uuid.UUID("01920000-0000-7000-8000-0000000000d4")
ORDER_ID = uuid.UUID("01920000-0000-7000-8000-0000000000e5")
V1_ID = uuid.UUID("01920000-0000-7000-8000-0000000000f6")
V2_ID = uuid.UUID("01920000-0000-7000-8000-0000000000f7")
RESULTED_AT = datetime(2026, 11, 20, 5, 0, 0, 123456, tzinfo=UTC)
RELEASED_AT = datetime(2026, 11, 20, 6, 30, 0, 654321, tzinfo=UTC)
SIGNED_AT_MS = 1_795_000_000_000

COMMITMENT_CASES: list[tuple[str, dict[str, Any]]] = [
    ("00" * 32, {}),
    ("11" * 32, {"b": 2, "a": "x"}),
    ("ab" * 32, {"text": "नमस्ते", "n": 1.5, "items": [1, True, None]}),
]


def _row(released_at: datetime | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=RESULT_ID,
        lab_order_id=ORDER_ID,
        patient_id=PATIENT_ID,
        values={"hb": {"value": 13.1, "unit": "g/dL"}},
        document_id=None,
        resulted_by=LAB_USER,
        resulted_at=RESULTED_AT,
        released_at=released_at,
    )


def _api_time(value: datetime) -> str:
    """A UTC time as the API's JSON writes it."""
    return value.isoformat().replace("+00:00", "Z")


def _filler(i: int) -> dict[str, Any]:
    return {"type": "access", "access_event_id": f"01920000-0000-7000-8000-{i:012d}", "n": i}


def build_vectors() -> dict[str, Any]:
    key = derive_clinic_key(SEED, CLINIC_ID)
    public_key = public_key_bytes(key)
    salts = [bytes([0x21]) * 32, bytes([0x42]) * 32]
    contents = [
        content_of(RecordType.LAB_RESULT, _row(None)),
        content_of(RecordType.LAB_RESULT, _row(RELEASED_AT)),
    ]
    digests = [commitment(s, c) for s, c in zip(salts, contents, strict=True)]
    leaves = [
        leaf_payload(
            RecordType.LAB_RESULT,
            RESULT_ID,
            1,
            None,
            "lab_result/v1",
            digests[0],
            LAB_USER,
            RESULTED_AT,
        ),
        leaf_payload(
            RecordType.LAB_RESULT,
            RESULT_ID,
            2,
            V1_ID,
            "lab_result/v1",
            digests[1],
            LAB_USER,
            RELEASED_AT,
        ),
    ]
    payloads: list[dict[str, Any]] = [_filler(0), _filler(1), leaves[0], _filler(3), _filler(4)]
    payloads += [leaves[1], _filler(6), _filler(7)]
    positions = {1: 2, 2: 5}
    tree = MerkleTree([leaf_hash(payload_hash(p)) for p in payloads])
    head = TreeHead(
        clinic_id=str(CLINIC_ID),
        tree_size=tree.size,
        root_hex=tree.root().hex(),
        prev_root_hex=None,
        timestamp=SIGNED_AT_MS,
        key_id=key_id(public_key),
    )
    signature = sign(head, key).hex()
    anchors = [
        {
            "backend": "amoy",
            "status": "confirmed",
            "tx_ref": "0x" + "5a" * 32,
            "block_ref": "123456",
            "link": None,
        }
    ]
    versions = []
    for version, payload in zip((1, 2), leaves, strict=True):
        index = positions[version]
        current = version == 2
        digest = payload_hash(payload)
        versions.append(
            {
                "record_id": str(RESULT_ID),
                "version": version,
                "status": "match" if current else "superseded",
                "reason": None,
                "backfilled": False,
                "committed_at": _api_time(RELEASED_AT if current else RESULTED_AT),
                "content_schema": "lab_result/v1",
                "content": contents[1] if current else None,
                "salt": salts[version - 1].hex(),
                "commitment": payload["commitment"],
                "audit_seq": 1000 + index,
                "payload": payload,
                "payload_hash": digest.hex(),
                "leaf_hash": leaf_hash(digest).hex(),
                "leaf_index": index,
                "proof": [p.hex() for p in tree.inclusion_proof(index)],
                "sth": head.to_json(),
                "signature": signature,
                "anchored": True,
                "anchors": anchors,
            }
        )
    response = {
        "version": 1,
        "record_type": "lab_result",
        "record_id": str(RESULT_ID),
        "patient_id": str(PATIENT_ID),
        "verdict": "match",
        "reason": None,
        "covered_since": _api_time(RESULTED_AT),
        "backfilled": False,
        "clinic_id": str(CLINIC_ID),
        "clinic_id_bytes32": "0x" + clinic_bytes32(CLINIC_ID).hex(),
        "public_key": public_key.hex(),
        "versions": versions,
    }
    return {
        "comment": "Generated by python -m app.scripts.record_vectors. Do not edit by hand.",
        "commitment": [
            {
                "salt": salt,
                "content": content,
                "commitment": commitment(bytes.fromhex(salt), content).hex(),
            }
            for salt, content in COMMITMENT_CASES
        ],
        "record_check": response,
        "sth_digest": "0x" + head.digest().hex(),
    }


def render() -> str:
    return json.dumps(build_vectors(), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m app.scripts.record_vectors <output.json>")
        return 2
    Path(argv[1]).write_text(render(), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
