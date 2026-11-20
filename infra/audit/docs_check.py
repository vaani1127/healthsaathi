"""Docs truth check helper for the full app audit (docs/AUDIT_REPORT.md).

For each guide, lists the quoted UI names ("Register", "Send now", ...) and whether each one is a
text the web app really shows (a value in apps/web/src/locales/en.json, exactly or as part of
one), and the make targets and file paths the guide names that do not exist. Everything it
reports is then checked by hand; a quoted phrase can also be an example, not a UI name.

    uv run python infra/audit/docs_check.py docs/USER_GUIDE.md docs/DEMO_SCRIPT.md README.md
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def make_target_names(makefile_text: str) -> list[str]:
    """Every target a Makefile defines, including several names on one line and continued lines."""
    joined = re.sub(r"\\\r?\n", " ", makefile_text)
    names: list[str] = []
    for line in joined.splitlines():
        match = re.match(r"^([a-z][a-z0-9\s%-]*?)\s*:(?!=)", line)
        if match:
            names += [n for n in match.group(1).split() if "%" not in n]
    return list(dict.fromkeys(names))


def values(node: object) -> list[str]:
    if isinstance(node, dict):
        return [v for child in node.values() for v in values(child)]
    return [str(node)] if isinstance(node, str) else []


def main() -> None:
    texts = values(json.loads((ROOT / "apps/web/src/locales/en.json").read_text(encoding="utf-8")))
    plain = [re.sub(r"\{\{\w+\}\}", "", t) for t in texts]
    make = set(make_target_names((ROOT / "Makefile").read_text(encoding="utf-8")))
    for arg in sys.argv[1:]:
        doc = ROOT / arg
        print(f"## {arg}")
        for number, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), start=1):
            for quoted in re.findall(r'"([^"]{2,80})"', line):
                if quoted.startswith(("http", "/", ".")) or "=" in quoted:
                    continue
                exact = quoted in texts
                inside = any(quoted in t for t in plain)
                if not (exact or inside):
                    print(f'{arg}:{number}: quoted UI text not in en.json: "{quoted}"')
            for target in re.findall(r"`make ([a-z][a-z0-9-]*)", line):
                if target not in make:
                    print(f"{arg}:{number}: make target does not exist: {target}")
            roots = "apps|packages|infra|docs|contracts|sim|experiments"
            for path in re.findall(rf"`((?:{roots})/[^`\s]+)`", line):
                clean = path.split(":")[0].rstrip(".,")
                if "<" in clean or "*" in clean:
                    continue
                if not (ROOT / clean).exists():
                    print(f"{arg}:{number}: path does not exist: {clean}")
        print()


if __name__ == "__main__":
    main()
