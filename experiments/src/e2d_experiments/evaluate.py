"""Metrics (SPEC 8). This is the only experiment module that reads labels.

Evaluation covers staff accesses in the test part of a split. Break-glass accesses are left out
of the budgeted metrics for every method: they always go to the 24-hour review queue (SPEC 5.5).

- recall@B, precision@B: share of attack accesses alerted, and share of alerts that are attacks,
  with B alerts per clinic and day.
- pr_auc: average precision of the scores over all test accesses.
- campaign_detection: share of campaigns (with a test access) that got at least one alert.
- hours_to_first_alert: median hours from a detected campaign's first test access to its first
  alert.
- alerts_per_1000: alerts per 1,000 test accesses.
- coverage: share of benign test accesses explained at or above theta (method independent).
- false_explanation_rate: share of attack accesses of non-forging types (all but 10) explained at
  or above theta (method independent).
"""

from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
from sklearn.metrics import average_precision_score

from e2d_core.detect import budget
from e2d_core.explain import default_config
from saathibench.labels import attack_labels

FORGING_TYPES = (10,)
MIMICRY_BANDS = ((0.0, 1 / 3, "low"), (1 / 3, 2 / 3, "mid"), (2 / 3, 1.0001, "high"))


def _mean(series: pl.Series) -> float:
    value = series.mean() if len(series) else None
    return float(value) if isinstance(value, int | float) else float("nan")


def _median(series: pl.Series) -> float:
    value = series.median() if len(series) else None
    return float(value) if isinstance(value, int | float) else float("nan")


def with_labels(features: pl.DataFrame, run: Path) -> pl.DataFrame:
    labels = attack_labels(run).select(
        "access_event_id", "is_attack", "attack_type", "campaign_id", "mimicry"
    )
    return features.join(labels, on="access_event_id", how="left", maintain_order="left")


def evaluable(frame: pl.DataFrame) -> pl.DataFrame:
    """Staff accesses that are not break-glass."""
    return frame.filter((pl.col("role") != "patient") & (pl.col("break_glass") == 0))


def method_independent(test: pl.DataFrame) -> dict[str, float]:
    theta = default_config().theta
    benign = test.filter(~pl.col("is_attack"))
    non_forging = test.filter(pl.col("is_attack") & ~pl.col("attack_type").is_in(FORGING_TYPES))
    return {
        "coverage": _mean(benign["sigma"] >= theta) if benign.height else float("nan"),
        "false_explanation_rate": (
            _mean(non_forging["sigma"] >= theta) if non_forging.height else float("nan")
        ),
    }


def _band(m: float | None) -> str | None:
    if m is None:
        return None
    return next(name for low, high, name in MIMICRY_BANDS if low <= m < high)


def metrics(
    test: pl.DataFrame, scores: np.ndarray, budgets: tuple[int, ...]
) -> list[dict[str, Any]]:
    """One row per (budget, metric[, attack type or mimicry band])."""
    y = test["is_attack"].to_numpy().astype(bool)
    rows: list[dict[str, Any]] = []
    pr_auc = float(average_precision_score(y, scores)) if y.any() and (~y).any() else float("nan")
    scored = test.select(
        "access_event_id", "clinic_id", "day", "at", "is_attack", "attack_type", "campaign_id",
        "mimicry", "break_glass",
    ).with_columns(pl.Series("score", scores), pl.lit(True).alias("gated"))  # fmt: skip
    for b in budgets:
        ranked = budget(scored, b)
        alerted = ranked["alert"].to_numpy()
        attacks = int(y.sum())
        hits = int((alerted & y).sum())
        alerts = int(alerted.sum())
        campaigns = (
            ranked.filter(pl.col("is_attack"))
            .group_by("campaign_id")
            .agg(
                pl.col("at").min().alias("first_seen"),
                pl.col("at").filter(pl.col("alert")).min().alias("first_alert"),
            )
        )
        detected = campaigns.filter(pl.col("first_alert").is_not_null())
        delay = (detected["first_alert"] - detected["first_seen"]).dt.total_seconds() / 3600
        base = {"budget": b}
        rows += [
            {**base, "metric": "recall", "value": hits / attacks if attacks else float("nan")},
            {**base, "metric": "precision", "value": hits / alerts if alerts else float("nan")},
            {**base, "metric": "pr_auc", "value": pr_auc},
            {
                **base,
                "metric": "campaign_detection",
                "value": detected.height / campaigns.height if campaigns.height else float("nan"),
            },
            {
                **base,
                "metric": "hours_to_first_alert",
                "value": _median(delay),
            },
            {**base, "metric": "alerts_per_1000", "value": 1000 * alerts / max(1, len(y))},
        ]
        attack_rows = ranked.filter(pl.col("is_attack"))
        for attack_type, group in attack_rows.group_by("attack_type"):
            rows.append(
                {
                    **base,
                    "metric": "recall_by_type",
                    "group": f"type_{attack_type[0]}",
                    "value": _mean(group["alert"]),
                }
            )
        banded = attack_rows.with_columns(
            pl.col("mimicry").map_elements(_band, return_dtype=pl.String).alias("band")
        )
        for (attack_type, band), group in banded.group_by("attack_type", "band"):
            rows.append(
                {
                    **base,
                    "metric": "recall_by_type_mimicry",
                    "group": f"type_{attack_type}_{band}",
                    "value": _mean(group["alert"]),
                }
            )
    return rows
