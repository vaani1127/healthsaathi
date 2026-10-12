"""Every problem code the API can raise has an English and a Hindi message in the web app."""

import json
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
LOCALES = Path(__file__).resolve().parents[2] / "web" / "src" / "locales"
CODE = re.compile(r'ProblemError\(\s*\d+,\s*"([a-z0-9-]+)"|\bcode(?:: str)? ?= ?"([a-z0-9-]+)"')


def raised_codes() -> set[str]:
    codes: set[str] = set()
    for path in APP.rglob("*.py"):
        for match in CODE.finditer(path.read_text(encoding="utf-8")):
            codes.add(match.group(1) or match.group(2))
    return codes


def test_scan_finds_codes() -> None:
    assert {"access-denied", "slot-taken", "weak-password"} <= raised_codes()


def test_every_problem_code_is_translated() -> None:
    for lang in ("en", "hi"):
        errors = json.loads((LOCALES / f"{lang}.json").read_text(encoding="utf-8"))["errors"]
        missing = sorted(raised_codes() - errors.keys())
        assert not missing, f"add errors.<code> to {lang}.json for: {missing}"
