"""Figures for the paper and the results report, drawn only from committed result files:

- experiments/results/v1/<run>/summary.parquet and results.parquet (the registered runs),
- experiments/results/v1/diagnostics.json and diagnostics2.json (diagnostics, not registered),
- experiments/audit/v1/summary.json (the pre-registration audit).

Every figure is written as a vector PDF (for LaTeX) and a 300 DPI PNG (for documents).

    uv run python -m e2d_experiments.paper_figures                 # into paper/figures
"""

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import polars as pl
from matplotlib.figure import Figure

from e2d_experiments.export import METHOD_NAMES
from e2d_experiments.stats import bootstrap_ci

RESULTS = Path("experiments/results/v1")
AUDIT = Path("experiments/audit/v1/summary.json")
DPI = 300
SPLITS = ("temporal", "clinic", "attack", "user")
SPLIT_NAMES = {"temporal": "Temporal", "clinic": "Clinic", "attack": "Attack", "user": "User"}
BUDGETS = (3, 5, 10)
# A fixed colour per method (validated categorical palette). The upper bound is a ceiling, not a
# competitor, so it is drawn as a grey dashed reference.
METHODS = (
    "e2d", "b0_rules", "b1_iforest", "b2_iforest_role", "b3_vae", "b4_coaccess",
    "b5_explanation_only", "upper_lightgbm",
)  # fmt: skip
COLOURS = {
    "e2d": "#2a78d6", "b0_rules": "#eb6834", "b1_iforest": "#1baf7a",
    "b2_iforest_role": "#eda100", "b3_vae": "#e87ba4", "b4_coaccess": "#008300",
    "b5_explanation_only": "#4a3aa7", "upper_lightgbm": "#52514e",
}  # fmt: skip
MARKERS = {
    "e2d": "o", "b0_rules": "s", "b1_iforest": "^", "b2_iforest_role": "v", "b3_vae": "D",
    "b4_coaccess": "P", "b5_explanation_only": "X", "upper_lightgbm": "*",
}  # fmt: skip
INK, MUTED, GRID, NEUTRAL, LIGHT_E2D = "#0b0b0b", "#52514e", "#e4e3df", "#a9a8a2", "#9bbfe9"
ATTACK_NAMES = {
    1: "1 Relative", 2: "2 Colleague", 3: "3 Overreach", 4: "4 Post-care", 5: "5 Bulk export",
    6: "6 Low and slow", 7: "7 Shared login", 8: "8 Break-glass", 9: "9 Edit after sign",
    10: "10 Forgery",
}  # fmt: skip
FLAGS = (
    ("self_created_recent", "Self-created\nrecently"),
    ("no_progress", "No progress"),
    ("off_path_creation", "Off-path\ncreation"),
    ("created_off_hours", "Created\noff-hours"),
    ("cancelled_after_access", "Cancelled\nafter access"),
)
VARIANTS = (
    ("appointment", "Forged appointment", COLOURS["e2d"]),
    ("lab_order", "Forged lab order", COLOURS["b0_rules"]),
    ("care_team", "Forged care-team entry", COLOURS["b1_iforest"]),
)
ABLATIONS = (
    ("none", "E2D as registered"), ("no_forgery", "No forgery features"),
    ("no_graph", "No graph features"), ("templates_core", "Core templates only"),
    ("role_onehot", "Role one-hot"), ("role_per_role", "One model per role"),
    ("learning_curve_10", "Per role, 10% of training"), ("learning_curve_25", "Per role, 25%"),
    ("learning_curve_50", "Per role, 50%"), ("window_1h", "Behaviour window 1 h"),
    ("window_24h", "Behaviour window 24 h"), ("window_7d", "Behaviour window 7 d"),
)  # fmt: skip
HARD_NEGATIVE_RUNS = (
    ("hard_neg_half", "hard_neg_half", "half"),
    ("none", "main", "v1"),
    ("hard_neg_double", "hard_neg_double", "double"),
)


