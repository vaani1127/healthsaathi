"""Gate, scorers and budgeted alerting (SPEC 5.5). Scores only rank accesses for review; they
never grant or refuse access.

- Gate: an access is scored only if its explanation strength is below theta, a forgery flag
  fired, or it used break-glass. Every other access scores 0.
- Scorers: IsolationForest (default), ECOD or COPOD, or the deterministic rules baseline when no
  model is fitted yet. Higher means more unusual.
- Budget: per clinic and day, the top B scored accesses become alerts. Break-glass accesses go to
  their own 24-hour review queue instead and never use the budget.
"""

import hashlib
import io
import pickle
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import polars as pl

from e2d_core.explain import default_config

SCORERS = ("iforest", "ecod", "copod")
DEFAULT_SCORER = "iforest"
DEFAULT_BUDGET = 5
# Fewer gated training rows than this and a clinic uses the global model.
MIN_CLINIC_ROWS = 500

# Rules baseline (B0). Weights and the alert threshold are design choices, not fitted values.
RULES_THRESHOLD = 0.5
RULES: tuple[tuple[str, float, float], ...] = (
    # feature, at least this value, points
    ("flags_any", 1, 1.5),
    ("off_shift", 1, 0.5),
    ("new_device", 1, 1.0),
    ("exports_1h", 10, 0.5),
    ("patients_1h", 20, 0.5),
    ("path_len", 4, 0.5),
    ("shares_surname", 1, 0.5),
    ("shares_address", 1, 0.5),
)


def gate(features: pl.DataFrame, theta: float | None = None) -> pl.Series:
    """True for accesses that are scored."""
    theta = default_config().theta if theta is None else theta
    return features.select(
        ((pl.col("sigma") < theta) | (pl.col("flags_any") > 0) | (pl.col("break_glass") > 0)).alias(
            "gated"
        )
    )["gated"]


def rules_score(features: pl.DataFrame) -> np.ndarray:
    """The rules baseline: one minus sigma plus points for each rule that fires."""
    expr = 1.0 - pl.col("sigma")
    for name, at_least, points in RULES:
        expr = expr + pl.when(pl.col(name) >= at_least).then(points).otherwise(0.0)
    return features.select(expr.alias("score"))["score"].to_numpy()


def _make(scorer: str, seed: int) -> Any:
    if scorer == "iforest":
        from sklearn.ensemble import IsolationForest

        return IsolationForest(n_estimators=200, random_state=seed)
    if scorer == "ecod":
        from pyod.models.ecod import ECOD

        return ECOD()
    if scorer == "copod":
        from pyod.models.copod import COPOD

        return COPOD()
    raise ValueError(f"unknown scorer {scorer!r}; use one of {SCORERS}")


@dataclass
class ModelBundle:
    """A fitted scorer with what is needed to use and explain it later."""

    scorer: str
    columns: tuple[str, ...]
    model: Any
    median: list[float]
    scale: list[float]
    trained_rows: int
    clinic_id: str | None = None
    # Score above which an access becomes an alert when scoring one access at a time.
    threshold: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def score(self, features: pl.DataFrame) -> np.ndarray:
        x = _matrix(features, self.columns)
        if self.scorer == "iforest":
            return np.asarray(-self.model.score_samples(x), dtype=float)
        return np.asarray(self.model.decision_function(x), dtype=float)

    def top_features(self, row: Mapping[str, float], k: int = 5) -> list[dict[str, Any]]:
        """The k features furthest from the training median, in robust z units."""
        out: list[tuple[float, dict[str, Any]]] = []
        for name, med, scale in zip(self.columns, self.median, self.scale, strict=True):
            value = float(row.get(name) or 0.0)
            z = (value - med) / scale
            out.append((abs(z), {"feature": name, "value": value, "z": round(z, 3)}))
        return [item for _, item in sorted(out, key=lambda pair: -pair[0])[:k]]


def _matrix(features: pl.DataFrame, columns: Sequence[str]) -> np.ndarray:
    return features.select([pl.col(c).cast(pl.Float64).fill_null(0.0) for c in columns]).to_numpy()


