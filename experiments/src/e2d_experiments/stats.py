"""Statistics as registered in experiments/PREREGISTRATION.md section 5.

- Intervals: 95% bootstrap (10,000 resamples) over seeds, and a cluster bootstrap over test
  clinics pooled across seeds (a clinic is identified by its position in the run config).
- Tests: one-sided Wilcoxon signed-rank; zero differences are dropped (Wilcoxon's method) and the
  number dropped is reported. Effect size: matched-pairs rank-biserial r.
"""

from collections.abc import Sequence
from typing import Any

import numpy as np
import polars as pl
from scipy.stats import wilcoxon

RAW_BASELINES = ("b0_rules", "b1_iforest", "b2_iforest_role", "b3_vae", "b4_coaccess")
BASELINES = (*RAW_BASELINES, "b5_explanation_only")
KEYS = ("split", "ablation", "method", "budget", "metric", "group")
RESAMPLES = 10_000
PRIMARY = ("temporal", 5)
SNOOPING_TYPES = (1, 2, 6)
OTHER_TYPES = (3, 4, 5, 7, 9)


def bootstrap_ci(
    values: Sequence[float], n: int = RESAMPLES, seed: int = 0, alpha: float = 0.05
) -> tuple[float, float, float]:
    data = np.asarray([v for v in values if not np.isnan(v)], dtype=float)
    if len(data) == 0:
        return float("nan"), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    means = rng.choice(data, size=(n, len(data)), replace=True).mean(axis=1)
    low, high = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    return float(data.mean()), float(low), float(high)


def cluster_ci(
    numerators: Sequence[float],
    denominators: Sequence[float],
    n: int = RESAMPLES,
    seed: int = 0,
    alpha: float = 0.05,
) -> tuple[float, float, float]:
    """Pooled ratio sum(num) / sum(den) with a bootstrap over clusters (clinics)."""
    num = np.asarray(numerators, dtype=float)
    den = np.asarray(denominators, dtype=float)
    if len(num) == 0 or den.sum() == 0:
        return float("nan"), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    picks = rng.integers(0, len(num), size=(n, len(num)))
    totals = den[picks].sum(axis=1)
    ratios = np.where(totals > 0, num[picks].sum(axis=1) / np.where(totals > 0, totals, 1), np.nan)
    low, high = np.nanquantile(ratios, [alpha / 2, 1 - alpha / 2])
    return float(num.sum() / den.sum()), float(low), float(high)


def wilcoxon_greater(a: Sequence[float], b: Sequence[float]) -> float:
    """p-value that a is larger than b, paired by position. 1.0 when they never differ."""
    diff = np.asarray(a, dtype=float) - np.asarray(b, dtype=float)
    diff = diff[~np.isnan(diff)]
    if len(diff) == 0 or np.all(diff == 0):
        return 1.0
    return float(wilcoxon(diff, alternative="greater", zero_method="wilcox").pvalue)


def zeros_dropped(a: Sequence[float], b: Sequence[float]) -> int:
    diff = np.asarray(a, dtype=float) - np.asarray(b, dtype=float)
    return int((diff[~np.isnan(diff)] == 0).sum())


def rank_biserial(a: Sequence[float], b: Sequence[float]) -> float:
    """Matched-pairs rank-biserial correlation: +1 when a is always larger, -1 when b is."""
    diff = np.asarray(a, dtype=float) - np.asarray(b, dtype=float)
    diff = diff[(~np.isnan(diff)) & (diff != 0)]
    if len(diff) == 0:
        return 0.0
    ranks = np.argsort(np.argsort(np.abs(diff))) + 1.0
    plus, minus = ranks[diff > 0].sum(), ranks[diff < 0].sum()
    return float((plus - minus) / (plus + minus))


def holm(pvalues: Sequence[float]) -> list[float]:
    """Holm-Bonferroni adjusted p-values, in the input order."""
    order = np.argsort(pvalues)
    m = len(pvalues)
    adjusted = np.empty(m)
    running = 0.0
    for rank, index in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvalues[index]))
        adjusted[index] = running
    return [float(v) for v in adjusted]