def style() -> None:
    plt.rcParams.update(
        {
            "font.size": 8,
            "axes.titlesize": 9,
            "axes.labelsize": 8,
            "legend.fontsize": 7,
            "xtick.labelsize": 7,
            "ytick.labelsize": 7,
            "axes.edgecolor": MUTED,
            "axes.labelcolor": INK,
            "text.color": INK,
            "xtick.color": MUTED,
            "ytick.color": MUTED,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.color": GRID,
            "grid.linewidth": 0.6,
            "axes.axisbelow": True,
            "lines.linewidth": 1.5,
            "pdf.fonttype": 42,
            "savefig.bbox": "tight",
        }
    )


def summary(run: str) -> pl.DataFrame:
    return pl.read_parquet(RESULTS / run / "summary.parquet")


def cell(frame: pl.DataFrame, **match: Any) -> dict[str, Any] | None:
    for key, value in match.items():
        frame = frame.filter(pl.col(key).is_null() if value is None else pl.col(key) == value)
    return frame.row(0, named=True) if frame.height else None


def recall5(frame: pl.DataFrame, method: str) -> dict[str, Any] | None:
    return cell(frame, method=method, split="temporal", budget=5, metric="recall", group=None)


def interval(row: dict[str, Any]) -> tuple[float, float, float]:
    return float(row["mean"]), float(row["ci_low"]), float(row["ci_high"])


def error(mean: float, low: float, high: float) -> list[list[float]]:
    return [[mean - low], [high - mean]]


def save(fig: Figure, out: Path, name: str) -> list[Path]:
    paths = [out / f"{name}.pdf", out / f"{name}.png"]
    fig.savefig(paths[0])
    fig.savefig(paths[1], dpi=DPI)
    plt.close(fig)
    return paths


def method_line(ax: Any, xs: list[float], rows: list[dict[str, Any]], method: str) -> None:
    ax.errorbar(
        xs,
        [r["mean"] for r in rows],
        yerr=[[r["mean"] - r["ci_low"] for r in rows], [r["ci_high"] - r["mean"] for r in rows]],
        label=METHOD_NAMES[method],
        color=COLOURS[method],
        marker=MARKERS[method],
        markersize=4,
        capsize=2,
        linestyle="--" if method == "upper_lightgbm" else "-",
        linewidth=2.2 if method == "e2d" else 1.1,
    )


def recall_by_budget(out: Path) -> list[Path]:
    main = summary("none")
    fig, axes = plt.subplots(1, 4, figsize=(7.2, 2.4), sharey=True)
    for ax, split in zip(axes, SPLITS, strict=True):
        for method in METHODS:
            rows = [
                cell(main, method=method, split=split, budget=b, metric="recall", group=None)
                for b in BUDGETS
            ]
            method_line(ax, [float(b) for b in BUDGETS], [r for r in rows if r], method)
        ax.set_title(f"{SPLIT_NAMES[split]} split")
        ax.set_xticks(BUDGETS)
    fig.supxlabel("Alerts per clinic-day (B)", fontsize=8, y=-0.04)
    axes[0].set_ylabel("Recall@B (attack accesses alerted)")
    axes[0].set_ylim(0, 0.72)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(
        handles, labels, loc="upper center", ncol=4, frameon=False, bbox_to_anchor=(0.5, -0.1)
    )
    fig.suptitle("Recall at each alert budget (mean of 10 seeds, 95% bootstrap interval)", y=1.02)
    return save(fig, out, "fig_recall_by_budget")


def main_result(out: Path) -> list[Path]:
    main = summary("none")
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 2.6), sharey=True)
    order = list(reversed(METHODS))
    for ax, metric, title in zip(
        axes, ("recall", "precision"), ("Recall@5", "Precision@5"), strict=True
    ):
        for i, method in enumerate(order):
            row = cell(main, method=method, split="temporal", budget=5, metric=metric, group=None)
            if row is None:
                continue
            mean, low, high = interval(row)
            ax.barh(i, mean, color=COLOURS[method], height=0.7)
            ax.errorbar(mean, i, xerr=error(mean, low, high), color=INK, capsize=2, lw=0.8)
            ax.text(high + 0.008, i, f"{mean:.3f}", va="center", fontsize=7)
        ax.set_yticks(range(len(order)), [METHOD_NAMES[m] for m in order])
        ax.set_title(f"{title}, temporal split")
        ax.set_xlim(0, ax.get_xlim()[1] * 1.15)
        ax.grid(axis="y", visible=False)
    fig.suptitle(
        "Main result at the primary budget (B = 5; mean of 10 seeds, 95% interval)", y=1.03
    )
    return save(fig, out, "fig_main_result")