def fit(
    features: pl.DataFrame,
    columns: Sequence[str],
    scorer: str = DEFAULT_SCORER,
    seed: int = 0,
    clinic_id: str | None = None,
) -> ModelBundle:
    x = _matrix(features, columns)
    if len(x) == 0:
        raise ValueError("no rows to fit on")
    model = _make(scorer, seed)
    model.fit(x)
    median = np.median(x, axis=0)
    q75, q25 = np.percentile(x, [75, 25], axis=0)
    scale = np.where(q75 - q25 > 0, q75 - q25, 1.0)
    return ModelBundle(
        scorer=scorer,
        columns=tuple(columns),
        model=model,
        median=[float(v) for v in median],
        scale=[float(v) for v in scale],
        trained_rows=len(x),
        clinic_id=clinic_id,
    )


def calibrate(bundle: ModelBundle, train: pl.DataFrame, days: float, budget: int) -> float:
    """Set the threshold so that, on the training rows, about `budget` accesses a day score
    above it. Used when accesses are scored one at a time instead of ranked per day."""
    scores = bundle.score(train)
    per_day = len(scores) / max(days, 1.0)
    share = min(1.0, budget / per_day) if per_day > 0 else 1.0
    bundle.threshold = float(np.quantile(scores, 1.0 - share)) if len(scores) else 0.0
    return bundle.threshold


def fit_per_clinic(
    by_clinic: Mapping[str, pl.DataFrame],
    columns: Sequence[str],
    scorer: str = DEFAULT_SCORER,
    seed: int = 0,
    min_rows: int = MIN_CLINIC_ROWS,
) -> tuple[dict[str, ModelBundle], ModelBundle | None]:
    """One model per clinic with at least `min_rows` gated rows, and a global model on all rows
    for the rest (None when there is nothing to fit)."""
    models = {
        clinic: fit(rows, columns, scorer, seed, clinic)
        for clinic, rows in sorted(by_clinic.items())
        if rows.height >= min_rows
    }
    pooled = [rows for rows in by_clinic.values() if rows.height]
    fallback = fit(pl.concat(pooled), columns, scorer, seed) if pooled else None
    return models, fallback


def dumps(bundle: ModelBundle) -> bytes:
    buffer = io.BytesIO()
    pickle.dump(bundle, buffer, protocol=pickle.HIGHEST_PROTOCOL)
    return buffer.getvalue()


def loads(data: bytes) -> ModelBundle:
    """Only for model files this system wrote itself (pickle runs code on load)."""
    bundle = pickle.loads(data)  # noqa: S301
    if not isinstance(bundle, ModelBundle):
        raise TypeError("not a model bundle")
    return bundle


def model_version(data: bytes) -> str:
    """sha256 of the model file, stored on every alert."""
    return hashlib.sha256(data).hexdigest()


def budget(
    scored: pl.DataFrame, b: int = DEFAULT_BUDGET, *, key: Sequence[str] = ("clinic_id", "day")
) -> pl.DataFrame:
    """Top `b` eligible accesses per clinic and day.

    `scored` needs access_event_id, the key columns, score, gated and break_glass. Adds
    rank_in_day (null when not ranked) and alert. Break-glass accesses are not ranked: they go
    to the 24-hour review queue.
    """
    eligible = (
        (pl.col("gated").cast(pl.Boolean)) & (pl.col("break_glass") == 0) & (pl.col("score") > 0)
    )
    ranked = (
        scored.filter(eligible)
        .sort([*key, "score", "access_event_id"], descending=[False] * len(key) + [True, False])
        .with_columns(pl.int_range(1, pl.len() + 1).over(list(key)).alias("rank_in_day"))
        .select("access_event_id", "rank_in_day")
    )
    return scored.join(
        ranked, on="access_event_id", how="left", maintain_order="left"
    ).with_columns((pl.col("rank_in_day") <= b).fill_null(False).alias("alert"))