def summarise(results: pl.DataFrame, clinics: pl.DataFrame | None = None) -> pl.DataFrame:
    """Mean and seed-bootstrap interval for every result cell, plus the clinic cluster interval
    for recall and precision when per-clinic counts are given."""
    rows = []
    for key, group in results.group_by(list(KEYS), maintain_order=True):
        cell = dict(zip(KEYS, key, strict=True))
        mean, low, high = bootstrap_ci(group["value"].to_list())
        row: dict[str, Any] = {
            **cell, "seeds": group.height, "mean": mean, "ci_low": low, "ci_high": high,
            "cluster_low": None, "cluster_high": None,
        }  # fmt: skip
        pooled_metric = cell["metric"] in ("recall", "precision") and cell["group"] is None
        if clinics is not None and pooled_metric:
            part = clinics.filter(
                (pl.col("split") == cell["split"])
                & (pl.col("ablation") == cell["ablation"])
                & (pl.col("method") == cell["method"])
                & (pl.col("budget") == cell["budget"])
            )
            pooled = part.group_by("clinic_index").agg(pl.col("attacks", "hits", "alerts").sum())
            den = "attacks" if cell["metric"] == "recall" else "alerts"
            _, c_low, c_high = cluster_ci(pooled["hits"].to_list(), pooled[den].to_list())
            row |= {"cluster_low": c_low, "cluster_high": c_high}
        rows.append(row)
    return pl.DataFrame(rows, infer_schema_length=None)


def _paired(group: pl.DataFrame, a: str, b: str) -> tuple[list[float], list[float]]:
    ours = group.filter(pl.col("method") == a).select("seed", "value")
    theirs = group.filter(pl.col("method") == b).select("seed", pl.col("value").alias("other"))
    paired = ours.join(theirs, on="seed").sort("seed")
    return paired["value"].to_list(), paired["other"].to_list()


def _test_row(group: pl.DataFrame, against: str, method: str = "e2d") -> dict[str, Any]:
    a, b = _paired(group, method, against)
    return {
        "e2d_mean": float(np.nanmean(a)) if a else float("nan"),
        "baseline_mean": float(np.nanmean(b)) if b else float("nan"),
        "seeds": len(a),
        "zeros_dropped": zeros_dropped(a, b),
        "p": wilcoxon_greater(a, b) if a else float("nan"),
        "effect": rank_biserial(a, b) if a else float("nan"),
    }


def best_baseline(group: pl.DataFrame, baselines: Sequence[str] = RAW_BASELINES) -> str | None:
    means = {
        name: float(group.filter(pl.col("method") == name)["value"].mean() or 0.0)  # type: ignore[arg-type]
        for name in baselines
        if group.filter(pl.col("method") == name).height
    }
    return max(means, key=lambda k: means[k]) if means else None


def hypothesis_tests(
    results: pl.DataFrame, method: str = "e2d", metric: str = "recall", ablation: str = "none"
) -> pl.DataFrame:
    """H2: E2D against the best raw-feature baseline per split and budget. The primary test
    (temporal, B = 5) is not corrected; the other cells are Holm-corrected together."""
    cells = results.filter(
        (pl.col("metric") == metric) & (pl.col("ablation") == ablation) & pl.col("group").is_null()
    )
    rows = []
    for (split, b), group in cells.group_by("split", "budget", maintain_order=True):
        best = best_baseline(group)
        if best is None or group.filter(pl.col("method") == method).is_empty():
            continue
        rows.append(
            {"split": split, "budget": b, "best_baseline": best,
             "primary": (split, b) == PRIMARY, **_test_row(group, best, method)}
        )  # fmt: skip
    if not rows:
        return pl.DataFrame()
    table = pl.DataFrame(rows)
    secondary = [i for i, r in enumerate(rows) if not r["primary"]]
    adjusted = holm([rows[i]["p"] for i in secondary]) if secondary else []
    p_adjusted = [r["p"] for r in rows]
    for i, value in zip(secondary, adjusted, strict=True):
        p_adjusted[i] = value
    return table.with_columns(pl.Series("p_adjusted", p_adjusted))


