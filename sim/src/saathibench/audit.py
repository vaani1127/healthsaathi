"""Separability audit (SPEC 7): attacks must not be separable from benign accesses by any single
observable feature or by a depth-2 decision tree (ROC-AUC above 0.9 fails the run).

The features are the e2d-core behaviour features plus the access's local hour, weekday and
whether it was refused, on staff accesses only. Explanation results are deliberately not used:
the generator is tuned only against this audit, never against E2D.

    uv run python -m saathibench.audit sim/output/saathibench-v1
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from sklearn.tree import DecisionTreeClassifier

from e2d_core.features import behaviour_features
from e2d_core.features.columns import BEHAVIOUR_COLUMNS
from saathibench.coverage import clinic_keys, read
from saathibench.labels import attack_labels

THRESHOLD = 0.9
FEATURES = BEHAVIOUR_COLUMNS


def table(run: Path, name: str, key: str) -> pl.DataFrame:
    frame = read(run, "tables", name, key)
    if frame is None:
        raise SystemExit(f"{run} has no {name} for clinic {key}")
    return frame


def clinic_features(run: Path, key: str, timezone: str) -> pl.DataFrame:
    events = read(run, "tables", "access_events", key)
    if events is None:
        return pl.DataFrame()
    staff = events.filter(pl.col("role") != "patient")
    behaviour = behaviour_features(
        staff, table(run, "shifts", key), table(run, "sessions", key), table(run, "devices", key)
    )
    local = pl.col("at").dt.convert_time_zone(timezone)
    extra = staff.select(
        (local.dt.hour() + local.dt.minute() / 60).alias("hour_local"),
        local.dt.weekday().alias("weekday"),
        (pl.col("decision") == "deny").cast(pl.Int8).alias("refused"),
    )
    return behaviour.hstack(extra)


def auc(y: np.ndarray, score: np.ndarray) -> float:
    """ROC-AUC regardless of direction: a feature that is lower for attacks separates too."""
    if len(np.unique(score)) < 2:
        return 0.5
    value = float(roc_auc_score(y, score))
    return max(value, 1.0 - value)


def audit(run: Path, max_benign: int = 400_000, seed: int = 0) -> dict[str, Any]:
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    frames = [clinic_features(run, key, manifest["timezone"]) for key in clinic_keys(run)]
    features = pl.concat([f for f in frames if f.height])
    labels = attack_labels(run).select("access_event_id", "is_attack", "attack_type")
    data = features.join(labels, on="access_event_id", how="inner")
    attacks = data.filter(pl.col("is_attack"))
    benign = data.filter(~pl.col("is_attack"))
    if attacks.height == 0:
        raise SystemExit("this run has no attacks to audit")
    if benign.height > max_benign:
        benign = benign.sample(n=max_benign, seed=seed)
    sample = pl.concat([attacks, benign])
    x = sample.select(FEATURES).to_numpy().astype(float)
    y = sample["is_attack"].to_numpy().astype(int)

    single = {name: round(auc(y, x[:, i]), 4) for i, name in enumerate(FEATURES)}
    folds = StratifiedKFold(n_splits=5, shuffle=True, random_state=seed)
    tree = DecisionTreeClassifier(max_depth=2, class_weight="balanced", random_state=seed)
    proba = cross_val_predict(tree, x, y, cv=folds, method="predict_proba")[:, 1]
    tree_auc = round(auc(y, proba), 4)

    by_type: dict[str, Any] = {}
    types = sample["attack_type"].to_numpy()
    benign_rows = y == 0
    for attack_type in sorted(attacks["attack_type"].unique().to_list()):
        rows = benign_rows | (types == attack_type)
        scores = {n: auc(y[rows], x[rows, i]) for i, n in enumerate(FEATURES)}
        best = max(scores, key=lambda k: scores[k])
        by_type[str(attack_type)] = {
            "attacks": int((types == attack_type).sum()),
            "best_feature": best,
            "best_auc": round(scores[best], 4),
        }

    worst = max(single, key=lambda k: single[k])
    failures = [f"{n} {v}" for n, v in single.items() if v > THRESHOLD]
    if tree_auc > THRESHOLD:
        failures.append(f"depth-2 tree {tree_auc}")
    return {
        "run": str(run),
        "threshold": THRESHOLD,
        "attacks": attacks.height,
        "benign_sampled": benign.height,
        "single_feature_auc": single,
        "max_single": {"feature": worst, "auc": single[worst]},
        "depth2_tree_auc": tree_auc,
        "by_attack_type_info": by_type,
        "passed": not failures,
        "failures": failures,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="SaathiBench separability audit.")
    parser.add_argument("run", type=Path)
    parser.add_argument("--max-benign", type=int, default=400_000)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    report = audit(args.run, args.max_benign, args.seed)
    (args.run / "audit.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"{report['attacks']} attack and {report['benign_sampled']} benign staff accesses")
    for name, value in sorted(report["single_feature_auc"].items(), key=lambda kv: -kv[1]):
        print(f"  {name:20} {value:.4f}")
    print(f"  {'depth-2 tree':20} {report['depth2_tree_auc']:.4f}")
    print("per attack type (information only, not part of the gate):")
    for attack_type, row in report["by_attack_type_info"].items():
        best = f"{row['best_feature']} {row['best_auc']}"
        print(f"  type {attack_type:>2} n={row['attacks']:>6}  {best}")
    if report["passed"]:
        print(f"PASS: nothing above {THRESHOLD}")
        return 0
    print(f"FAIL: above {THRESHOLD}: " + ", ".join(report["failures"]))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
