"""Explanation coverage of a simulator run, per role and per benign scenario.

Runs the e2d-core explanation engine (in-memory backend) on a sample of each clinic's access
events and prints the share explained at or above the gate theta. Numbers come only from this
script; do not copy them into docs by hand.

    uv run python -m saathibench.coverage sim/output/saathibench-small --per-role 300
"""

import argparse
import json
import random
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from e2d_core.explain import AccessEvent, EvidenceQuery, default_config, explain
from e2d_core.repo.memory import COLUMNS, MemoryRepository


@dataclass
class Tally:
    n: int = 0
    explained: int = 0
    strength: float = 0.0
    templates: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def add(self, template: str | None, strength: float, theta: float) -> None:
        self.n += 1
        self.strength += strength
        if strength >= theta:
            self.explained += 1
        self.templates[template or "none"] += 1

    def row(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "coverage": round(self.explained / self.n, 4) if self.n else None,
            "mean_strength": round(self.strength / self.n, 4) if self.n else None,
            "templates": dict(sorted(self.templates.items(), key=lambda kv: -kv[1])),
        }


def clinic_keys(run: Path) -> list[str]:
    return sorted(p.stem.split("-")[0] for p in (run / "tables" / "clinics").glob("*.parquet"))


def read(run: Path, folder: str, table: str, key: str) -> pl.DataFrame | None:
    files = sorted((run / folder / table).glob(f"{key}-*.parquet"))
    return pl.concat([pl.read_parquet(f) for f in files]) if files else None


def clinic_frames(run: Path, key: str) -> dict[str, pl.DataFrame]:
    frames = {}
    for table, columns in COLUMNS.items():
        frame = read(run, "tables", table, key)
        if frame is not None:
            frames[table] = frame.select(columns)
    return frames


def sample_events(events: pl.DataFrame, per_role: int, rng: random.Random) -> pl.DataFrame:
    parts = []
    for role in sorted(events["role"].unique().to_list()):
        of_role = events.filter(pl.col("role") == role)
        if of_role.height > per_role:
            of_role = of_role.sample(n=per_role, seed=rng.randrange(2**31))
        parts.append(of_role)
    return pl.concat(parts)


def coverage(run: Path, per_role: int, seed: int = 0) -> dict[str, Any]:
    cfg = default_config()
    rng = random.Random(seed)
    by_role: dict[str, Tally] = defaultdict(Tally)
    by_scenario: dict[str, Tally] = defaultdict(Tally)
    timezone = json.loads((run / "manifest.json").read_text(encoding="utf-8"))["timezone"]
    started = time.perf_counter()
    explained_events = 0
    for key in clinic_keys(run):
        events = read(run, "tables", "access_events", key)
        if events is None:
            continue
        events = sample_events(events.filter(pl.col("decision") == "allow"), per_role, rng)
        scenarios = read(run, "labels", "benign_scenarios", key)
        tags: dict[str, str] = (
            dict(zip(scenarios["access_event_id"], scenarios["scenario"], strict=True))
            if scenarios is not None
            else {}
        )
        repo = MemoryRepository(clinic_frames(run, key))
        for row in events.iter_rows(named=True):
            event = AccessEvent(
                id=uuid.UUID(row["id"]),
                clinic_id=uuid.UUID(row["clinic_id"]),
                user_id=uuid.UUID(row["user_id"]),
                role=row["role"],
                patient_id=uuid.UUID(row["patient_id"]),
                resource=row["resource_type"],
                action=row["action"],
                at=row["at"],
                break_glass_id=uuid.UUID(row["break_glass_id"]) if row["break_glass_id"] else None,
            )
            query = EvidenceQuery(
                event.clinic_id, event.user_id, event.patient_id, event.at, timezone, event.role
            )
            result = explain(event, repo.evidence_for_sync(query), cfg, timezone)
            by_role[event.role].add(result.template_code, result.strength, cfg.theta)
            scenario = tags.get(row["id"])
            if scenario is not None:
                by_scenario[scenario].add(result.template_code, result.strength, cfg.theta)
            explained_events += 1
    return {
        "run": str(run),
        "theta": cfg.theta,
        "per_role_sample_per_clinic": per_role,
        "events_explained": explained_events,
        "seconds": round(time.perf_counter() - started, 1),
        "by_role": {k: v.row() for k, v in sorted(by_role.items())},
        "by_scenario": {k: v.row() for k, v in sorted(by_scenario.items())},
    }


def print_report(report: dict[str, Any]) -> None:
    print(
        f"{report['events_explained']} sampled accesses in {report['seconds']} s, "
        f"theta {report['theta']}"
    )
    for title, key in (("role", "by_role"), ("benign scenario", "by_scenario")):
        print(f"\n{title:24} {'n':>7} {'coverage':>9} {'strength':>9}  top templates")
        for name, row in report[key].items():
            top = ", ".join(f"{t} {n}" for t, n in list(row["templates"].items())[:3])
            numbers = f"{row['n']:>7} {row['coverage']:>9.3f} {row['mean_strength']:>9.3f}"
            print(f"{name:24} {numbers}  {top}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Explanation coverage of a SaathiBench run.")
    parser.add_argument("run", type=Path)
    parser.add_argument("--per-role", type=int, default=300, help="sample per role per clinic")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--json", type=Path, help="also write the report here")
    args = parser.parse_args(argv)
    report = coverage(args.run, args.per_role, args.seed)
    print_report(report)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