def h1(results: pl.DataFrame, split: str = "temporal") -> dict[str, float]:
    """H1: coverage of benign accesses and false-explanation rate, mean and interval."""
    out: dict[str, float] = {}
    for metric in ("coverage", "false_explanation_rate"):
        values = results.filter(
            (pl.col("split") == split)
            & (pl.col("metric") == metric)
            & (pl.col("ablation") == "none")
        )["value"].to_list()
        mean, low, high = bootstrap_ci(values)
        out |= {f"{metric}_mean": mean, f"{metric}_low": low, f"{metric}_high": high}
    return out


def _type_recall(
    results: pl.DataFrame, method: str, split: str, budget: int, group: str
) -> dict[int, float]:
    rows = results.filter(
        (pl.col("method") == method)
        & (pl.col("split") == split)
        & (pl.col("budget") == budget)
        & (pl.col("group") == group)
    )
    return dict(zip(rows["seed"].to_list(), rows["value"].to_list(), strict=True))


def h3(
    results: pl.DataFrame, baseline: str, split: str = "temporal", budget: int = 5
) -> dict[str, float]:
    """H3: per seed, mean recall gain over types 1, 2 and 6 at high mimicry (m >= 1/2) minus the
    mean gain over types 3, 4, 5, 7 and 9 at high mimicry, each over the types with a
    high-mimicry campaign in that seed's test part; one-sided Wilcoxon against 0. Seeds where
    either group is empty are dropped and counted."""

    def gain(seed: int, t: int) -> float | None:
        group = f"type_{t}_high"
        ours = _type_recall(results, "e2d", split, budget, group).get(seed)
        theirs = _type_recall(results, baseline, split, budget, group).get(seed)
        return None if ours is None or theirs is None else ours - theirs

    diffs, dropped = [], 0
    for seed in sorted(set(results["seed"].to_list())):
        snoop = [g for t in SNOOPING_TYPES if (g := gain(seed, t)) is not None]
        other = [g for t in OTHER_TYPES if (g := gain(seed, t)) is not None]
        if snoop and other:
            diffs.append(float(np.mean(snoop) - np.mean(other)))
        else:
            dropped += 1
    zeros = [0.0] * len(diffs)
    return {
        "seeds": float(len(diffs)),
        "seeds_dropped": float(dropped),
        "zeros_dropped": float(zeros_dropped(diffs, zeros)),
        "mean_difference": float(np.mean(diffs)) if diffs else float("nan"),
        "p": wilcoxon_greater(diffs, zeros) if diffs else float("nan"),
    }


def h4(main: pl.DataFrame, no_forgery: pl.DataFrame, budget: int = 5) -> pl.DataFrame:
    """H4: type 10 recall@B of E2D with and without forgery features, on the temporal and attack
    splits, Holm-corrected over the two."""
    rows = []
    for split in ("temporal", "attack"):
        ours = _type_recall(main, "e2d", split, budget, "type_10")
        theirs = _type_recall(no_forgery, "e2d", split, budget, "type_10")
        seeds = sorted(set(ours) & set(theirs))
        a, b = [ours[s] for s in seeds], [theirs[s] for s in seeds]
        rows.append(
            {
                "split": split,
                "seeds": len(seeds),
                "zeros_dropped": zeros_dropped(a, b),
                "with_forgery": float(np.mean(a)) if a else float("nan"),
                "without_forgery": float(np.mean(b)) if b else float("nan"),
                "p": wilcoxon_greater(a, b) if seeds else float("nan"),
                "effect": rank_biserial(a, b) if seeds else float("nan"),
            }
        )
    table = pl.DataFrame(rows)
    return table.with_columns(pl.Series("p_holm", holm(table["p"].to_list())))


def h5(results: pl.DataFrame) -> dict[str, Any]:
    """H5: E2D recall@5 above B5 (explanation only) on the temporal split."""
    split, b = PRIMARY
    group = results.filter(
        (pl.col("metric") == "recall")
        & (pl.col("ablation") == "none")
        & pl.col("group").is_null()
        & (pl.col("split") == split)
        & (pl.col("budget") == b)
    )
    return {"split": split, "budget": b, **_test_row(group, "b5_explanation_only")}
