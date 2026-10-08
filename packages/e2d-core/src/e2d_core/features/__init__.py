"""Residual features of accesses (SPEC 5.4). Feature code never reads simulator labels.

`build_features` works on frames with the product's table shapes, so the same code runs on the
simulator's Parquet output and on rows the API loads from Postgres.
"""

from collections.abc import Mapping

import polars as pl

from e2d_core.features.behaviour import FEATURES as BEHAVIOUR_FEATURES
from e2d_core.features.behaviour import behaviour_features
from e2d_core.features.explanation import FEATURES as EXPLANATION_FEATURES
from e2d_core.features.explanation import (
    FORGERY_FEATURES,
    explanation_features,
    explanation_rows,
    flags_from_json,
)
from e2d_core.features.graph import FEATURES as GRAPH_FEATURES
from e2d_core.features.graph import graph_features

ROLES = ("clinic_admin", "doctor", "lab_tech", "nurse", "reception")
ROLE_FEATURES = tuple(f"role_{r}" for r in ROLES)

FEATURE_GROUPS: dict[str, tuple[str, ...]] = {
    "behaviour": BEHAVIOUR_FEATURES,
    "explanation": EXPLANATION_FEATURES,
    "forgery": FORGERY_FEATURES,
    "graph": GRAPH_FEATURES,
}
ALL_FEATURES = tuple(f for group in FEATURE_GROUPS.values() for f in group)


def build_features(
    events: pl.DataFrame,
    frames: Mapping[str, pl.DataFrame],
    explanations: pl.DataFrame,
    *,
    role_onehot: bool = False,
) -> pl.DataFrame:
    """access_event_id plus every feature group (and role one-hot if asked), in event order.

    `events` are access_events rows; `frames` the clinic's other tables (shifts, sessions,
    devices, patients, users, memberships and the care evidence tables); `explanations` one row
    per event as made by `explanation_rows`.
    """
    order = events.select(pl.col("id").cast(pl.String).alias("access_event_id"))
    parts = [
        behaviour_features(events, frames["shifts"], frames["sessions"], frames["devices"]),
        explanation_features(explanations),
        graph_features(events, frames),
    ]
    out = order
    for part in parts:
        out = out.join(part, on="access_event_id", how="left", maintain_order="left")
    if role_onehot:
        out = out.hstack(
            events.select(*((pl.col("role") == r).cast(pl.Int8).alias(f"role_{r}") for r in ROLES))
        )
    return out


__all__ = [
    "ALL_FEATURES",
    "BEHAVIOUR_FEATURES",
    "EXPLANATION_FEATURES",
    "FEATURE_GROUPS",
    "FORGERY_FEATURES",
    "GRAPH_FEATURES",
    "ROLE_FEATURES",
    "behaviour_features",
    "build_features",
    "explanation_features",
    "explanation_rows",
    "flags_from_json",
    "graph_features",
]
