from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import polars as pl
from omegaconf import OmegaConf

from e2d_experiments.anchoring import cadence, report

ROOT = Path(__file__).resolve().parents[2]
TS = pl.Datetime("us", "UTC")
IST = "Asia/Kolkata"
# Wednesday 4 Nov 2026, 05:00 UTC = 10:30 IST, inside the morning session.
DAY = datetime(2026, 11, 4, 5, 0, tzinfo=UTC)
STALL = DAY + timedelta(hours=1)
FIX = DAY + timedelta(days=2)
PERIODS = [
    cadence.Period("timer-working", "Timer working", DAY - timedelta(days=1), STALL),
    cadence.Period("stalled", "Timer stalled", STALL, FIX),
    cadence.Period("clean", "After the fix", FIX, None),
]
SESSIONS = cadence.sessions_from_config(cadence.DEMO_SESSIONS)


def receipts(times: list[datetime], clinic: str = "c", backend: str = "amoy") -> pl.DataFrame:
    return pl.DataFrame(
        {
            "clinic_id": [clinic] * len(times),
            "backend": [backend] * len(times),
            "submitted_at": times,
        },
        schema={"clinic_id": pl.String, "backend": pl.String, "submitted_at": TS},
    )


def test_the_configured_periods_cover_the_study_without_gaps_or_overlaps() -> None:
    cfg = OmegaConf.load(ROOT / "experiments" / "configs" / "anchoring.yaml")
    periods = cadence.periods_from_config(cfg.periods)
    assert [p.name for p in periods] == ["timer-working", "stalled", "clean"]
    assert periods[0].start == datetime.fromisoformat(str(cfg.since))
    for earlier, later in pairwise(periods):
        assert earlier.end == later.start
    assert periods[1].start == datetime(2026, 11, 5, 6, 6, tzinfo=UTC)
    assert periods[2].start == datetime(2026, 11, 10, 18, 47, 10, tzinfo=UTC)
    assert periods[-1].end is None


def test_each_time_gets_the_period_it_falls_in() -> None:
    frame = receipts([DAY - timedelta(days=2), DAY, STALL, FIX - timedelta(seconds=1), FIX])
    labelled = cadence.with_period(frame, "submitted_at", PERIODS)["period"].to_list()
    assert labelled == [None, "timer-working", "stalled", "stalled", "clean"]


def test_anchors_are_counted_per_clinic_day_in_clinic_time() -> None:
    # 18:45 UTC is already the next day in India.
    frame = receipts([DAY, DAY + timedelta(minutes=15), DAY.replace(hour=18, minute=45)])
    per_day = cadence.anchors_per_day(frame, IST, PERIODS)
    assert per_day.select("day", "period", "anchors").rows() == [
        (DAY.date(), "timer-working", 2),
        ((DAY + timedelta(days=1)).date(), "stalled", 1),
    ]


def test_gaps_are_split_by_period_and_by_opd_session() -> None:
    minute = timedelta(minutes=1)
    times = [
        DAY,
        DAY + 15 * minute,
        DAY + 30 * minute,
        # 16:00 IST, after the morning session: a pair that is not inside one session.
        DAY + timedelta(hours=5, minutes=30),
        # Stalled: three hours later, inside the evening session.
        DAY + timedelta(hours=8, minutes=30),
    ]
    gaps = cadence.anchor_gaps(receipts(times), PERIODS, IST, SESSIONS)
    assert gaps["gap_min"].to_list() == [15.0, 15.0, 300.0, 180.0]
    assert gaps["same_session"].to_list() == [True, True, False, False]
    assert gaps["period"].to_list() == ["timer-working"] * 2 + ["stalled"] * 2
    summary = {
        (r["period"], r["scope"]): r
        for r in cadence.gap_summary(gaps).to_dicts()
        if r["backend"] == "amoy"
    }
    assert summary[("timer-working", "inside one OPD session")]["median_min"] == 15.0
    assert summary[("stalled", "all")]["max_min"] == 300.0
    assert ("stalled", "inside one OPD session") not in summary


def test_gaps_are_per_clinic_and_backend() -> None:
    frame = pl.concat(
        [receipts([DAY, DAY + timedelta(minutes=15)]), receipts([DAY + timedelta(minutes=5)], "d")]
    )
    gaps = cadence.anchor_gaps(frame, PERIODS, IST, SESSIONS)
    assert gaps.select("clinic_id", "gap_min").rows() == [("c", 15.0)]


def test_report_splits_windows_and_adds_cadence_by_period(tmp_path: Path) -> None:
    ts: dict[str, Any] = {"clinic_id": pl.String, "idx": pl.Int64, "at": TS}
    events = pl.DataFrame(
        {"clinic_id": ["c", "c"], "idx": [0, 1], "at": [DAY, STALL + timedelta(minutes=5)]},
        schema=ts,
    )
    checkpoints = pl.DataFrame(
        {
            "checkpoint_id": ["k1", "k2"],
            "clinic_id": ["c", "c"],
            "tree_size": [1, 2],
            "signed_at": [DAY + timedelta(minutes=10), STALL + timedelta(hours=3)],
        },
        schema={
            "checkpoint_id": pl.String,
            "clinic_id": pl.String,
            "tree_size": pl.Int64,
            "signed_at": TS,
        },
    )
    receipt_rows = pl.DataFrame(
        {
            "checkpoint_id": ["k1", "k2"],
            "clinic_id": ["c", "c"],
            "backend": ["amoy", "amoy"],
            "status": ["confirmed", "confirmed"],
            "submitted_at": [DAY + timedelta(minutes=10), STALL + timedelta(hours=3)],
            "confirmed_at": [DAY + timedelta(minutes=11), STALL + timedelta(hours=3, minutes=1)],
            "final_at": [DAY + timedelta(minutes=11), STALL + timedelta(hours=3, minutes=1)],
            "gas_used": [50_000, 50_000],
            "gas_price_wei": [30 * 10**9, 30 * 10**9],
        },
        schema={
            "checkpoint_id": pl.String,
            "clinic_id": pl.String,
            "backend": pl.String,
            "status": pl.String,
            "submitted_at": TS,
            "confirmed_at": TS,
            "final_at": TS,
            "gas_used": pl.Int64,
            "gas_price_wei": pl.Int64,
        },
    )
    snap = tmp_path / "live"
    snap.mkdir()
    events.write_parquet(snap / "events.parquet")
    checkpoints.write_parquet(snap / "checkpoints.parquet")
    receipt_rows.write_parquet(snap / "enriched.parquet")
    report.write(snap, tmp_path / "tables", tmp_path / "figures", [1.0], PERIODS, IST, SESSIONS)

    windows = (tmp_path / "tables" / "anchoring_windows.tex").read_text(encoding="utf-8")
    rows = [line for line in windows.splitlines() if "Polygon Amoy" in line]
    assert rows[0].startswith("Timer working & Polygon Amoy & 1 & 0 & 11.0")
    assert rows[1].startswith("Timer stalled & Polygon Amoy & 1 & 0 & 176.0")
    assert "After the fix" not in windows, "a period without events has no row"
    parts = (tmp_path / "tables" / "anchoring_parts.tex").read_text(encoding="utf-8")
    assert "Timer working & Polygon Amoy" in parts and "Timer stalled & Polygon Amoy" in parts
    cadence_tex = (tmp_path / "tables" / "anchoring_cadence.tex").read_text(encoding="utf-8")
    assert "Timer working & Polygon Amoy & 1 & 1.0" in cadence_tex
    assert "Timer stalled & Polygon Amoy & 1 & 1.0 & 230.0 & 230.0 & -- & --" in cadence_tex
