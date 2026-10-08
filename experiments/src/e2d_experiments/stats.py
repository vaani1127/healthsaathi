"""Statistics over seeds (SPEC 8): mean with a 95% bootstrap interval, paired one-sided Wilcoxon
signed-rank tests of E2D against the best baseline with Holm correction, and the matched-pairs
rank-biserial effect size."""

from collections.abc import Sequence

import numpy as np
import polars as pl
from scipy.stats import wilcoxon

# H2 compares E2D with the best raw-feature baseline. B5 uses explanations, so it is reported
# but is not a raw-feature baseline.
RAW_BASELINES = ("b0_rules", "b1_iforest", "b2_iforest_role", "b3_vae", "b4_coaccess")
BASELINES = (*RAW_BASELINES, "b5_explanation_only")
KEYS = ("split", "ablation", "method", "budget", "metric", "group")


def bootstrap_ci(
    values: Sequence[float], n: int = 10_000, seed: int = 0, alpha: float = 0.05
) -> tuple[float, float, float]:
    data = np.asarray([v for v in values if not np.isnan(v)], dtype=float)
    if len(data) == 0:
        return float("nan"), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    means = rng.choice(data, size=(n, len(data)), replace=True).mean(axis=1)
    low, high = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    return float(data.mean()), float(low), float(high)


def wilcoxon_greater(a: Sequence[float], b: Sequence[float]) -> float:
    """p-value that a is larger than b, paired by position. 1.0 when they never differ."""
    diff = np.asarray(a, dtype=float) - np.asarray(b, dtype=float)
    diff = diff[~np.isnan(diff)]
    if len(diff) == 0 or np.all(diff == 0):
        return 1.0
    return float(wilcoxon(diff, alternative="greater", zero_method="wilcox").pvalue)


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


def summarise(results: pl.DataFrame) -> pl.DataFrame:
    """Mean and 95% bootstrap interval over seeds for every result cell."""
    rows = []
    for key, group in results.group_by(list(KEYS), maintain_order=True):
        mean, low, high = bootstrap_ci(group["value"].to_list())
        rows.append({**dict(zip(KEYS, key, strict=True)), "seeds": group.height,
                     "mean": mean, "ci_low": low, "ci_high": high})  # fmt: skip
    return pl.DataFrame(rows)


def hypothesis_tests(
    results: pl.DataFrame,
    method: str = "e2d",
    metric: str = "recall",
    ablation: str = "none",
    baselines: Sequence[str] = RAW_BASELINES,
) -> pl.DataFrame:
    """E2D against the raw-feature baseline with the highest mean, per split and budget (H2),
    Holm-corrected over all the tests in the table."""
    cells = results.filter(
        (pl.col("metric") == metric) & (pl.col("ablation") == ablation) & pl.col("group").is_null()
    )
    rows = []
    for (split, b), group in cells.group_by("split", "budget", maintain_order=True):
        ours = group.filter(pl.col("method") == method).sort("seed")
        means = {
            name: group.filter(pl.col("method") == name)["value"].mean()
            for name in baselines
            if group.filter(pl.col("method") == name).height
        }
        if ours.is_empty() or not means:
            continue
        best = max(means, key=lambda k: float(means[k] or 0.0))  # type: ignore[arg-type]
        theirs = group.filter(pl.col("method") == best).sort("seed")
        paired = ours.join(theirs, on="seed", suffix="_base")
        a, b_values = paired["value"].to_list(), paired["value_base"].to_list()
        rows.append(
            {
                "split": split,
                "budget": b,
                "best_baseline": best,
                "e2d_mean": float(np.nanmean(a)),
                "baseline_mean": float(np.nanmean(b_values)),
                "seeds": len(a),
                "p": wilcoxon_greater(a, b_values),
                "effect": rank_biserial(a, b_values),
            }
        )
    if not rows:
        return pl.DataFrame()
    table = pl.DataFrame(rows)
    return table.with_columns(pl.Series("p_holm", holm(table["p"].to_list())))


SNOOPING_TYPES = (1, 2, 6)
OTHER_TYPES = (3, 4, 5, 7, 8, 9)


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
    """H3: per seed, mean recall gain over types 1, 2 and 6 at high mimicry minus the mean gain over
    the other non-forging types at high mimicry; one-sided Wilcoxon against 0."""

    def gain(seed: int, t: int) -> float | None:
        group = f"type_{t}_high"
        ours = _type_recall(results, "e2d", split, budget, group).get(seed)
        theirs = _type_recall(results, baseline, split, budget, group).get(seed)
        return None if ours is None or theirs is None else ours - theirs

    diffs = []
    for seed in sorted(set(results["seed"].to_list())):
        snoop = [g for t in SNOOPING_TYPES if (g := gain(seed, t)) is not None]
        other = [g for t in OTHER_TYPES if (g := gain(seed, t)) is not None]
        if snoop and other:
            diffs.append(float(np.mean(snoop) - np.mean(other)))
    return {
        "seeds": float(len(diffs)),
        "mean_difference": float(np.mean(diffs)) if diffs else float("nan"),
        "p": wilcoxon_greater(diffs, [0.0] * len(diffs)) if diffs else float("nan"),
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
                "with_forgery": float(np.mean(a)) if a else float("nan"),
                "without_forgery": float(np.mean(b)) if b else float("nan"),
                "p": wilcoxon_greater(a, b) if seeds else float("nan"),
                "effect": rank_biserial(a, b) if seeds else float("nan"),
            }
        )
    table = pl.DataFrame(rows)
    return table.with_columns(pl.Series("p_holm", holm(table["p"].to_list())))
