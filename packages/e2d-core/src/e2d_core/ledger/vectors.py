"""Shared test vectors for the TypeScript receipt verifier.

Run `python -m e2d_core.ledger.vectors <output.json>` to regenerate. The output is deterministic
(fixed seed, fixed timestamps, Ed25519 signatures are deterministic), and a Python test checks that
the committed file matches, so both implementations are tested against the same data.
"""

import json
import sys
import uuid
from pathlib import Path
from typing import Any

from e2d_core.ledger.hashchain import payload_hash
from e2d_core.ledger.jcs import canonicalize
from e2d_core.ledger.merkle import MerkleTree, leaf_hash
from e2d_core.ledger.sth import (
    TreeHead,
    derive_clinic_key,
    key_id,
    public_key_bytes,
    sign,
)

SEED = b"healthsaathi-test-vector-seed-0001"
CLINIC_ID = uuid.UUID("01920000-0000-7000-8000-00000000c001")
TIMESTAMP = 1_790_000_000_000

JCS_INPUTS = [
    '{"b":2,"a":1}',
    '{"numbers":[333333333.33333329,1E30,4.50,2e-3,0.000000000000000000000000001]}',
    '{"string":"\\u20ac$\\u000F\\u000aA\'\\u0042\\u0022\\u005c\\\\\\"\\/"}',
    '{"literals":[null,true,false],"nested":{"z":[],"y":{}}}',
    '{"\\u20ac":1,"\\r":2,"\\ufb33":3,"1":4,"\\ud83d\\ude00":5,"\\u0080":6,"\\u00f6":7}',
    "[1e21,1e20,-0,5e-324,1.7976931348623157e308,0.000001,1e-7,123456789]",
    '{"payload":{"kind":"access","patient":"p-1","at":"2026-10-08T05:00:00Z"}}',
]


def _hex(items: list[bytes]) -> list[str]:
    return [b.hex() for b in items]


def _access_payload(i: int) -> dict[str, Any]:
    """Shaped like the audit payload the API writes for an access (synthetic ids only)."""
    return {
        "type": "access",
        "access_event_id": f"01920000-0000-7000-8000-{i:012x}",
        "at": f"2026-10-01T09:{i:02d}:00+00:00",
        "user_id": "01920000-0000-7000-8000-0000000000d1",
        "role": "doctor",
        "patient_id": "01920000-0000-7000-8000-0000000000e1",
        "resource": "notes",
        "action": "view",
        "decision": "allow",
        "policy_version": "1",
        "template": "T1_APPOINTMENT" if i % 2 == 0 else None,
        "strength": 0.731058 if i % 2 == 0 else 0,
    }


def _api_receipt(key: Any, public_key: bytes) -> dict[str, Any]:
    """A receipt exactly as GET /access-events/{id}/receipt returns it."""
    payloads = [_access_payload(i) for i in range(6)]
    hashes = [payload_hash(p) for p in payloads]
    tree = MerkleTree([leaf_hash(h) for h in hashes])
    head = TreeHead(
        clinic_id=str(CLINIC_ID),
        tree_size=tree.size,
        root_hex=tree.root().hex(),
        prev_root_hex=None,
        timestamp=TIMESTAMP,
        key_id=key_id(public_key),
    )
    index = 4
    return {
        "version": 1,
        "clinic_id": str(CLINIC_ID),
        "clinic_id_bytes32": "0x" + (CLINIC_ID.bytes + bytes(16)).hex(),
        "public_key": public_key.hex(),
        "audit_seq": 100 + index,
        "payload": payloads[index],
        "payload_hash": hashes[index].hex(),
        "leaf_hash": leaf_hash(hashes[index]).hex(),
        "leaf_index": index,
        "proof": _hex(tree.inclusion_proof(index)),
        "sth": head.to_json(),
        "signature": sign(head, key).hex(),
        "sth_digest": head.digest().hex(),
        "anchors": [],
    }


def build_vectors() -> dict[str, Any]:
    data = [f"audit-event-{i}".encode() for i in range(13)]
    tree = MerkleTree.from_data(data)
    n = tree.size

    inclusion: list[dict[str, Any]] = []
    for size in (1, 2, 5, 8, 13):
        for index in sorted({0, size // 2, size - 1}):
            inclusion.append(
                {
                    "index": index,
                    "size": size,
                    "leaf_hash": tree.levels[0][index].hex(),
                    "proof": _hex(tree.inclusion_proof(index, size)),
                    "root": tree.root(size).hex(),
                    "valid": True,
                }
            )
    good = inclusion[-1]
    inclusion.append({**good, "index": good["index"] - 1, "valid": False})
    inclusion.append({**good, "root": tree.root(n - 1).hex(), "valid": False})
    inclusion.append({**good, "proof": good["proof"][:-1], "valid": False})

    consistency: list[dict[str, Any]] = []
    for old, new in ((1, 2), (3, 7), (4, 8), (5, 13), (8, 13), (13, 13)):
        consistency.append(
            {
                "old_size": old,
                "new_size": new,
                "old_root": tree.root(old).hex(),
                "new_root": tree.root(new).hex(),
                "proof": _hex(tree.consistency_proof(old, new)),
                "valid": True,
            }
        )
    c = consistency[3]
    consistency.append({**c, "old_root": tree.root(4).hex(), "valid": False})

    key = derive_clinic_key(SEED, CLINIC_ID)
    public_key = public_key_bytes(key)
    heads: list[dict[str, Any]] = []
    for size, prev in ((8, None), (13, tree.root(8).hex())):
        head = TreeHead(
            clinic_id=str(CLINIC_ID),
            tree_size=size,
            root_hex=tree.root(size).hex(),
            prev_root_hex=prev,
            timestamp=TIMESTAMP + size,
            key_id=key_id(public_key),
        )
        heads.append(
            {
                "head": head.to_json(),
                "signing_bytes": head.signing_bytes().decode(),
                "digest": head.digest().hex(),
                "signature": sign(head, key).hex(),
                "valid": True,
            }
        )
    tampered: dict[str, Any] = dict(heads[1])
    tampered["head"] = {**heads[1]["head"], "tree_size": 12}
    tampered["valid"] = False
    heads.append(tampered)

    receipt_index = 6
    receipt = {
        "leaf_data": data[receipt_index].hex(),
        "leaf_hash": leaf_hash(data[receipt_index]).hex(),
        "leaf_index": receipt_index,
        "proof": _hex(tree.inclusion_proof(receipt_index, 13)),
        "sth": heads[1]["head"],
        "signature": heads[1]["signature"],
    }

    return {
        "comment": "Generated by python -m e2d_core.ledger.vectors. Do not edit by hand.",
        "jcs": [
            {"input": raw, "canonical": canonicalize(json.loads(raw)).decode()}
            for raw in JCS_INPUTS
        ],
        "merkle": {
            "leaf_data": _hex(data),
            "leaf_hashes": _hex(tree.levels[0]),
            "roots": [{"size": s, "root": tree.root(s).hex()} for s in range(0, n + 1)],
            "inclusion": inclusion,
            "consistency": consistency,
        },
        "sth": {
            "public_key": public_key.hex(),
            "key_id": key_id(public_key),
            "heads": heads,
        },
        "receipt": receipt,
        "api_receipt": _api_receipt(key, public_key),
    }


def render() -> str:
    return json.dumps(build_vectors(), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m e2d_core.ledger.vectors <output.json>")
        return 2
    Path(argv[1]).write_text(render(), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