def recall_by_type(out: Path) -> list[Path]:
    main = summary("none")
    types = [t for t in range(1, 11) if t != 8]
    grid = np.full((len(METHODS), len(types)), np.nan)
    for i, method in enumerate(METHODS):
        for j, t in enumerate(types):
            row = cell(
                main, method=method, split="temporal", budget=5, metric="recall_by_type",
                group=f"type_{t}",
            )  # fmt: skip
            if row:
                grid[i, j] = row["mean"]
    fig, ax = plt.subplots(figsize=(7.2, 3.0))
    image = ax.imshow(grid, cmap="Blues", vmin=0, vmax=1, aspect="auto")
    for i in range(grid.shape[0]):
        for j in range(grid.shape[1]):
            value = float(grid[i, j])
            colour = "white" if value > 0.55 else INK
            ax.text(j, i, f"{value:.2f}", ha="center", va="center", fontsize=6.5, color=colour)
    ax.set_xticks(range(len(types)), [ATTACK_NAMES[t] for t in types], rotation=30, ha="right")
    ax.set_yticks(range(len(METHODS)), [METHOD_NAMES[m] for m in METHODS])
    ax.grid(visible=False)
    for spine in ax.spines.values():
        spine.set_visible(False)
    fig.colorbar(image, ax=ax, fraction=0.025, pad=0.01, label="Recall@5")
    ax.set_title(
        "Recall@5 by attack type, temporal split (mean of 10 seeds; type 8 goes to review)"
    )
    return save(fig, out, "fig_recall_by_type")


def per_seed(results: pl.DataFrame, **match: Any) -> dict[int, float]:
    for key, value in match.items():
        results = results.filter(pl.col(key).is_null() if value is None else pl.col(key) == value)
    return dict(zip(results["seed"].to_list(), results["value"].to_list(), strict=True))


def paired(a: dict[int, float], b: dict[int, float]) -> list[float]:
    return [a[s] - b[s] for s in sorted(set(a) & set(b))]


def hypotheses(out: Path) -> list[Path]:
    """Paired per-seed differences behind H2, H4 and H5, with a bootstrap interval over seeds."""
    main = pl.read_parquet(RESULTS / "none" / "results.parquet")
    no_forgery = pl.read_parquet(RESULTS / "no_forgery" / "results.parquet")
    tests = pl.read_parquet(RESULTS / "none" / "tests.parquet")
    rows: list[tuple[str, list[float], str]] = []
    for split in SPLITS:
        for b in BUDGETS:
            chosen = tests.filter((pl.col("split") == split) & (pl.col("budget") == b))
            best = str(chosen["best_baseline"][0])
            plain = {"split": split, "budget": b, "metric": "recall", "group": None}
            primary = "PRIMARY: " if (split, b) == ("temporal", 5) else ""
            label = f"H2 {primary}{SPLIT_NAMES[split]}, B={b} (vs {METHOD_NAMES[best].split()[0]})"
            values = paired(
                per_seed(main, method="e2d", **plain), per_seed(main, method=best, **plain)
            )
            rows.append((label, values, COLOURS["e2d"]))
    for split in ("temporal", "attack"):
        match = {"split": split, "budget": 5, "metric": "recall_by_type", "group": "type_10"}
        values = paired(
            per_seed(main, method="e2d", **match), per_seed(no_forgery, method="e2d", **match)
        )
        rows.append((f"H4 type 10, {SPLIT_NAMES[split]} (with vs without forgery features)",
                     values, COLOURS["b1_iforest"]))  # fmt: skip
    plain5 = {"split": "temporal", "budget": 5, "metric": "recall", "group": None}
    values = paired(
        per_seed(main, method="e2d", **plain5),
        per_seed(main, method="b5_explanation_only", **plain5),
    )
    rows.append(("H5 Temporal, B=5 (E2D vs B5 explanation only)", values, COLOURS["e2d"]))
    fig, ax = plt.subplots(figsize=(7.2, 4.0))
    for i, (label, diffs, colour) in enumerate(reversed(rows)):
        mean, low, high = bootstrap_ci(diffs)
        big = "PRIMARY" in label
        ax.errorbar(
            mean, i, xerr=error(mean, low, high), fmt="o", color=colour,
            markersize=6 if big else 4, capsize=2, lw=1.6 if big else 1,
        )  # fmt: skip
        ax.text(high + 0.004, i, f"+{mean:.3f}", va="center", fontsize=6.5)
    ax.axvline(0, color=MUTED, lw=0.8)
    ax.set_yticks(range(len(rows)), [label for label, _, _ in reversed(rows)])
    ax.set_xlabel("Difference in recall (E2D minus comparison), per seed")
    ax.set_title("Pre-registered comparisons: mean paired difference over 10 seeds, 95% interval")
    ax.grid(axis="y", visible=False)
    return save(fig, out, "fig_hypotheses")


