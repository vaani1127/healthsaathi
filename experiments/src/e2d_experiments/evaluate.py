"""Metrics, as defined in experiments/PREREGISTRATION.md section 4. This is the only experiment
module (with the runner and report) that reads labels.

Evaluation covers staff accesses in the test part of a split, refused accesses included.
Break-glass accesses are left out of the budgeted metrics for every method because they always go
to the 24-hour review queue (SPEC 5.5); for the same reason attack type 8 (break-glass abuse) is
left out and reported as review-queue outcomes instead.

Alerts: for each method and test clinic-day, the B accesses with the highest score above 0
(fewer if fewer score above 0), ties broken by access event id (`e2d_core.detect.budget`).

- recall@B: attack accesses that are alerts / all attack accesses, pooled over all test
  clinic-days. The unit is the access event.
- precision@B: attack accesses that are alerts / alerts raised; alerts_raised is reported too.
- pr_auc: average precision of the scores over all scored test accesses.
- campaign_detection: share of campaigns (with a test access) with at least one alert.
- hours_to_first_alert: median over detected campaigns of the hours from the campaign's first
  test access to the time its first alert could be raised. An alert is timed when every feature
  it used is available: `feature_delay_hours` after the access (24 hours for methods that use the
  forgery flags, which are computed 24 hours later).
- alerts_per_1000: alerts per 1,000 evaluated test accesses.
- coverage: share of benign test accesses explained at or above theta (method independent).
- false_explanation_rate: share of attack accesses of types 1 to 7 and 9 explained at or above
  theta (method independent).
- review-queue outcomes: break-glass accesses in the test part, the share that are attacks, and
  the share of type 8 accesses that went to the review queue.
- feature_auc: single-feature ROC-AUC (either direction) of sigma and each forgery flag, attack
  against benign, reported but not gated by the audit.
"""

from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
from sklearn.metrics import average_precision_score, roc_auc_score

from e2d_core.detect import budget
from e2d_core.explain import default_config
from e2d_core.features import FORGERY_FEATURES
from saathibench.labels import attack_labels

BREAK_GLASS_TYPE = 8
NON_FORGING_TYPES = (1, 2, 3, 4, 5, 6, 7, 9)
# H3 calls mimicry m >= 1/2 high.
MIMICRY_BANDS = ((0.0, 0.5, "low"), (0.5, 1.0001, "high"))
EXPLANATION_SIGNALS = ("sigma", *FORGERY_FEATURES)


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
    """Staff accesses that are neither break-glass nor attack type 8."""
    return frame.filter(
        (pl.col("role") != "patient")
        & (pl.col("break_glass") == 0)
        & (pl.col("attack_type").is_null() | (pl.col("attack_type") != BREAK_GLASS_TYPE))
    )


def method_independent(test: pl.DataFrame) -> dict[str, float]:
    """`test` is every staff access of the test part (before `evaluable`)."""
    theta = default_config().theta
    scored = evaluable(test)
    benign = scored.filter(~pl.col("is_attack"))
    non_forging = scored.filter(
        pl.col("is_attack") & pl.col("attack_type").is_in(NON_FORGING_TYPES)
    )
    queue = test.filter((pl.col("role") != "patient") & (pl.col("break_glass") == 1))
    type8 = test.filter(pl.col("attack_type") == BREAK_GLASS_TYPE)
    return {
        "coverage": _mean(benign["sigma"] >= theta),
        "false_explanation_rate": _mean(non_forging["sigma"] >= theta),
        "review_queue_accesses": float(queue.height),
        "review_queue_attack_share": _mean(queue["is_attack"].cast(pl.Float64)),
        "type8_in_review_queue": _mean((type8["break_glass"] == 1).cast(pl.Float64)),
    }


def feature_auc(test: pl.DataFrame) -> dict[str, float]:
    """ROC-AUC of each explanation signal on its own, attack against benign, either direction."""
    y = test["is_attack"].to_numpy().astype(int)
    out: dict[str, float] = {}
    if y.min(initial=1) == y.max(initial=0):
        return out
    for name in EXPLANATION_SIGNALS:
        x = test[name].cast(pl.Float64).fill_null(0.0).to_numpy()
        value = float(roc_auc_score(y, x)) if len(np.unique(x)) > 1 else 0.5
        out[name] = max(value, 1.0 - value)
    return out


def _band(m: float | None) -> str | None:
    if m is None:
        return None
    return next(name for low, high, name in MIMICRY_BANDS if low <= m < high)


def metrics(
    test: pl.DataFrame,
    scores: np.ndarray,
    budgets: tuple[int, ...],
    feature_delay_hours: float = 0.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Result rows per (budget, metric[, group]) and per-clinic counts per budget (for the cluster
    bootstrap). `test` must already be `evaluable`."""
    y = test["is_attack"].to_numpy().astype(bool)
    rows: list[dict[str, Any]] = []
    clinic_rows: list[dict[str, Any]] = []
    pr_auc = float(average_precision_score(y, scores)) if y.any() and (~y).any() else float("nan")
    scored = test.select(
        "access_event_id", "clinic_id", "clinic_index", "day", "at", "is_attack", "attack_type",
        "campaign_id", "mimicry", "break_glass",
    ).with_columns(pl.Series("score", scores), pl.lit(True).alias("gated"))  # fmt: skip
    delay = pl.duration(seconds=round(feature_delay_hours * 3600))
    for b in budgets:
        ranked = budget(scored, b).with_columns((pl.col("at") + delay).alias("alert_time"))
        alerted = ranked["alert"].to_numpy()
        attacks = int(y.sum())
        hits = int((alerted & y).sum())
        alerts = int(alerted.sum())
        campaigns = (
            ranked.filter(pl.col("is_attack"))
            .group_by("campaign_id")
            .agg(
                pl.col("at").min().alias("first_seen"),
                pl.col("alert_time").filter(pl.col("alert")).min().alias("first_alert"),
            )
        )
        detected = campaigns.filter(pl.col("first_alert").is_not_null())
        hours = (detected["first_alert"] - detected["first_seen"]).dt.total_seconds() / 3600
        base = {"budget": b}
        rows += [
            {**base, "metric": "recall", "value": hits / attacks if attacks else float("nan")},
            {**base, "metric": "precision", "value": hits / alerts if alerts else float("nan")},
            {**base, "metric": "alerts_raised", "value": float(alerts)},
            {**base, "metric": "pr_auc", "value": pr_auc},
            {
                **base,
                "metric": "campaign_detection",
                "value": detected.height / campaigns.height if campaigns.height else float("nan"),
            },
            {**base, "metric": "hours_to_first_alert", "value": _median(hours)},
            {**base, "metric": "alerts_per_1000", "value": 1000 * alerts / max(1, len(y))},
        ]
        per_clinic = ranked.group_by("clinic_index").agg(
            pl.col("is_attack").sum().alias("attacks"),
            (pl.col("is_attack") & pl.col("alert")).sum().alias("hits"),
            pl.col("alert").sum().alias("alerts"),
        )
        clinic_rows += [{**base, **r} for r in per_clinic.iter_rows(named=True)]
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
    return rows, clinic_rows
