"""Inventory for the full app audit (docs/AUDIT_REPORT.md).

Lists every API route with its auth check and the runs that called it, every web screen, every
worker loop and every make target, as Markdown tables.

    uv run python infra/audit/inventory.py \\
        --pytest-routes <file written by make test with HS_ROUTE_LOG=<file>> \\
        --e2e-log <output of make e2e with HS_ROUTE_LOG set> \\
        --demo-log <docker compose logs api, covering make demo-e2e> \\
        --out audit-artifacts/inventory.md

"Called" comes from real runs: the API tests record the matched route template of every request
(apps/api/tests/conftest.py, HS_ROUTE_LOG); the e2e and demo runs are read from the API's own
request log (method and path of every request the browser sent), mapped to route templates.
A route that no run called is "not tested".
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
API_PREFIX = "/api/v1"


def make_target_names(makefile_text: str) -> list[str]:
    """Every target a Makefile defines, including several names on one line and continued lines."""
    joined = re.sub(r"\\\r?\n", " ", makefile_text)
    names: list[str] = []
    for line in joined.splitlines():
        match = re.match(r"^([a-z][a-z0-9\s%-]*?)\s*:(?!=)", line)
        if match:
            names += [n for n in match.group(1).split() if "%" not in n]
    return list(dict.fromkeys(names))


def _app() -> Any:
    for name, value in {
        "JWT_SECRET": "j" * 40,
        "OTP_HMAC_KEY": "o" * 40,
        "DATA_KEYS": "t1:" + "A" * 43 + "=",
        "LEDGER_SIGNING_SEED": "l" * 40,
        "DATABASE_URL": "postgresql+asyncpg://audit:audit@localhost/audit",
    }.items():
        os.environ.setdefault(name, value)
    sys.path.insert(0, str(ROOT / "apps" / "api"))
    from app.main import create_app

    return create_app()


def _auth(route: Any) -> str:
    """The auth checks a route depends on, from its FastAPI dependency tree."""
    found: list[str] = []

    def walk(dependant: Any) -> None:
        for dep in dependant.dependencies:
            call = dep.call
            name = getattr(call, "__name__", type(call).__name__)
            if name == "dependency" and call.__closure__:
                for cell in call.__closure__:
                    if isinstance(cell.cell_contents, frozenset):
                        roles = sorted(str(r.value) for r in cell.cell_contents)
                        found.append("roles: " + ", ".join(roles))
            elif name in {
                "get_principal",
                "get_clinic_principal",
                "require_patient_data_access",
            }:
                found.append(name)
            walk(dep)

    walk(route.dependant)
    labels = {
        "get_principal": "signed in",
        "get_clinic_principal": "clinic token",
        "require_patient_data_access": "patient data gate",
    }
    out: list[str] = []
    for item in found:
        label = labels.get(item, item)
        if label not in out:
            out.append(label)
    return "; ".join(out) or "public"


def api_routes() -> list[dict[str, str]]:
    app = _app()
    rows: list[dict[str, str]] = []

    def add(route: Any, prefix: str) -> None:
        methods = sorted(getattr(route, "methods", None) or {"WS"})
        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            rows.append(
                {
                    "method": method,
                    "path": prefix + route.path,
                    "auth": _auth(route) if hasattr(route, "dependant") else "public",
                    "name": getattr(route, "name", ""),
                }
            )

    for route in app.routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            for sub in inner.routes:
                add(sub, API_PREFIX)
        elif getattr(route, "path", "").startswith(API_PREFIX):
            add(route, "")
    return sorted(rows, key=lambda r: (r["path"], r["method"]))


def _template_regex(path: str) -> re.Pattern[str]:
    return re.compile(
        "^"
        + re.sub(r"\{[^/]+\}", "[^/]+", re.escape(path).replace(r"\{", "{").replace(r"\}", "}"))
        + "$"
    )


def map_paths(
    seen: Iterable[tuple[str, str]], routes: list[dict[str, str]]
) -> set[tuple[str, str]]:
    """Concrete (method, path) pairs mapped to (method, route template)."""
    patterns = [(r["method"], r["path"], _template_regex(r["path"])) for r in routes]
    # Literal segments win over parameters, like the router.
    patterns.sort(key=lambda p: p[1].count("{"))
    out: set[tuple[str, str]] = set()
    for method, path in seen:
        path = path.split("?", 1)[0]
        for m, template, regex in patterns:
            if m == method and regex.match(path):
                out.add((m, template))
                break
    return out


def from_pytest(file: Path | None) -> set[tuple[str, str]]:
    if file is None or not file.exists():
        return set()
    pairs = json.loads(file.read_text(encoding="utf-8"))
    return {(m, p if p.startswith(API_PREFIX) else API_PREFIX + p) for m, p in pairs}


REQUEST_LINE = re.compile(r'\{.*"msg": "request".*\}')


def from_request_log(file: Path | None) -> set[tuple[str, str]]:
    if file is None or not file.exists():
        return set()
    seen: set[tuple[str, str]] = set()
    for line in file.read_text(encoding="utf-8", errors="replace").splitlines():
        match = REQUEST_LINE.search(line)
        if not match:
            continue
        try:
            entry = json.loads(match.group(0))
        except ValueError:
            continue
        if entry.get("path", "").startswith(API_PREFIX):
            seen.add((entry.get("method", ""), entry["path"]))
    return seen


def screens() -> list[dict[str, str]]:
    source = (ROOT / "apps/web/src/router.tsx").read_text(encoding="utf-8")
    rows = []
    for match in re.finditer(r"createRoute\(\{(.*?)\}\)", source, re.S):
        body = match.group(1)
        path = re.search(r'path:\s*"([^"]+)"', body)
        component = re.search(
            r'component:\s*(?:lazyRouteComponent\([^,]+,\s*"(\w+)"\)|(\w+))', body
        )
        if path:
            name = (component.group(1) or component.group(2)) if component else ""
            rows.append({"path": path.group(1), "component": name})
    return rows


def tabs() -> list[dict[str, str]]:
    rows = []
    for file in sorted((ROOT / "apps/web/src/features").rglob("*.tsx")):
        text = file.read_text(encoding="utf-8")
        for match in re.finditer(r"const (\w*TABS\w*) = \[([^\]]+)\]", text):
            for tab in re.findall(r'"(\w+)"', match.group(2)):
                rows.append({"file": str(file.relative_to(ROOT)).replace("\\", "/"), "tab": tab})
    return rows


def worker_loops() -> list[dict[str, str]]:
    rows = []
    for rel in ("apps/api/app/worker.py", "apps/api/app/startup.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        for match in re.finditer(r"^async def (\w+)\(", text, re.M):
            name = match.group(1)
            if name.endswith(("_loop", "_tick", "_run")) or name in {"run_startup_tasks", "main"}:
                tests = sorted(
                    {
                        str(p.relative_to(ROOT)).replace("\\", "/")
                        for p in (ROOT / "apps/api/tests").rglob("test_*.py")
                        if re.search(
                            r"worker\.main\b" if name == "main" else rf"\b{name}\b",
                            p.read_text(encoding="utf-8"),
                        )
                    }
                )
                rows.append(
                    {"file": rel, "function": name, "tests": ", ".join(tests) or "not tested"}
                )
    return rows


def make_targets() -> list[dict[str, str]]:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    help_lines = dict(re.findall(r'@echo "([a-z][\w-]*)\s+(.*?)"', text))
    return [{"target": t, "help": help_lines.get(t, "")} for t in make_target_names(text)]


def table(rows: list[dict[str, str]], columns: list[str]) -> str:
    out = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for row in rows:
        out.append(
            "| " + " | ".join(str(row.get(c, "")).replace("|", "\\|") for c in columns) + " |"
        )
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pytest-routes", type=Path)
    parser.add_argument("--e2e-log", type=Path)
    parser.add_argument("--demo-log", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    routes = api_routes()
    called = {
        "API tests": from_pytest(args.pytest_routes),
        "e2e": map_paths(from_request_log(args.e2e_log), routes),
        "demo suite": map_paths(from_request_log(args.demo_log), routes),
    }
    for row in routes:
        key = (row["method"], row["path"])
        runs = [name for name, pairs in called.items() if key in pairs]
        row["called by"] = ", ".join(runs) or "**not tested**"
    untested = [r for r in routes if r["called by"] == "**not tested**"]

    lines = [
        "# Inventory",
        "",
        f"Generated by `infra/audit/inventory.py`. API routes: {len(routes)}; "
        f"called in no run: {len(untested)}. Requests seen per run: "
        + ", ".join(f"{name} {len(pairs)} route templates" for name, pairs in called.items())
        + ".",
        "",
        "## API routes",
        "",
        table(routes, ["method", "path", "auth", "called by"]),
        "",
        "## Screens (web routes)",
        "",
        table(screens(), ["path", "component"]),
        "",
        "## Tabs",
        "",
        table(tabs(), ["file", "tab"]),
        "",
        "## Worker and startup jobs",
        "",
        table(worker_loops(), ["file", "function", "tests"]),
        "",
        "## Make targets",
        "",
        table(make_targets(), ["target", "help"]),
        "",
    ]
    text = "\n".join(lines)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}: {len(routes)} routes, {len(untested)} not tested")
    else:
        print(text)


if __name__ == "__main__":
    main()
