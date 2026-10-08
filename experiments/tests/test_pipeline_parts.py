from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from e2d_core.features import ALL_FEATURES, ROLE_FEATURES
from e2d_experiments import evaluate, export, stats
from e2d_experiments.methods import E2D, RAW_FEATURES, e2d_columns, make_method

METHODS = (
    "b0_rules",
    "b1_iforest",
    "b2_iforest_role",
    "b3_vae",
    "b4_coaccess",
    "b5_explanation_only",
    "e2d",
    "upper_lightgbm",
)


def synthetic(n: int = 600, seed: int = 0) -> pl.DataFrame:
    """Feature rows shaped like prepare.prepare output, with a few obvious attacks."""
    rng = np.random.default_rng(seed)
    start = datetime(2026, 3, 2, 3, 30, tzinfo=UTC)
    attack = rng.random(n) < 0.05
    columns = {c: rng.random(n) for c in (*ALL_FEATURES, *ROLE_FEATURES)}
    columns["sigma"] = np.where(attack, 0.1, 0.9)
    columns["break_glass"] = np.zeros(n)
    columns["flags_any"] = np.zeros(n)
    columns["off_shift"] = attack.astype(float)
    return pl.DataFrame(
        {
            "access_event_id": [f"e{i}" for i in range(n)],
            "clinic_id": rng.choice(["c1", "c2"], n),
            "user_id": rng.choice(["u1", "u2", "u3", "u4"], n),
            "patient_id": rng.choice([f"p{i}" for i in range(50)], n),
            "role": rng.choice(["doctor", "nurse"], n),
            "at": [start + timedelta(minutes=7 * i) for i in range(n)],
            "day": [(start + timedelta(minutes=7 * i)).date() for i in range(n)],
            "hour_local": rng.uniform(8, 20, n),
            "weekday": rng.integers(0, 7, n),
            "refused": np.zeros(n, dtype=int),
            **columns,
            "is_attack": attack,
            "attack_type": pl.Series(
                [
                    int(t) if a else None
                    for t, a in zip(rng.choice([1, 6, 10], n), attack, strict=True)
                ],
                dtype=pl.Int64,
            ),
            "campaign_id": pl.Series(
                [
                    str(c) if a else None
                    for c, a in zip(rng.choice(["a", "b"], n), attack, strict=True)
                ],
                dtype=pl.String,
            ),
            "mimicry": pl.Series(
                [float(m) if a else None for m, a in zip(rng.random(n), attack, strict=True)],
                dtype=pl.Float64,
            ),
        }
    )


@pytest.mark.parametrize("name", METHODS)
def test_every_method_fits_and_scores(name: str) -> None:
    data = synthetic()
    method = make_method(name, seed=0, vae_epochs=1)
    labels = data["is_attack"].to_numpy().astype(int)
    method.fit(data, labels if name == "upper_lightgbm" else None)
    scores = method.score(data)
    assert scores.shape == (data.height,) and np.isfinite(scores).all()


def test_e2d_ranks_gated_accesses_first_and_ablations_drop_groups() -> None:
    data = synthetic()
    method = make_method("e2d", seed=0)
    method.fit(data)
    scores = method.score(data)
    gated = data["sigma"].to_numpy() < 0.5
    assert (scores[gated] > 0).all() and (scores[~gated] == 0).all()
    assert "flag_no_progress" not in e2d_columns("no_forgery")
    assert "path_len" not in e2d_columns("no_graph")
    assert set(ROLE_FEATURES) <= set(e2d_columns("role_onehot"))
    window = e2d_columns(window="1h")
    assert "accesses_1h" in window and "accesses_7d" not in window
    per_role = make_method("e2d", 0, "learning_curve_25", train_share=0.25)
    assert isinstance(per_role, E2D) and per_role.per_role and per_role.equal_size
    per_role.fit(data)
    assert per_role.fallback is not None
    with pytest.raises(ValueError, match="unknown method"):
        make_method("magic", 0)
    with pytest.raises(ValueError, match="labels"):
        make_method("upper_lightgbm", 0).fit(data)
    assert set(RAW_FEATURES) >= {"hour_local", "weekday", "refused"}


def test_metrics_and_method_independent_values() -> None:
    data = synthetic()
    scores = (1 - data["sigma"]).to_numpy()
    rows = evaluate.metrics(data, scores, (3, 5))
    by = {(r["budget"], r["metric"], r.get("group")): r["value"] for r in rows}
    assert 0 < by[(5, "recall", None)] <= 1
    assert by[(5, "recall", None)] >= by[(3, "recall", None)]
    assert by[(5, "pr_auc", None)] == pytest.approx(1.0)
    assert by[(5, "campaign_detection", None)] == 1.0
    assert any(k[1] == "recall_by_type_mimicry" for k in by)
    fixed = evaluate.method_independent(data)
    assert fixed["coverage"] == 1.0 and fixed["false_explanation_rate"] == 0.0
    assert evaluate.evaluable(data.with_columns(pl.lit(1).alias("break_glass"))).is_empty()


