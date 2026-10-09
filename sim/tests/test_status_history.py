"""The status history (status_events) agrees with the rows, lab orders are resolved, forged lab
orders are made by lab technicians, and antenatal patients registered during the run plan their
visits."""

import json
import random
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from saathibench.clinic import ClinicSim
from saathibench.config import load_config
from saathibench.policy import load_policy

SMALL = Path(__file__).resolve().parents[1] / "configs" / "small.yaml"

ENTITIES = {
    "appointments": ("appointment", "booked"),
    "lab_orders": ("lab_order", "ordered"),
    "referrals": ("referral", "active"),
    "invoices": ("invoice", "issued"),
}


def table(run: Path, name: str, folder: str = "tables") -> pl.DataFrame:
    return pl.concat([pl.read_parquet(f) for f in (run / folder / name).glob("*.parquet")])


def run_end(run: Path) -> datetime:
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    start = datetime.combine(
        date.fromisoformat(manifest["start_date"]), time.min, ZoneInfo(manifest["timezone"])
    )
    return (start + timedelta(days=manifest["days"])).astimezone(UTC)


def history(run: Path, entity: str) -> pl.DataFrame:
    """Statuses of each entity in time order, as a list per entity."""
    return (
        table(run, "status_events")
        .filter(pl.col("entity_type") == entity)
        .sort("entity_id", "at", maintain_order=True)
        .group_by("entity_id", maintain_order=True)
        .agg(pl.col("status").alias("statuses"), pl.col("at").alias("times"))
    )


@pytest.mark.parametrize("name", list(ENTITIES))
def test_every_row_has_a_history_that_ends_in_its_status(forgery_run: Path, name: str) -> None:
    entity, first = ENTITIES[name]
    rows = table(forgery_run, name).select("id", "status", "created_at")
    assert rows.height > 0
    joined = rows.join(history(forgery_run, entity), left_on="id", right_on="entity_id")
    assert joined.height == rows.height, "every row has a history"
    for status, created, statuses, times in joined.select(
        "status", "created_at", "statuses", "times"
    ).iter_rows():
        assert statuses[0] == first and times[0] == created, "the first status is at creation"
        assert statuses[-1] == status, "the last status is the row's status"


def test_histories_with_several_transitions(forgery_run: Path) -> None:
    visits = history(forgery_run, "appointment")["statuses"].to_list()
    assert ["booked", "checked_in", "in_consult", "completed"] in visits
    assert ["booked", "no_show"] in visits and ["booked", "cancelled"] in visits
    orders = history(forgery_run, "lab_order")["statuses"].to_list()
    assert ["ordered", "collected", "resulted"] in orders
    assert ["ordered", "cancelled"] in orders
    invoices = history(forgery_run, "invoice")["statuses"].to_list()
    assert ["issued", "paid"] in invoices


def test_lab_orders_inside_the_run_are_resolved(forgery_run: Path) -> None:
    orders = table(forgery_run, "lab_orders").filter(
        pl.col("created_at") < run_end(forgery_run) - timedelta(days=3)
    )
    assert orders.height > 0
    assert (orders["status"] != "ordered").all()
    # Uncollected orders are cancelled the same day and never get a result.
    uncollected = orders.filter(
        pl.col("encounter_id").is_not_null() & (pl.col("status") == "cancelled")
    )
    assert uncollected.height > 0
    results = table(forgery_run, "lab_results").select(pl.col("lab_order_id").alias("id"))
    assert uncollected.join(results, on="id", how="semi").is_empty()
    tz = ZoneInfo("Asia/Kolkata")
    for created, updated in uncollected.select("created_at", "updated_at").iter_rows():
        assert updated >= created
        assert updated.astimezone(tz).date() == created.astimezone(tz).date()


def test_forged_lab_orders_are_made_by_lab_technicians(forgery_run: Path) -> None:
    forgers = table(forgery_run, "campaigns", "labels").filter(
        (pl.col("attack_type") == 10) & (pl.col("variant") == "lab_order")
    )
    assert forgers.height > 0, "the run has lab order forgeries"
    assert (forgers["actor_role"] == "lab_tech").all()
    forged = table(forgery_run, "lab_orders").join(
        forgers.select(pl.col("actor_user_id").alias("ordered_by")), on="ordered_by", how="semi"
    )
    assert forged.height > 0
    assert forged["encounter_id"].is_null().all()
    assert (forged["status"] == "cancelled").all()
    assert (forged["updated_at"] > forged["created_at"]).all()


def test_antenatal_patients_registered_during_the_run_plan_visits(tmp_path: Path) -> None:
    cfg = load_config(SMALL)
    sim = ClinicSim(cfg.clinics[2], cfg, tmp_path, load_policy())
    sim.setup()
    sim.rng = random.Random(3)
    at = sim.epoch.astimezone(UTC) + timedelta(days=2)
    clerk = sim.staff["reception"][0]
    antenatal = [p for p in (sim.new_patient(at, clerk) for _ in range(400)) if p.kind == "anc"]
    assert antenatal, "some new patients are antenatal"
    assert all(2 <= p.anc_left <= 7 for p in antenatal)
