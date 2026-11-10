"""Rewrite windows and anchoring cost from a snapshot (pure functions on data frames).

Rewrite window of an audit event for one backend: the time from the event being written until the
first witness record of that backend that covers it became final. A checkpoint of tree size n
covers leaf positions 0..n-1. SPEC 8 splits it into the anchoring interval and job start delay
(waiting for the next checkpoint) and the time to finality (checkpoint to final witness record);
both parts are reported.

Final time per backend: Amoy and Besu, the timestamp of the block holding the transaction;
OpenTimestamps, the timestamp of the Bitcoin block in the attestation; GitHub, the time the commit
was accepted (`confirmed_at`).
"""

from datetime import datetime

import polars as pl

INTERVAL_MINUTES = 15
# Backends measured by a bench run (anchors sent back to back), not by the 15 minute schedule.
BENCH_BACKENDS = ("besu",)
WINDOW_COLUMNS = ("backend", "events", "censored", "median_min", "p90_min", "p95_min", "max_min")


def final_times(receipts: pl.DataFrame) -> pl.DataFrame:
    """Confirmed receipts with the moment each became final."""
    confirmed = receipts.filter(pl.col("status") == "confirmed")
    return confirmed.with_columns(
        pl.when(pl.col("backend") == "github")
        .then(pl.col("confirmed_at"))
        .otherwise(pl.col("final_at"))
        .alias("final_at")
    ).filter(pl.col("final_at").is_not_null())


def event_windows(
    events: pl.DataFrame, checkpoints: pl.DataFrame, receipts: pl.DataFrame
) -> pl.DataFrame:
    """One row per (event, backend): window in seconds, null when not yet covered (censored).

    `events`: clinic_id, idx, at. `checkpoints`: checkpoint_id, clinic_id, tree_size, signed_at.
    `receipts`: checkpoint_id, backend, status, confirmed_at, final_at.
    """
    final = final_times(receipts).join(
        checkpoints.select("checkpoint_id", "clinic_id", "tree_size"), on="checkpoint_id"
    )
    out = []
    for backend in sorted(final["backend"].unique().to_list()):
        rows = (
            final.filter(pl.col("backend") == backend)
            .select("clinic_id", (pl.col("tree_size") - 1).alias("last_idx"), "final_at")
            .sort("clinic_id", "last_idx")
            # The earliest final record among this checkpoint and every later (larger) one.
            .with_columns(
                pl.col("final_at").reverse().cum_min().reverse().over("clinic_id").alias("first")
            )
        )
        joined = events.sort("idx").join_asof(
            rows.sort("last_idx"),
            left_on="idx",
            right_on="last_idx",
            by="clinic_id",
            strategy="forward",
            check_sortedness=False,  # sorted within each clinic above
        )
        out.append(
            joined.select(
                "clinic_id",
                "idx",
                "at",
                pl.lit(backend).alias("backend"),
                (pl.col("first") - pl.col("at"))
                .dt.total_milliseconds()
                .truediv(1000)
                .alias("window_s"),
            )
        )
    if not out:
        return pl.DataFrame(
            schema={
                "clinic_id": pl.String,
                "idx": pl.Int64,
                "at": pl.Datetime("us", "UTC"),
                "backend": pl.String,
                "window_s": pl.Float64,
            }
        )
    return pl.concat(out)


def _keys(frame: pl.DataFrame) -> list[str]:
    """Group by study period too when the frame has one (cadence.with_period)."""
    return ["period", "backend"] if "period" in frame.columns else ["backend"]


def window_summary(windows: pl.DataFrame) -> pl.DataFrame:
    minutes = pl.col("window_s") / 60
    keys = _keys(windows)
    return (
        windows.group_by(keys)
        .agg(
            pl.len().alias("events"),
            pl.col("window_s").null_count().alias("censored"),
            minutes.median().alias("median_min"),
            minutes.quantile(0.9, "nearest").alias("p90_min"),
            minutes.quantile(0.95, "nearest").alias("p95_min"),
            minutes.max().alias("max_min"),
        )
        .sort(keys)
        .select(*keys[:-1], *WINDOW_COLUMNS)
    )


def checkpoint_parts(checkpoints: pl.DataFrame, receipts: pl.DataFrame) -> pl.DataFrame:
    """Per (checkpoint, backend): job start delay after the 15 minute slot, and time to finality."""
    slot = pl.col("signed_at").dt.truncate(f"{INTERVAL_MINUTES}m")
    return (
        final_times(receipts)
        .join(checkpoints.select("checkpoint_id", "signed_at"), on="checkpoint_id")
        .select(
            "checkpoint_id",
            "backend",
            pl.when(pl.col("backend").is_in(BENCH_BACKENDS))
            .then(None)
            .otherwise((pl.col("signed_at") - slot).dt.total_milliseconds().truediv(1000))
            .alias("job_delay_s"),
            (pl.col("final_at") - pl.col("signed_at"))
            .dt.total_milliseconds()
            .truediv(1000)
            .alias("finality_s"),
            "signed_at",
        )
    )


def parts_summary(parts: pl.DataFrame) -> pl.DataFrame:
    keys = _keys(parts)
    return (
        parts.group_by(keys)
        .agg(
            pl.len().alias("checkpoints"),
            (pl.col("job_delay_s") / 60).median().alias("job_delay_median_min"),
            (pl.col("finality_s") / 60).median().alias("finality_median_min"),
            (pl.col("finality_s") / 60).quantile(0.95, "nearest").alias("finality_p95_min"),
        )
        .sort(keys)
    )


def gas_summary(receipts: pl.DataFrame, mainnet_gwei: list[float]) -> pl.DataFrame:
    """Gas per anchor transaction on each EVM chain, the fee actually paid, and an ESTIMATE of the
    fee on Ethereum mainnet at each stated gas price (not a measurement)."""
    evm = receipts.filter(pl.col("gas_used").is_not_null())
    base = evm.group_by("backend").agg(
        pl.len().alias("anchors"),
        pl.col("gas_used").median().alias("gas_median"),
        pl.col("gas_used").min().alias("gas_min"),
        pl.col("gas_used").max().alias("gas_max"),
        (pl.col("gas_used") * pl.col("gas_price_wei") / 1e18).median().alias("fee_paid_median"),
    )
    estimates = [
        (pl.col("gas_median") * gwei / 1e9).alias(f"mainnet_eth_at_{gwei:g}_gwei")
        for gwei in mainnet_gwei
    ]
    return base.with_columns(estimates).sort("backend")


def study_period(events: pl.DataFrame) -> tuple[datetime | None, datetime | None]:
    if events.is_empty():
        return None, None
    return events["at"].min(), events["at"].max()  # type: ignore[return-value]