def ablations(out: Path) -> list[Path]:
    rows: list[tuple[str, tuple[float, float, float]]] = []
    for run, label in ABLATIONS:
        row = recall5(summary(run), "e2d")
        if row:
            rows.append((label, interval(row)))
    scorers = summary("scorers")
    for method, label in (("e2d_ecod", "ECOD scorer"), ("e2d_copod", "COPOD scorer")):
        row = recall5(scorers, method)
        if row:
            rows.append((label, interval(row)))
    best = recall5(summary("none"), "b0_rules")
    registered = rows[0][1][0]
    fig, ax = plt.subplots(figsize=(7.2, 3.6))
    for i, (label, (mean, low, high)) in enumerate(reversed(rows)):
        colour = COLOURS["e2d"] if label == "E2D as registered" else LIGHT_E2D
        ax.barh(i, mean, color=colour, height=0.7)
        ax.errorbar(mean, i, xerr=error(mean, low, high), color=INK, capsize=2, lw=0.8)
        ax.text(high + 0.005, i, f"{mean:.3f}", va="center", fontsize=6.5)
    ax.axvline(registered, color=COLOURS["e2d"], lw=0.9, linestyle=":")
    ax.text(registered, -0.9, " E2D as registered", color=MUTED, fontsize=6.5)
    if best:
        ax.axvline(best["mean"], color=COLOURS["b0_rules"], lw=1.2, linestyle="--")
        ax.text(best["mean"], len(rows) - 0.2, " best raw baseline (B0)", color=MUTED, fontsize=6.5)
    ax.set_yticks(range(len(rows)), [label for label, _ in reversed(rows)])
    ax.set_xlabel("E2D recall@5, temporal split")
    ax.set_title("Ablations of E2D (descriptive; mean of 10 seeds, 95% interval)")
    ax.grid(axis="y", visible=False)
    return save(fig, out, "fig_ablations")


