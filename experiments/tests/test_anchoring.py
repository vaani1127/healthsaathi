from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from e2d_experiments.anchoring import analysis, bench, collect, report

T0 = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
TS = pl.Datetime("us", "UTC")


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def frames() -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    # Clinic c: leaves 0-3. Checkpoint k1 covers 0-1, k2 covers 0-3.
    events = pl.DataFrame(
        {"clinic_id": ["c"] * 4, "idx": [0, 1, 2, 3], "at": [at(0), at(5), at(20), at(40)]},
        schema={"clinic_id": pl.String, "idx": pl.Int64, "at": TS},
    )
    checkpoints = pl.DataFrame(
        {
            "checkpoint_id": ["k1", "k2"],
            "clinic_id": ["c", "c"],
            "tree_size": [2, 4],
            "signed_at": [at(15.5), at(45.25)],
        },
        schema={
            "checkpoint_id": pl.String,
            "clinic_id": pl.String,
            "tree_size": pl.Int64,
            "signed_at": TS,
        },
    )
    receipts = pl.DataFrame(
        {
            "checkpoint_id": ["k1", "k2", "k1", "k2"],
            "backend": ["amoy", "amoy", "github", "github"],
            # The amoy anchor of k1 failed: its events are covered by k2.
            "status": ["failed", "confirmed", "confirmed", "pending"],
            "confirmed_at": [None, at(46), at(16), None],
            "final_at": [None, at(45.75), None, None],
            "gas_used": [None, 50_000, None, None],
            "gas_price_wei": [None, 30 * 10**9, None, None],
        },
        schema={
            "checkpoint_id": pl.String,
            "backend": pl.String,
            "status": pl.String,
            "confirmed_at": TS,
            "final_at": TS,
            "gas_used": pl.Int64,
            "gas_price_wei": pl.Int64,
        },
    )
    return events, checkpoints, receipts


def windows_by(backend: str) -> dict[int, float | None]:
    w = analysis.event_windows(*frames())
    rows = w.filter(pl.col("backend") == backend).sort("idx")
    return dict(zip(rows["idx"].to_list(), rows["window_s"].to_list(), strict=True))


def test_window_runs_to_the_first_final_record_that_covers_the_event() -> None:
    # Amoy: k1 failed, so every event waits for k2's block at minute 45.75.
    assert windows_by("amoy") == {0: 45.75 * 60, 1: 40.75 * 60, 2: 25.75 * 60, 3: 5.75 * 60}


def test_events_after_the_last_final_record_are_censored() -> None:
    # GitHub confirmed only k1 (at minute 16, the commit time); k2 is still pending.
    assert windows_by("github") == {0: 16 * 60, 1: 11 * 60, 2: None, 3: None}
    summary = analysis.window_summary(analysis.event_windows(*frames()))
    github = summary.filter(pl.col("backend") == "github").to_dicts()[0]
    assert (github["events"], github["censored"]) == (4, 2)


def test_a_later_checkpoint_that_finalised_first_wins() -> None:
    events, checkpoints, receipts = frames()
    # k1 on amoy confirmed after all, but later than k2: the window still ends at k2's block.
    late = receipts.with_columns(
        pl.when((pl.col("backend") == "amoy") & (pl.col("checkpoint_id") == "k1"))
        .then(pl.lit("confirmed"))
        .otherwise(pl.col("status"))
        .alias("status"),
        pl.when((pl.col("backend") == "amoy") & (pl.col("checkpoint_id") == "k1"))
        .then(pl.lit(at(90)).cast(TS))
        .otherwise(pl.col("final_at"))
        .alias("final_at"),
    )
    w = analysis.event_windows(events, checkpoints, late).filter(pl.col("backend") == "amoy")
    assert w.sort("idx")["window_s"][0] == 45.75 * 60


def test_parts_split_job_delay_and_finality() -> None:
    _, checkpoints, receipts = frames()
    parts = analysis.checkpoint_parts(checkpoints, receipts).sort("backend")
    amoy = parts.filter(pl.col("backend") == "amoy").to_dicts()[0]
    assert amoy["job_delay_s"] == 0.25 * 60  # signed at :45:15, slot :45
    assert amoy["finality_s"] == 0.5 * 60
    github = parts.filter(pl.col("backend") == "github").to_dicts()[0]
    assert github["job_delay_s"] == 0.5 * 60 and github["finality_s"] == 0.5 * 60


def test_gas_summary_labels_mainnet_cost_as_a_function_of_the_stated_price() -> None:
    _, _, receipts = frames()
    gas = analysis.gas_summary(receipts, [1.0, 10.0]).to_dicts()[0]
    assert gas["anchors"] == 1 and gas["gas_median"] == 50_000
    assert gas["fee_paid_median"] == pytest.approx(50_000 * 30e9 / 1e18)
    assert gas["mainnet_eth_at_1_gwei"] == pytest.approx(5e-5)
    assert gas["mainnet_eth_at_10_gwei"] == pytest.approx(5e-4)


def test_proof_sizes_grow_with_log_tree_size() -> None:
    proofs = bench.proof_sizes([1, 2, 1000, 1024]).to_dicts()
    assert [p["inclusion_hashes"] for p in proofs] == [0, 1, 10, 10]
    assert all(p["inclusion_bytes"] == 32 * p["inclusion_hashes"] for p in proofs)
    assert proofs[0]["sth_bytes"] > 64


def test_build_times_cover_every_size() -> None:
    times = bench.build_times([10, 100], repeats=2)
    assert times["tree_size"].to_list() == [10, 100] and (times["median_s"] > 0).all()


def test_dsn_is_made_plain_for_asyncpg() -> None:
    url = "postgresql+asyncpg://u:p@h:5432/db?ssl=require"
    assert collect.plain_dsn(url) == "postgresql://u:p@h:5432/db?sslmode=require"


def test_report_writes_tables_and_figure(tmp_path: Path) -> None:
    events, checkpoints, receipts = frames()
    snap = tmp_path / "live"
    snap.mkdir()
    events.write_parquet(snap / "events.parquet")
    checkpoints.write_parquet(snap / "checkpoints.parquet")
    receipts.write_parquet(snap / "enriched.parquet")
    paths = report.write(snap, tmp_path / "tables", tmp_path / "figures", [1.0])
    assert all(p.exists() for p in paths)
    text = (tmp_path / "tables" / "anchoring_windows.tex").read_text(encoding="utf-8")
    assert "Polygon Amoy" in text and "Do not edit by hand" in text
    assert "estimate" in (tmp_path / "tables" / "anchoring_gas.tex").read_text(encoding="utf-8")
