"""Every appointment inside the run is resolved (follow-ups in whichever session they fall in,
and forged appointments like real ones), and appointments after the run stay booked and are kept
out of in-run counts."""

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from saathibench.config import NO_SHOW_MARKED_AFTER
from saathibench.realism import measure

SMALL = Path(__file__).resolve().parents[1] / "configs" / "small.yaml"
RESOLVED = ("completed", "no_show", "cancelled")


def table(run: Path, name: str) -> pl.DataFrame:
    return pl.concat([pl.read_parquet(f) for f in (run / "tables" / name).glob("*.parquet")])


@pytest.fixture
def run(forgery_run: Path) -> Path:
    # Attacks on, many of them forgery, so forged appointments are checked too.
    return forgery_run


def run_end(run: Path) -> datetime:
    start = datetime.combine(date(2026, 1, 5), time.min, ZoneInfo("Asia/Kolkata"))
    return (start + timedelta(days=42)).astimezone(UTC)


def test_no_appointment_inside_the_run_stays_booked(run: Path) -> None:
    appointments = table(run, "appointments").filter(pl.col("slot_end") < run_end(run))
    assert appointments.height > 0
    stuck = appointments.filter(~pl.col("status").is_in(RESOLVED))
    assert stuck.is_empty(), stuck.group_by("kind", "source").len()


def test_forged_appointments_are_resolved_like_real_ones(run: Path) -> None:
    forgers = pl.concat(
        [pl.read_parquet(f) for f in (run / "labels" / "campaigns").glob("*.parquet")]
    ).filter((pl.col("attack_type") == 10) & (pl.col("variant") == "appointment"))
    assert forgers.height > 0, "the run has appointment forgeries"
    forged = (
        table(run, "appointments")
        .join(
            forgers.select(pl.col("actor_user_id").alias("created_by")), on="created_by", how="semi"
        )
        .filter((pl.col("source") != "patient") & (pl.col("slot_end") < run_end(run)))
    )
    assert forged.height > 0
    assert forged["status"].is_in(["no_show", "cancelled", "completed"]).all()
    for status, slot, updated in forged.select("status", "slot_start", "updated_at").iter_rows():
        if status == "no_show":
            assert (updated - slot).total_seconds() == pytest.approx(NO_SHOW_MARKED_AFTER * 60)


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


def test_followup_chain_statistics(run: Path) -> None:
    chains = measure(run)["followups"]
    assert chains["visits"] > 0 and chains["visits_per_patient"] > 0
    assert chains["visits_per_patient_seen"] >= chains["visits_per_patient"]
    assert chains["chains"] > 0
    assert sum(chains["chain_lengths"].values()) == chains["chains"]
    assert sum(chains["chain_ends"].values()) == chains["chains"]
    assert chains["longest_chain"] >= 1


def test_forgery_flag_auc_report(run: Path) -> None:
    from e2d_experiments.prereg_audit import FLAG_SIGNALS, forgery_flag_auc

    report = forgery_flag_auc(run, 0)
    assert report["forged_accesses"] > 0 and report["benign_sampled"] > 0
    assert set(FLAG_SIGNALS) == set(report["auc"])
    assert all(0.0 <= report["auc"][f] <= 1.0 for f in FLAG_SIGNALS)
    assert set(report["fire_rate_type10"]) == set(report["fire_rate_benign"])
    assert all(0.0 <= v <= 1.0 for v in report["fire_rate_benign"].values())