def resimulated(out: Path) -> list[Path]:
    diagnostics = json.loads((RESULTS / "diagnostics.json").read_text(encoding="utf-8"))
    mimicry = [(0.0, "mimicry_0", "0"), (0.5, "mimicry_05", "0.5"), (1.0, "mimicry_1", "1")]
    hard = [
        (float(np.mean([d[f"hard_negative_share_{key}"] for d in diagnostics])), run, name)
        for run, key, name in HARD_NEGATIVE_RUNS
    ]
    panels = (
        ("Attacker mimicry (every campaign at one level)", mimicry, "Mimicry m"),
        ("Benign hard negatives (half, v1, double)", hard,
         "Measured hard-negative share of benign accesses"),
    )  # fmt: skip
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 2.8), sharey=True)
    for ax, (title, points, xlabel) in zip(axes, panels, strict=True):
        frames = {run: summary(run) for _, run, _ in points}
        xs = [x for x, _, _ in points]
        for method in METHODS:
            rows = [recall5(frames[run], method) for _, run, _ in points]
            present = [r for r in rows if r]
            if len(present) == len(rows):
                method_line(ax, xs, present, method)
        ax.set_title(title)
        ax.set_xlabel(xlabel)
        ticks = [
            name if run.startswith("mimicry") else f"{x:.3f}\n({name})" for x, run, name in points
        ]
        ax.set_xticks(xs, ticks)
    axes[0].annotate(
        "m = 0: data fail the\nseparability audit\n(run unaudited)",
        xy=(0, 0.21), xytext=(0.12, 0.55), fontsize=6.5, color=MUTED,
        arrowprops={"arrowstyle": "-", "color": MUTED, "lw": 0.6},
    )  # fmt: skip
    axes[0].set_ylabel("Recall@5, temporal split")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(
        handles, labels, loc="upper center", ncol=4, frameon=False, bbox_to_anchor=(0.5, -0.11)
    )
    fig.suptitle("Re-simulated ablations, every method (mean of 10 seeds, 95% interval)", y=1.03)
    return save(fig, out, "fig_resimulated")


def h4_forgery_features(out: Path) -> list[Path]:
    runs = ((summary("none"), "With forgery features", COLOURS["e2d"]),
            (summary("no_forgery"), "Without", LIGHT_E2D))  # fmt: skip
    fig, ax = plt.subplots(figsize=(3.5, 2.6))
    for i, split in enumerate(("temporal", "attack")):
        for j, (frame, label, colour) in enumerate(runs):
            row = cell(
                frame, method="e2d", split=split, budget=5, metric="recall_by_type",
                group="type_10",
            )  # fmt: skip
            if row is None:
                continue
            mean, low, high = interval(row)
            x = i + (j - 0.5) * 0.36
            ax.bar(x, mean, width=0.34, color=colour, label=label if i == 0 else None)
            ax.errorbar(x, mean, yerr=error(mean, low, high), color=INK, capsize=2, lw=0.8)
            ax.text(x, high + 0.01, f"{mean:.2f}", ha="center", fontsize=6.5)
    ax.set_xticks([0, 1], ["Temporal", "Attack\n(type 10 unseen)"])
    ax.set_ylabel("E2D recall@5, type 10")
    ax.set_title("H4: E2D type-10 recall with and without forgery features")
    ax.set_ylim(0, 0.55)
    ax.legend(frameon=False, loc="upper center", ncol=2, fontsize=6.5)
    ax.grid(axis="x", visible=False)
    return save(fig, out, "fig_h4_forgery_features")


def forgery_flags(out: Path) -> list[Path]:
    """Fire rate of each forgery flag for benign accesses and each type-10 variant, from the
    pre-registration audit."""
    audit = json.loads(AUDIT.read_text(encoding="utf-8"))["runs"]
    tables = [run["forgery_flag_auc_type10_vs_benign"] for run in audit]
    groups: list[tuple[str, str, list[float]]] = [
        (
            "Benign",
            NEUTRAL,
            [float(np.mean([t["fire_rate_benign"][k] for t in tables])) for k, _ in FLAGS],
        )
    ]
    for variant, label, colour in VARIANTS:
        rates = [
            float(np.mean([t["by_variant"][variant]["fire_rate_type10"][k] for t in tables]))
            for k, _ in FLAGS
        ]
        groups.append((label, colour, rates))
    fig, ax = plt.subplots(figsize=(7.2, 2.6))
    width = 0.8 / len(groups)
    for g, (label, colour, rates) in enumerate(groups):
        xs = np.arange(len(FLAGS)) + (g - (len(groups) - 1) / 2) * width
        ax.bar(xs, rates, width=width * 0.92, color=colour, label=label)
    ax.set_xticks(range(len(FLAGS)), [name for _, name in FLAGS])
    ax.set_ylabel("Fire rate")
    ax.set_title("Forgery flags: benign vs each forged-evidence variant (audit, 10 seeds)")
    ax.set_ylim(0, 1)
    ax.legend(frameon=False, fontsize=6.5, ncol=4, loc="upper center")
    ax.grid(axis="x", visible=False)
    return save(fig, out, "fig_forgery_flags")


