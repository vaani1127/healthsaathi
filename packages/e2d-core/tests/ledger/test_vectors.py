import json
from pathlib import Path

from e2d_core.ledger import verify, verify_consistency, verify_inclusion
from e2d_core.ledger.sth import TreeHead
from e2d_core.ledger.vectors import build_vectors, main, render

VECTORS_FILE = (
    Path(__file__).resolve().parents[3] / "receipt-verify" / "test" / "vectors" / "ledger.json"
)


def test_committed_vectors_are_up_to_date() -> None:
    assert VECTORS_FILE.read_text(encoding="utf-8") == render(), (
        "run: uv run python -m e2d_core.ledger.vectors "
        "packages/receipt-verify/test/vectors/ledger.json"
    )


def test_python_agrees_with_every_vector() -> None:
    v = build_vectors()
    for case in v["merkle"]["inclusion"]:
        ok = verify_inclusion(
            bytes.fromhex(case["leaf_hash"]),
            case["index"],
            case["size"],
            [bytes.fromhex(p) for p in case["proof"]],
            bytes.fromhex(case["root"]),
        )
        assert ok is case["valid"]
    for case in v["merkle"]["consistency"]:
        ok = verify_consistency(
            case["old_size"],
            case["new_size"],
            bytes.fromhex(case["old_root"]),
            bytes.fromhex(case["new_root"]),
            [bytes.fromhex(p) for p in case["proof"]],
        )
        assert ok is case["valid"]
    public_key = bytes.fromhex(v["sth"]["public_key"])
    for case in v["sth"]["heads"]:
        head = TreeHead.from_json(case["head"])
        assert verify(head, bytes.fromhex(case["signature"]), public_key) is case["valid"]


def test_cli_writes_file(tmp_path: Path) -> None:
    out = tmp_path / "v.json"
    assert main(["vectors", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["sth"]["key_id"]
    assert main(["vectors"]) == 2
