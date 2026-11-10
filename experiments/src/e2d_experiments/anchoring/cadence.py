"""Anchoring cadence on the live demo, by study period (pure functions on a snapshot).

    uv run python -m e2d_experiments.anchoring cadence     # after the snapshot step

From the snapshot's receipts (counts and times only, no payloads): anchors per clinic, backend
and day in clinic time, and the gaps between consecutive anchors of each clinic and backend, for
all pairs and for pairs inside one OPD session. Each row carries the study period (configs/
anchoring.yaml) of its later anchor, so a stalled timer shows up as its own period instead of
being mixed into the design figures. The events and windows are split the same way in report.py.
"""

from dataclasses import dataclass
from datetime import date, datetime, time
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

# The demo activity bot's sessions (configs/anchoring.yaml, opd_sessions), used when none are given.
DEMO_SESSIONS = {"weekdays": ["09:30-13:30", "17:00-20:00"], "sunday": ["09:30-12:30"]}
GAP_COLUMNS = ("period", "backend", "scope", "gaps", "min_min", "median_min", "max_min")


@dataclass(frozen=True)
class Period:
    name: str
    label: str
    start: datetime
    end: datetime | None


def periods_from_config(items: Any) -> list[Period]:
    return [
        Period(
            str(p["name"]),
            str(p["label"]),
            datetime.fromisoformat(str(p["start"])),
            datetime.fromisoformat(str(p["end"])) if p["end"] is not None else None,
        )
        for p in items
    ]


def with_period(frame: pl.DataFrame, column: str, periods: list[Period]) -> pl.DataFrame:
    """Adds `period` (a period name, null outside every period) from the time in `column`."""
    expr: pl.Expr = pl.lit(None, dtype=pl.String)
    for p in reversed(periods):
        inside = pl.col(column) >= p.start
        if p.end is not None:
            inside = inside & (pl.col(column) < p.end)
        expr = pl.when(inside).then(pl.lit(p.name)).otherwise(expr)
    return frame.with_columns(expr.alias("period"))


def sessions_from_config(items: Any) -> dict[int, list[tuple[time, time]]]:
    """Weekday (Monday 0) to its OPD sessions."""

    def parse(spans: Any) -> list[tuple[time, time]]:
        out = []
        for span in spans:
            start, end = str(span).split("-")
            out.append((time.fromisoformat(start), time.fromisoformat(end)))
        return out

    weekdays = parse(items["weekdays"])
    return {**{d: weekdays for d in range(6)}, 6: parse(items["sunday"])}


def session_key(
    at: datetime, tz: ZoneInfo, sessions: dict[int, list[tuple[time, time]]]
) -> tuple[date, int] | None:
    local = at.astimezone(tz)
    for i, (start, end) in enumerate(sessions[local.weekday()]):
        if start <= local.time() < end:
            return (local.date(), i)
    return None


def anchors_per_day(receipts: pl.DataFrame, tz: str, periods: list[Period]) -> pl.DataFrame:
    """clinic_id, backend, day (clinic time), period of the day's anchors, anchors."""
    return (
        with_period(receipts, "submitted_at", periods)
        .with_columns(pl.col("submitted_at").dt.convert_time_zone(tz).dt.date().alias("day"))
        .group_by("clinic_id", "backend", "day", "period")
        .agg(pl.len().alias("anchors"))
        .sort("clinic_id", "backend", "day", "period")
    )


def anchor_gaps(
    receipts: pl.DataFrame,
    periods: list[Period],
    tz: str,
    sessions: dict[int, list[tuple[time, time]]],
) -> pl.DataFrame:
    """One row per pair of consecutive anchors of a clinic and backend: the gap in minutes, the
    period of the later anchor, and whether both fall inside the same OPD session."""
    zone = ZoneInfo(tz)
    rows = []
    for (clinic, backend), group in receipts.sort("submitted_at").group_by(
        "clinic_id", "backend", maintain_order=True
    ):
        times = group["submitted_at"].to_list()
        for earlier, later in pairwise(times):
            key = session_key(later, zone, sessions)
            rows.append(
                {
                    "clinic_id": clinic,
                    "backend": backend,
                    "at": later,
                    "gap_min": (later - earlier).total_seconds() / 60,
                    "same_session": key is not None and key == session_key(earlier, zone, sessions),
                }
            )
    schema: dict[str, Any] = {
        "clinic_id": pl.String,
        "backend": pl.String,
        "at": pl.Datetime("us", "UTC"),
        "gap_min": pl.Float64,
        "same_session": pl.Boolean,
    }
    return with_period(pl.DataFrame(rows, schema=schema, orient="row"), "at", periods)


def gap_summary(gaps: pl.DataFrame) -> pl.DataFrame:
    """Per period and backend, all clinics together: gaps for all pairs and inside one session."""

    def summarise(frame: pl.DataFrame, scope: str) -> pl.DataFrame:
        return (
            frame.filter(pl.col("period").is_not_null())
            .group_by("period", "backend")
            .agg(
                pl.len().alias("gaps"),
                pl.col("gap_min").min().alias("min_min"),
                pl.col("gap_min").median().alias("median_min"),
                pl.col("gap_min").max().alias("max_min"),
            )
            .with_columns(pl.lit(scope).alias("scope"))
        )

    return (
        pl.concat(
            [
                summarise(gaps, "all"),
                summarise(gaps.filter(pl.col("same_session")), "inside one OPD session"),
            ]
        )
        .sort("period", "backend", "scope")
        .select(GAP_COLUMNS)
    )


def daily_summary(per_day: pl.DataFrame) -> pl.DataFrame:
    """Per period and backend: clinic-days with anchors and the median anchors per clinic-day."""
    return (
        per_day.filter(pl.col("period").is_not_null())
        .group_by("period", "backend")
        .agg(
            pl.len().alias("clinic_days"),
            pl.col("anchors").median().alias("median_per_day"),
            pl.col("anchors").min().alias("min_per_day"),
            pl.col("anchors").max().alias("max_per_day"),
        )
        .sort("period", "backend")
    )