def false_explanation(out: Path) -> list[Path]:
    diagnostics = json.loads((RESULTS / "diagnostics.json").read_text(encoding="utf-8"))
    types = sorted({int(t) for d in diagnostics for t in d["false_explanation_by_type"]})
    fig, ax = plt.subplots(figsize=(7.2, 2.4))
    for i, t in enumerate(types):
        mean, low, high = bootstrap_ci(
            [d["false_explanation_by_type"][str(t)][0] for d in diagnostics]
        )
        ax.bar(i, mean, color=COLOURS["b5_explanation_only"], width=0.65)
        ax.errorbar(i, mean, yerr=error(mean, low, high), color=INK, capsize=2, lw=0.8)
        ax.text(i, high + 0.015, f"{mean:.2f}", ha="center", fontsize=6.5)
    ax.axhline(0.10, color=COLOURS["b0_rules"], linestyle="--", lw=1)
    ax.text(-0.45, 0.112, "H1 threshold 0.10", ha="left", fontsize=6.5, color=MUTED)
    ax.set_xticks(range(len(types)), [ATTACK_NAMES[t] for t in types], rotation=20, ha="right")
    ax.set_ylabel("Share explained (sigma >= 0.5)")
    ax.set_title("False-explanation rate by attack type, temporal split (diagnostic; 10 seeds)")
    ax.grid(axis="x", visible=False)
    return save(fig, out, "fig_false_explanation")


def pr_auc(out: Path) -> list[Path]:
    diagnostics = json.loads((RESULTS / "diagnostics.json").read_text(encoding="utf-8"))
    kinds = (("pooled", "Pooled over clinics (registered)", LIGHT_E2D),
             ("mean_per_clinic", "Mean per clinic", COLOURS["e2d"]))  # fmt: skip
    fig, ax = plt.subplots(figsize=(3.5, 2.4))
    for i, split in enumerate(("temporal", "user")):
        for j, (key, label, colour) in enumerate(kinds):
            mean, low, high = bootstrap_ci([d[f"e2d_pr_auc_{split}"][key] for d in diagnostics])
            x = i + (j - 0.5) * 0.36
            ax.bar(x, mean, width=0.34, color=colour, label=label if i == 0 else None)
            ax.errorbar(x, mean, yerr=error(mean, low, high), color=INK, capsize=2, lw=0.8)
            ax.text(x, high + 0.006, f"{mean:.3f}", ha="center", fontsize=6.5)
    ax.set_xticks([0, 1], ["Temporal", "User"])
    ax.set_ylabel("E2D PR-AUC")
    ax.set_title("E2D PR-AUC: pooled vs per clinic (diagnostic)")
    ax.set_ylim(0, 0.25)
    ax.legend(frameon=False, fontsize=6.5, loc="upper right")
    ax.grid(axis="x", visible=False)
    return save(fig, out, "fig_pr_auc")


def sigma_distribution(seeds: list[dict[str, Any]], out: Path) -> list[Path]:
    bins = np.linspace(0, 1, 21)
    centres = (bins[:-1] + bins[1:]) / 2

    def total(histograms: list[list[int]]) -> np.ndarray:
        return np.sum(np.asarray(histograms, dtype=float), axis=0)

    benign = total([s["sigma_hist_benign"] for s in seeds])
    other = total(
        [h for s in seeds for t, h in s["sigma_hist_by_type"].items() if t not in ("8", "10")]
    )
    forged = total(
        [s["sigma_hist_by_type"]["10"] for s in seeds if "10" in s["sigma_hist_by_type"]]
    )
    lines = ((benign, "Benign", NEUTRAL),
             (other, "Attacks, types 1-7 and 9", COLOURS["b0_rules"]),
             (forged, "Attacks, type 10 (forgery)", COLOURS["e2d"]))  # fmt: skip
    fig, ax = plt.subplots(figsize=(3.5, 2.5))
    for counts, label, colour in lines:
        ax.step(centres, counts / counts.sum(), where="mid", label=label, color=colour, lw=1.4)
    ax.axvline(0.5, color=MUTED, linestyle=":", lw=0.9)
    ax.set_xlabel("Explanation strength sigma (theta = 0.5 dotted)")
    ax.set_ylabel("Share of accesses (log scale)")
    ax.set_yscale("log")
    ax.set_title("Explanation strength, temporal test part (10 seeds)")
    ax.legend(frameon=False, fontsize=6.5)
    return save(fig, out, "fig_sigma_distribution")


