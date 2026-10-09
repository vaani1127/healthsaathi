"""Realism measures of a run, from the generated tables and labels only (never the explanation
engine), for the pre-registration's realism table.

- hard_negative_share: share of benign staff accesses that belong to a benign hard-negative
  scenario (labels/benign_scenarios).
- no_show_or_cancelled_share: share of appointments that ended as no-show or cancelled.
- clinician_booked_share: share of appointments created by a doctor or nurse (source "doctor").
- incidental_snooping_share: share of snooping accesses (types 1, 2 and 6) whose patient has a
  booked, non-cancelled appointment on the same local day.

    uv run python -m saathibench.realism sim/output/saathibench-v1
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import polars as pl

from saathibench.labels import attack_labels, read_labels

SNOOPING_TYPES = (1, 2, 6)


def _scan(run: Path, table: str) -> pl.LazyFrame:
    return pl.scan_parquet(str(run / "tables" / table / "*.parquet"))


def measure(run: Path) -> dict[str, Any]:
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    tz = manifest["timezone"]
    events = (
        _scan(run, "access_events")
        .filter(pl.col("role") != "patient")
        .select("id", "patient_id", "at")
        .collect()
    )
    labels = attack_labels(run).select("access_event_id", "is_attack", "attack_type")
    scenarios = read_labels(run, "benign_scenarios").select("access_event_id").unique()
    staff = events.join(labels, left_on="id", right_on="access_event_id", how="left")
    benign = staff.filter(~pl.col("is_attack"))
    tagged = benign.join(scenarios, left_on="id", right_on="access_event_id", how="semi").height

    appointments = _scan(run, "appointments").select("patient_id", "slot_start", "status", "source")
    counts = appointments.select(
        pl.len().alias("all"),
        pl.col("status").is_in(["no_show", "cancelled"]).sum().alias("lost"),
        (pl.col("source") == "doctor").sum().alias("clinician"),
    ).collect()
    booked_days = (
        appointments.filter(pl.col("status") != "cancelled")
        .select("patient_id", pl.col("slot_start").dt.convert_time_zone(tz).dt.date().alias("day"))
        .unique()
        .collect()
    )
    snooping = staff.filter(pl.col("attack_type").is_in(SNOOPING_TYPES)).with_columns(
        pl.col("at").dt.convert_time_zone(tz).dt.date().alias("day")
    )
    incidental = snooping.join(booked_days, on=["patient_id", "day"], how="semi").height
    total = int(counts["all"][0])
    return {
        "run": run.name,
        "hard_negative_share": tagged / benign.height if benign.height else None,
        "no_show_or_cancelled_share": int(counts["lost"][0]) / total if total else None,
        "clinician_booked_share": int(counts["clinician"][0]) / total if total else None,
        "incidental_snooping_share": incidental / snooping.height if snooping.height else None,
        "snooping_accesses": snooping.height,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Realism measures of a SaathiBench run.")
    parser.add_argument("run", type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(measure(args.run), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
