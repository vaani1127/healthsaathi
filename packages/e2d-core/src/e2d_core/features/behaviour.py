"""Behaviour features of an access (SPEC 5.4), computed for many accesses at once.

For each access a by user u at time t:

- accesses_w, patients_w, exports_w: u's accesses, distinct patients and exports or prints in the
  window (t - w, t], for w in 1 h, 24 h and 7 d. The access itself counts.
- off_shift: 1 when no shift of u (not cancelled) covers t.
- new_device: 1 when the device was first seen less than 24 h before t, or is unknown.
- session_age_min: minutes since the session started (0 when the session is unknown).

Inputs are frames with the product's column names; only past and present rows are used, so the
same values can be computed when an access happens.
"""

from datetime import timedelta

import polars as pl

WINDOWS = {"1h": "1h", "24h": "24h", "7d": "7d"}
NEW_DEVICE_AGE = timedelta(hours=24)
EXPORT_ACTIONS = ("export", "print")

FEATURES = (
    *(f"{kind}_{w}" for kind in ("accesses", "patients", "exports") for w in WINDOWS),
    "off_shift",
    "new_device",
    "session_age_min",
)

EVENT_COLUMNS = ("id", "user_id", "patient_id", "action", "at", "session_id", "device_id")


def _rolling(events: pl.DataFrame) -> pl.DataFrame:
    """Per-user rolling counts. `events` must be sorted by user_id, at, id."""
    out = events.select("id")
    for name, period in WINDOWS.items():
        rolled = events.rolling(index_column="at", period=period, group_by="user_id").agg(
            pl.len().alias(f"accesses_{name}"),
            pl.col("patient_id").n_unique().alias(f"patients_{name}"),
            pl.col("is_export").sum().alias(f"exports_{name}"),
        )
        # rolling keeps one row per input row, in the (sorted) input order.
        if not rolled["at"].equals(events["at"]) or not rolled["user_id"].equals(events["user_id"]):
            raise AssertionError("rolling output is not aligned with its input")
        out = out.hstack(rolled.drop("user_id", "at"))
    return out


def _off_shift(events: pl.DataFrame, shifts: pl.DataFrame) -> pl.Series:
    active = (
        shifts.filter(pl.col("status") != "cancelled")
        .select(
            pl.col("user_id").cast(pl.String),
            pl.col("starts_at").cast(events.schema["at"]),
            pl.col("ends_at").cast(events.schema["at"]),
        )
        .sort("user_id", "starts_at")
    )
    # Latest shift that started at or before t; overlapping shifts are merged per user first, so
    # the latest start also has the latest end among those covering t.
    merged = active.with_columns(pl.col("ends_at").cum_max().over("user_id").alias("ends_at"))
    joined = (
        events.select("id", "user_id", "at")
        .sort("at")
        .join_asof(
            merged.sort("starts_at"),
            left_on="at",
            right_on="starts_at",
            by="user_id",
            strategy="backward",
            check_sortedness=False,  # both sides are sorted just above
        )
    )
    covered = pl.col("ends_at").is_not_null() & (pl.col("ends_at") >= pl.col("at"))
    flags = joined.select("id", (~covered).cast(pl.Int8).alias("off_shift"))
    return events.select("id").join(flags, on="id", how="left", maintain_order="left")["off_shift"]


def behaviour_features(
    events: pl.DataFrame,
    shifts: pl.DataFrame,
    sessions: pl.DataFrame,
    devices: pl.DataFrame,
) -> pl.DataFrame:
    """One row per access event: `access_event_id` plus FEATURES, in the order of `events`."""
    missing = set(EVENT_COLUMNS) - set(events.columns)
    if missing:
        raise ValueError(f"access events are missing columns {sorted(missing)}")
    base = (
        events.select(
            pl.col("id").cast(pl.String),
            pl.col("user_id").cast(pl.String),
            pl.col("patient_id").cast(pl.String),
            "action",
            "at",
            pl.col("session_id").cast(pl.String),
            pl.col("device_id").cast(pl.String),
        )
        .with_columns(pl.col("action").is_in(EXPORT_ACTIONS).cast(pl.Int32).alias("is_export"))
        .sort("user_id", "at", "id")
    )
    rolling = _rolling(base)
    off_shift = _off_shift(base, shifts)
    device_seen = devices.select(
        pl.col("id").cast(pl.String).alias("device_id"),
        pl.col("first_seen").cast(base.schema["at"]),
    )
    session_start = sessions.select(
        pl.col("id").cast(pl.String).alias("session_id"),
        pl.col("created_at").cast(base.schema["at"]).alias("session_start"),
    )
    context = (
        base.select("id", "at", "device_id", "session_id")
        .join(device_seen, on="device_id", how="left", maintain_order="left")
        .join(session_start, on="session_id", how="left", maintain_order="left")
        .select(
            (
                pl.col("first_seen").is_null()
                | (pl.col("at") - pl.col("first_seen") < NEW_DEVICE_AGE)
            )
            .cast(pl.Int8)
            .alias("new_device"),
            ((pl.col("at") - pl.col("session_start")).dt.total_seconds() / 60)
            .clip(lower_bound=0)
            .fill_null(0.0)
            .alias("session_age_min"),
        )
    )
    features = (
        rolling.with_columns(off_shift.alias("off_shift"))
        .hstack(context)
        .rename({"id": "access_event_id"})
    )
    order = events.select(pl.col("id").cast(pl.String).alias("access_event_id"))
    return order.join(features, on="access_event_id", how="left", maintain_order="left").select(
        "access_event_id", *FEATURES
    )
