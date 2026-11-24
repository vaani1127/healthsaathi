"""Plain summary of one study period of a live snapshot, for the snapshot report.

    uv run python -m e2d_experiments.anchoring.summary            # clean period, after `enrich`
    uv run python -m e2d_experiments.anchoring.summary --period stalled

Writes `<snapshot_dir>/../summary_<period>.json` and prints it. It reuses the study's own
functions (analysis.py, cadence.py), so its rewrite windows match `anchoring_windows.tex`. On top
of the tables it gives: anchors per clinic and backend, gas per clinic, every gap between two
anchors of a clinic and backend longer than `--gap-minutes` (30 by default), and how many events
came after `--days` (14 by default) from the period start. Counts and times only.
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
from omegaconf import OmegaConf

from e2d_experiments.anchoring import analysis, cadence

ROOT = Path(__file__).resolve().parents[4]
CONFIG = ROOT / "experiments" / "configs" / "anchoring.yaml"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _num(value: Any) -> Any:
    return round(float(value), 2) if value is not None else None


def summarise(
    events: pl.DataFrame,
    checkpoints: pl.DataFrame,
    receipts: pl.DataFrame,
    period: cadence.Period,
    taken_at: datetime,
    tz: str,
    sessions: dict[int, list[Any]],
    *,
    gap_minutes: float = 30.0,
    days: int = 14,
) -> dict[str, Any]:
    end = period.end or taken_at
    periods = [period]
    in_period = cadence.with_period(events, "at", periods).filter(pl.col("period").is_not_null())
    windows = cadence.with_period(
        analysis.event_windows(events, checkpoints, receipts), "at", periods
    ).filter(pl.col("period").is_not_null())
    sent = cadence.with_period(receipts, "submitted_at", periods).filter(
        pl.col("period").is_not_null()
    )

    anchors = (
        sent.group_by("clinic_id", "backend")
        .agg(
            pl.len().alias("anchors"),
            (pl.col("status") == "confirmed").sum().alias("confirmed"),
            pl.col("submitted_at").min().alias("first"),
            pl.col("submitted_at").max().alias("last"),
        )
        .sort("clinic_id", "backend")
    )
    statuses = sent.group_by("backend", "status").agg(pl.len().alias("n")).sort("backend", "status")
    gas = (
        sent.filter((pl.col("status") == "confirmed") & pl.col("gas_used").is_not_null())
        .group_by("clinic_id", "backend")
        .agg(
            pl.len().alias("transactions"),
            pl.col("gas_used").sum().alias("gas_total"),
            pl.col("gas_used").median().alias("gas_median"),
            pl.col("gas_used").min().alias("gas_min"),
            pl.col("gas_used").max().alias("gas_max"),
            (pl.col("gas_used") * pl.col("gas_price_wei") / 1e18).sum().alias("fee_total"),
        )
        .sort("clinic_id", "backend")
    )
    gaps = cadence.anchor_gaps(receipts, periods, tz, sessions).filter(
        pl.col("period").is_not_null() & (pl.col("gap_min") > gap_minutes)
    )
    window_rows = analysis.window_summary(windows.drop("period"))
    mark = period.start + timedelta(days=days)

    return {
        "period": period.name,
        "start": _iso(period.start),
        "end": _iso(end),
        "end_is_snapshot_time": period.end is None,
        "first_event": _iso(in_period["at"].min()),  # type: ignore[arg-type]
        "last_event": _iso(in_period["at"].max()),  # type: ignore[arg-type]
        "events": in_period.height,
        "events_per_clinic": {
            r["clinic_id"]: r["len"]
            for r in in_period.group_by("clinic_id").len().sort("clinic_id").to_dicts()
        },
        f"events_after_{days}_days": int((in_period["at"] >= mark).sum()),
        f"{days}_day_mark": _iso(mark),
        "anchors": [
            {**r, "first": _iso(r["first"]), "last": _iso(r["last"])} for r in anchors.to_dicts()
        ],
        "anchor_status": statuses.to_dicts(),
        "rewrite_window_min": [
            {k: (_num(v) if k.endswith("_min") else v) for k, v in r.items()}
            for r in window_rows.to_dicts()
        ],
        "gas": [{**r, "gas_median": _num(r["gas_median"])} for r in gas.to_dicts()],
        "gas_note": "fee_total is in the chain's native coin (test POL on Amoy)",
        f"gaps_over_{gap_minutes:g}_min": [
            {
                "clinic_id": r["clinic_id"],
                "backend": r["backend"],
                "anchor_at": _iso(r["at"]),
                "gap_min": _num(r["gap_min"]),
                "inside_one_opd_session": r["same_session"],
            }
            for r in gaps.sort("clinic_id", "backend", "at").to_dicts()
        ],
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m e2d_experiments.anchoring.summary")
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--period", default="clean")
    parser.add_argument("--gap-minutes", type=float, default=30.0)
    parser.add_argument("--days", type=int, default=14)
    args = parser.parse_args(argv)
    cfg = OmegaConf.load(args.config)
    folder = ROOT / str(cfg.snapshot_dir)
    period = next(p for p in cadence.periods_from_config(cfg.periods) if p.name == args.period)
    meta = dict(
        line.split("=", 1)
        for line in (folder / "snapshot.txt").read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    data = summarise(
        pl.read_parquet(folder / "events.parquet"),
        pl.read_parquet(folder / "checkpoints.parquet"),
        pl.read_parquet(folder / "enriched.parquet"),
        period,
        datetime.fromisoformat(meta["taken_at"]),
        str(cfg.clinic_timezone),
        cadence.sessions_from_config(cfg.opd_sessions),
        gap_minutes=args.gap_minutes,
        days=args.days,
    )
    data["snapshot_taken_at"] = meta["taken_at"]
    out = folder.parent / f"summary_{args.period}.json"
    out.write_text(json.dumps(data, indent=1, default=str) + "\n", encoding="utf-8")
    print(json.dumps(data, indent=1, default=str))
    print(f"wrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
