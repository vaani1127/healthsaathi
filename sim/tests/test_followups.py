"""Follow-up appointments booked by doctors are visited in whichever session they fall in, and
appointments after the run stay booked and are kept out of in-run counts."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from saathibench.config import AttackConfig, load_config
from saathibench.realism import measure
from saathibench.run import simulate

SMALL = Path(__file__).resolve().parents[1] / "configs" / "small.yaml"
RESOLVED = ("completed", "no_show", "cancelled")


def table(run: Path, name: str) -> pl.DataFrame:
    return pl.concat([pl.read_parquet(f) for f in (run / "tables" / name).glob("*.parquet")])


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    # No attacks: forged appointments of attack type 10 stay booked on purpose.
    cfg = replace(
        load_config(SMALL),
        days=42,
        start_date=date(2026, 1, 5),
        attacks=AttackConfig(campaigns_per_clinic_month=0),
    )
    out = tmp_path_factory.mktemp("followups") / "run"
    simulate(cfg, out, workers=1)
    return out


def run_end(run: Path) -> datetime:
    start = datetime.combine(date(2026, 1, 5), time.min, ZoneInfo("Asia/Kolkata"))
    return (start + timedelta(days=42)).astimezone(UTC)


def test_no_appointment_inside_the_run_stays_booked(run: Path) -> None:
    appointments = table(run, "appointments").filter(
        (pl.col("kind") != "walkin") & (pl.col("slot_end") < run_end(run))
    )
    assert appointments.height > 0
    stuck = appointments.filter(~pl.col("status").is_in(RESOLVED))
    assert stuck.is_empty(), stuck.group_by("kind", "source").len()


def test_evening_followups_are_seen_in_the_evening(run: Path) -> None:
    tz = ZoneInfo("Asia/Kolkata")
    followups = table(run, "appointments").filter(
        (pl.col("source") == "doctor")
        & (pl.col("slot_end") < run_end(run))
        & (pl.col("slot_start").dt.convert_time_zone("Asia/Kolkata").dt.hour() >= 16)
    )
    assert followups.height > 0, "the run has follow-ups booked into evening sessions"
    assert followups["status"].is_in(RESOLVED).all()
    seen = followups.filter(pl.col("status") == "completed").join(
        table(run, "encounters").select(pl.col("appointment_id").alias("id"), "started_at"),
        on="id",
    )
    assert seen.height > 0
    for slot, started in seen.select("slot_start", "started_at").iter_rows():
        slot_local, started_local = slot.astimezone(tz), started.astimezone(tz)
        assert started_local.date() == slot_local.date()
        assert started_local.hour >= 16, "seen in the evening session, not the morning"


def test_appointments_after_the_run_stay_booked_and_are_counted_apart(run: Path) -> None:
    appointments = table(run, "appointments")
    after = appointments.filter(pl.col("slot_start") >= run_end(run))
    assert after.height > 0 and (after["status"] == "booked").all()
    report = measure(run)
    assert report["appointments_after_run"] == after.height
    in_run = appointments.filter(pl.col("slot_start") < run_end(run))
    assert report["appointments_in_run"] == in_run.height
    booked = in_run.filter(pl.col("kind") != "walkin")
    assert report["non_walk_in_appointments_in_run"] == booked.height
    lost = booked.filter(pl.col("status").is_in(["no_show", "cancelled"])).height
    assert report["no_show_or_cancelled_share"] == pytest.approx(lost / booked.height)