def explaining_templates(seeds: list[dict[str, Any]], out: Path) -> list[Path]:
    counts: dict[tuple[int, str], int] = {}
    for s in seeds:
        for attack_type, template, n in s["falsely_explained_by_template"]:
            key = (int(attack_type), str(template))
            counts[key] = counts.get(key, 0) + int(n)
    types = sorted({t for t, _ in counts})
    per_type = {t: sum(v for (t2, _), v in counts.items() if t2 == t) for t in types}
    templates = sorted(
        {tpl for _, tpl in counts},
        key=lambda tpl: -sum(v for (_, x), v in counts.items() if x == tpl),
    )
    palette = [COLOURS[m] for m in METHODS[:7]]
    fig, ax = plt.subplots(figsize=(7.2, 2.6))
    bottom = np.zeros(len(types))
    for k, template in enumerate(templates[: len(palette)]):
        shares = np.array([counts.get((t, template), 0) / per_type[t] for t in types])
        ax.bar(range(len(types)), shares, bottom=bottom, color=palette[k], label=template,
               width=0.65, edgecolor="white", linewidth=1)  # fmt: skip
        bottom += shares
    ax.set_xticks(range(len(types)), [ATTACK_NAMES[t] for t in types], rotation=20, ha="right")
    ax.set_ylabel("Share of falsely explained accesses")
    ax.set_title("Which template explains the attacks counted as falsely explained (H1)")
    ax.legend(frameon=False, fontsize=6.5, ncol=4, loc="upper center", bbox_to_anchor=(0.5, -0.32))
    ax.grid(axis="x", visible=False)
    return save(fig, out, "fig_false_explanation_templates")


def benign_alerts(seeds: list[dict[str, Any]], out: Path) -> list[Path]:
    scenario: dict[str, int] = {}
    for s in seeds:
        for name, n in s["benign_alerts_by_scenario"]:
            scenario[str(name)] = scenario.get(str(name), 0) + int(n)
    whole = sum(scenario.values())
    names = sorted(scenario, key=lambda n: scenario[n])
    fig, ax = plt.subplots(figsize=(3.5, 2.4))
    for i, name in enumerate(names):
        share = scenario[name] / whole
        ax.barh(i, share, color=NEUTRAL if name == "ordinary" else COLOURS["e2d"], height=0.7)
        ax.text(share + 0.01, i, f"{share:.2f}", va="center", fontsize=6.5)
    ax.set_yticks(range(len(names)), [n.replace("_", " ") for n in names])
    ax.set_xlabel("Share of E2D's benign alerts at B = 5")
    ax.set_title("What E2D's false alarms are (temporal, 10 seeds)")
    ax.grid(axis="y", visible=False)
    return save(fig, out, "fig_benign_alerts")


def diagnostics2(out: Path) -> list[Path]:
    """Figures from diagnostics2.json; skipped when that file does not exist."""
    path = RESULTS / "diagnostics2.json"
    if not path.exists():
        return []
    seeds: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    return (
        sigma_distribution(seeds, out)
        + explaining_templates(seeds, out)
        + benign_alerts(seeds, out)
    )


FIGURES: tuple[Callable[[Path], list[Path]], ...] = (
    recall_by_budget, main_result, recall_by_type, hypotheses, ablations, resimulated,
    h4_forgery_features, forgery_flags, false_explanation, pr_auc, diagnostics2,
)  # fmt: skip


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Paper figures from the committed results.")
    parser.add_argument("--out", type=Path, default=Path("paper/figures"))
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    style()
    written = [p for figure in FIGURES for p in figure(args.out)]
    print(f"wrote {len(written)} files to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
