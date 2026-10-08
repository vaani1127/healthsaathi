from pathlib import Path

from app.scripts.export_openapi import render

COMMITTED = Path(__file__).resolve().parents[2] / "web" / "src" / "lib" / "api" / "openapi.json"


def test_web_client_schema_is_up_to_date() -> None:
    assert COMMITTED.read_text(encoding="utf-8") == render(), "run: make api-types"
