"""Measured hard-negative share, false-explanation rate per attack type, and E2D PR-AUC pooled vs per
clinic, for one seed of the final runs (diagnostics; no registered result is changed).

Run on the execution machine after the final runs, one process per seed:
    python extra.py <seed index>     # writes /Capstone/extra/seed<i>.json
The ten outputs are combined in ../diagnostics.json.
"""
import json, sys
from pathlib import Path
import numpy as np, polars as pl
from sklearn.metrics import average_precision_score
from e2d_core.explain import default_config
from e2d_experiments import evaluate, prepare
from e2d_experiments.evaluate import NON_FORGING_TYPES
from e2d_experiments.methods import make_method
from saathibench.realism import measure
from saathibench.splits import assign

def main() -> None:
    i = int(sys.argv[1]); seed = 20261008 + i
    out = {"seed_index": i}
    for name in ("hard_neg_half", "hard_neg_double"):
        out[f"hard_negative_share_{name}"] = measure(Path(f"/dev/shm/runs/{name}/v1-{name}-s{seed}"))["hard_negative_share"]
    run = Path(f"/dev/shm/runs/none/v1-s{seed}")
    out["hard_negative_share_main"] = measure(run)["hard_negative_share"]
    data = evaluate.with_labels(prepare.prepare(run, workers=1), run)
    definitions = json.loads((run / "splits.json").read_text())
    events = data.select(pl.col("access_event_id").alias("id"), "clinic_id", "user_id", "at")
    labels = data.select("access_event_id", "attack_type")
    theta = default_config().theta
    for split in ("temporal", "user"):
        part = data.with_columns(assign(events, labels, split, definitions).alias("_split"))
        train = part.filter((pl.col("_split") == "train") & (pl.col("role") != "patient"))
        test = evaluate.evaluable(part.filter((pl.col("_split") == "test") & (pl.col("role") != "patient")))
        if split == "temporal":
            fe = (test.filter(pl.col("is_attack") & pl.col("attack_type").is_in(NON_FORGING_TYPES))
                  .group_by("attack_type").agg((pl.col("sigma") >= theta).mean().alias("rate"), pl.len().alias("n"))
                  .sort("attack_type"))
            out["false_explanation_by_type"] = {str(r["attack_type"]): [r["rate"], r["n"]] for r in fe.iter_rows(named=True)}
        method = make_method("e2d", i, "none"); method.fit(train)
        s = np.asarray(method.score(test), dtype=float); y = test["is_attack"].to_numpy().astype(int)
        clinics = test["clinic_id"].to_numpy()
        per = [average_precision_score(y[clinics == c], s[clinics == c]) for c in np.unique(clinics)
               if 0 < y[clinics == c].sum() < (clinics == c).sum()]
        out[f"e2d_pr_auc_{split}"] = {"pooled": average_precision_score(y, s), "mean_per_clinic": float(np.mean(per)),
                                      "gated_share": float((s > 0).mean()), "attacks_gated_share": float((s[y == 1] > 0).mean())}
    Path(f"/Capstone/extra/seed{i}.json").write_text(json.dumps(out, indent=1))
    print("done seed", i, flush=True)

if __name__ == "__main__":
    main()
