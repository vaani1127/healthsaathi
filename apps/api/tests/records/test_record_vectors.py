import hashlib
import json
from pathlib import Path

from app.records.schemas import RecordIntegrity
from app.scripts.record_vectors import build_vectors, main, render
from e2d_core.ledger import TreeHead, verify, verify_inclusion
from e2d_core.ledger.jcs import canonicalize

ROOT = Path(__file__).resolve().parents[4]
RECORD_VECTORS = ROOT / "packages/receipt-verify/test/record-vectors.json"
LEDGER_VECTORS = ROOT / "packages/receipt-verify/test/vectors/ledger.json"
# sha256 of ledger.json at main 61692ec: the e2d-core vectors must stay byte-identical.
LEDGER_SHA256 = "1d9dc104510a8579abd0d96692681aabd29dfea181eaf5e244d9ad7e6a7a5a7f"


def test_committed_record_vectors_are_up_to_date() -> None:
    assert RECORD_VECTORS.read_text(encoding="utf-8") == render(), "run: make record-vectors"


def test_ledger_vectors_are_unchanged() -> None:
    assert hashlib.sha256(LEDGER_VECTORS.read_bytes()).hexdigest() == LEDGER_SHA256


def test_vector_has_the_api_response_shape() -> None:
    vector = build_vectors()["record_check"]
    assert RecordIntegrity.model_validate(vector).model_dump(mode="json") == vector


def test_python_agrees_with_the_vectors() -> None:
    v = build_vectors()
    for case in v["commitment"]:
        digest = hashlib.sha256(bytes.fromhex(case["salt"]) + canonicalize(case["content"]))
        assert digest.hexdigest() == case["commitment"]
    response = v["record_check"]
    for version in response["versions"]:
        head = TreeHead.from_json(version["sth"])
        assert verify(
            head, bytes.fromhex(version["signature"]), bytes.fromhex(response["public_key"])
        )
        assert verify_inclusion(
            bytes.fromhex(version["leaf_hash"]),
            version["leaf_index"],
            head.tree_size,
            [bytes.fromhex(p) for p in version["proof"]],
            bytes.fromhex(head.root_hex),
        )
    newest = response["versions"][-1]
    digest = hashlib.sha256(bytes.fromhex(newest["salt"]) + canonicalize(newest["content"]))
    assert digest.hexdigest() == newest["payload"]["commitment"]


def test_cli_writes_file(tmp_path: Path) -> None:
    out = tmp_path / "v.json"
    assert main(["record_vectors", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["record_check"]["versions"]
    assert main(["record_vectors"]) == 2
