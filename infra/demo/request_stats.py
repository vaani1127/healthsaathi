"""Request timings from the API's JSON log (.demo/logs/api.log in native mode).

    uv run python infra/demo/request_stats.py .demo/logs/api.log --since 2026-10-13T11:00

Prints the count, p50, p95 and max duration of all requests in the window, then the same per
endpoint (ids in paths folded to {id}), slowest p50 first. Used for the timing numbers in
docs/PROGRESS.md and README.md.
"""

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

ID = re.compile(r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))]


def line(name: str, values: list[float]) -> str:
    return (
        f"{name:48s} n={len(values):5d}  p50={pct(values, 0.5):7.2f} s"
        f"  p95={pct(values, 0.95):7.2f} s  max={max(values):7.2f} s"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("log", type=Path)
    parser.add_argument("--since", default="")
    parser.add_argument("--until", default="9999")
    parser.add_argument("--top", type=int, default=15)
    args = parser.parse_args()

    every: list[float] = []
    by_endpoint: dict[str, list[float]] = defaultdict(list)
    for raw in args.log.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if record.get("logger") != "app.request" or "duration_ms" not in record:
            continue
        if not args.since <= record["ts"] < args.until:
            continue
        seconds = record["duration_ms"] / 1000
        every.append(seconds)
        by_endpoint[f"{record['method']} {ID.sub('/{id}', record['path'])}"].append(seconds)
    if not every:
        raise SystemExit("no requests in that window")
    print(line("all requests", every))
    ranked = sorted(by_endpoint.items(), key=lambda kv: -pct(kv[1], 0.5))
    for name, values in ranked[: args.top]:
        print(line(name, values))


if __name__ == "__main__":
    main()