def test_statistics() -> None:
    mean, low, high = stats.bootstrap_ci([1.0, 2.0, 3.0, 4.0])
    assert low <= mean == 2.5 <= high
    assert np.isnan(stats.bootstrap_ci([float("nan")])[0])
    better = [0.8, 0.82, 0.79, 0.85, 0.9, 0.81, 0.83, 0.86, 0.84, 0.88]
    worse = [0.5, 0.52, 0.49, 0.55, 0.6, 0.51, 0.53, 0.56, 0.54, 0.58]
    assert stats.wilcoxon_greater(better, worse) < 0.01
    assert stats.wilcoxon_greater(worse, worse) == 1.0
    assert stats.rank_biserial(better, worse) == 1.0
    assert stats.rank_biserial(worse, worse) == 0.0
    assert stats.holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])


def results_frame() -> pl.DataFrame:
    rows = []
    for seed in range(10):
        for method, base in (
            ("e2d", 0.8),
            ("b1_iforest", 0.5),
            ("b0_rules", 0.3),
            ("b5_explanation_only", 0.9),
        ):
            for split in ("temporal", "clinic"):
                rows.append(
                    {"seed": seed, "split": split, "ablation": "none", "method": method,
                     "budget": 5, "metric": "recall", "group": None,
                     "value": base + 0.01 * seed}
                )  # fmt: skip
        rows.append(
            {"seed": seed, "split": "temporal", "ablation": "none", "method": "-",
             "budget": 0, "metric": "coverage", "group": None, "value": 0.95}
        )  # fmt: skip
    return pl.DataFrame(rows)


def test_hypothesis_tests_and_export(tmp_path: Path) -> None:
    results = results_frame()
    tests = stats.hypothesis_tests(results)
    assert set(tests["best_baseline"]) == {"b1_iforest"}, "B5 is not a raw-feature baseline"
    assert (tests["p_holm"] < 0.05).all() and (tests["effect"] == 1.0).all()
    summary = stats.summarise(results)
    written = export.write_tables(summary, tests, tmp_path / "tables")
    names = {p.name for p in written}
    assert {"recall_at_5.tex", "tests.tex", "coverage.tex", "pr_auc.tex"} <= names
    table = (tmp_path / "tables" / "recall_at_5.tex").read_text(encoding="utf-8")
    assert "E2D" in table and "\\toprule" in table and "Generated by" in table
    figures = export.write_figures(summary, tmp_path / "figures")
    assert figures and all(p.stat().st_size > 0 for p in figures)
    assert stats.hypothesis_tests(results.filter(pl.col("method") != "e2d")).is_empty()
    assert date(2026, 1, 1)  # dates in results are plain values


def typed_results(method_values: dict[str, float], ablation: str = "none") -> pl.DataFrame:
    rows = []
    for seed in range(10):
        for method, base in method_values.items():
            for t in range(1, 11):
                bump = 0.3 if method == "e2d" and t in (1, 2, 6) else 0.0
                for split in ("temporal", "attack"):
                    rows.append(
                        {"seed": seed, "split": split, "ablation": ablation, "method": method,
                         "budget": 5, "metric": "recall_by_type_mimicry",
                         "group": f"type_{t}_high", "value": base + bump + 0.001 * seed}
                    )  # fmt: skip
                    rows.append(
                        {"seed": seed, "split": split, "ablation": ablation, "method": method,
                         "budget": 5, "metric": "recall_by_type", "group": f"type_{t}",
                         "value": base + 0.001 * seed}
                    )  # fmt: skip
    return pl.DataFrame(rows)


def test_h1_h3_h4_and_report(tmp_path: Path) -> None:
    from e2d_experiments import report

    main = pl.concat(
        [results_frame(), typed_results({"e2d": 0.5, "b1_iforest": 0.4})], how="vertical_relaxed"
    )
    h1 = stats.h1(main)
    assert h1["coverage_mean"] == pytest.approx(0.95)
    h3 = stats.h3(main, "b1_iforest")
    assert h3["seeds"] == 10 and h3["mean_difference"] == pytest.approx(0.3) and h3["p"] < 0.01
    weaker = typed_results({"e2d": 0.2}, ablation="no_forgery")
    h4 = stats.h4(main, weaker)
    assert set(h4["split"]) == {"temporal", "attack"} and (h4["p_holm"] < 0.05).all()

    (tmp_path / "none").mkdir()
    (tmp_path / "no_forgery").mkdir()
    main.write_parquet(tmp_path / "none" / "results.parquet")
    weaker.write_parquet(tmp_path / "no_forgery" / "results.parquet")
    assert report.main([str(tmp_path), "--export", str(tmp_path / "tables")]) == 0
    tex = (tmp_path / "tables" / "hypotheses.tex").read_text(encoding="utf-8")
    assert "H4 type 10 recall, temporal" in tex and r"\begin{table}" in tex
